"""待确认存储的守卫。

重点全在**失败方向**：
- 过期一律拒绝（哪怕调用方想写「允许」）
- 沉默不等于同意
- 已过期之后迟到的「允许」作废
- 凭据不可猜

「能存能取」那种正向断言几乎不用写 —— 真正会出事的是上面这几条。
"""
from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from freeagent.services.approval import (
    DEFAULT_TTL_SECONDS,
    ApprovalStore,
    new_credential,
)
from freeagent.storage.db import connect, migrate


class FakeClock:
    """可推进的时钟。过期行为必须能**确定地**测，不能靠 sleep。"""

    def __init__(self, start: datetime.datetime) -> None:
        self.now = start

    def __call__(self) -> datetime.datetime:
        return self.now

    def advance(self, **kw) -> None:
        self.now = self.now + datetime.timedelta(**kw)


@pytest.fixture
def store(tmp_path):
    conn = connect(Path(tmp_path) / "a.db")
    migrate(conn)
    clock = FakeClock(datetime.datetime(2026, 9, 29, 12, 0, 0))
    return ApprovalStore(conn, clock=clock), clock


# =============================================================================
class TestRoundTrip:
    def test_ask_then_get(self, store):
        s, _ = store
        item = s.ask("读目录 D:/x", detail="只读列出")
        got = s.get(item.credential)
        assert got is not None
        assert got.subject == "读目录 D:/x"
        assert got.detail == "只读列出"
        assert got.decision is None, "刚问的时候还没有答复"

    def test_unknown_credential_is_none(self, store):
        s, _ = store
        assert s.get("ap-不存在") is None
        assert s.decide("ap-不存在") is None


# =============================================================================
# 沉默不等于同意 —— 整份文件存在的理由
# =============================================================================
class TestSilenceIsNotConsent:
    def test_unanswered_within_ttl_is_none_not_allow(self, store):
        s, _ = store
        item = s.ask("读目录")
        assert s.decide(item.credential) is None, (
            "还没答就必须返回 None —— 返回 allow 就是把沉默读成同意"
        )

    def test_expired_unanswered_is_deny(self, store):
        """没答 + 过期 = 拒绝。**不是 None**，否则会一直等下去。"""
        s, clock = store
        item = s.ask("读目录", ttl_seconds=60)
        clock.advance(seconds=61)
        assert s.decide(item.credential) == "deny", (
            "过期必须读成拒绝；读成 None 会让请求方无限等待"
        )

    def test_wait_times_out_to_deny(self, store):
        """轮询到超时也必须返回 deny —— 这是 silence-is-not-consent 的端到端形态。"""
        s, _ = store
        item = s.ask("读目录", ttl_seconds=0)
        assert s.wait(item.credential, poll_seconds=0.01, timeout_seconds=0.1) == "deny"


# =============================================================================
# 过期即拒绝 —— 哪怕调用方想写「允许」
# =============================================================================
class TestExpiryBeatsAllow:
    def test_resolve_allow_on_expired_is_downgraded(self, store):
        s, clock = store
        item = s.ask("读目录", ttl_seconds=60)
        clock.advance(seconds=61)
        s.resolve(item.credential, "allow", decided_by="ou_x")
        assert s.decide(item.credential) == "deny", (
            "过期后写「允许」必须被降级 —— 那等于让失效授权变成永久授权"
        )

    def test_allow_written_before_expiry_survives(self, store):
        """反过来：在有效期内答的允许，离过期的允许 —— 别把好的一起毙了。"""
        s, clock = store
        item = s.ask("读目录", ttl_seconds=600)
        s.resolve(item.credential, "allow", decided_by="ou_x")
        clock.advance(seconds=120)
        assert s.decide(item.credential) == "allow"

    def test_allow_then_late_read_still_allow(self, store):
        """答复**写入时**在有效期内即可；之后过期不影响已给出的结论。

        这是上面那条的推论：过期拦的是「还没答就过期」，
        不是「答完之后把它翻掉」—— 那样会让用户点了还白点。
        """
        s, clock = store
        item = s.ask("读目录", ttl_seconds=60)
        s.resolve(item.credential, "allow", decided_by="ou_x")
        clock.advance(seconds=300)
        assert s.decide(item.credential) == "allow"

    def test_expired_resolve_records_the_downgrade(self, store):
        """降级要**留痕** —— decided_by 记成 expired。"""
        s, clock = store
        item = s.ask("读目录", ttl_seconds=60)
        clock.advance(seconds=61)
        s.resolve(item.credential, "allow", decided_by="ou_x")
        got = s.get(item.credential)
        assert got is not None
        assert got.decision == "deny"
        assert "expired" in (got.decided_by or ""), "降级必须留痕，否则查不出为何被拒"


# =============================================================================
class TestDeny:
    def test_explicit_deny(self, store):
        s, _ = store
        item = s.ask("读目录")
        s.resolve(item.credential, "deny", decided_by="ou_x")
        assert s.decide(item.credential) == "deny"

    def test_deny_within_ttl_stays_deny_after_expiry(self, store):
        s, clock = store
        item = s.ask("读目录", ttl_seconds=60)
        s.resolve(item.credential, "deny", decided_by="ou_x")
        clock.advance(seconds=300)
        assert s.decide(item.credential) == "deny"


# =============================================================================
class TestCredential:
    def test_is_unguessable_and_prefixed(self):
        """凭据会出现在卡片里，拿到就能批准一次本地操作 → 必须防猜。"""
        creds = {new_credential() for _ in range(200)}
        assert len(creds) == 200, "凭据撞了"
        assert all(c.startswith("ap-") for c in creds)
        # 不可猜：不能只由时间/计数推出来
        assert not any(c.endswith("0") and len(c) < 12 for c in creds)

    def test_repeat_ask_gets_new_credential(self, store):
        """重试要换新凭据，否则两个有效凭据同时指向同一次授权。"""
        s, _ = store
        a = s.ask("读目录")
        b = s.ask("读目录")
        assert a.credential != b.credential


class TestCard:
    def test_record_card_after_send(self, store):
        """message_id 是**发卡之后**才知道的，所以单独一个方法补记。"""
        s, _ = store
        item = s.ask("读目录")
        s.record_card(item.credential, "om_x123")
        got = s.get(item.credential)
        assert got is not None and got.open_message_id == "om_x123"


class TestPurge:
    def test_purges_only_old_rows(self, store):
        s, clock = store
        old = s.ask("旧的", ttl_seconds=10)
        clock.advance(seconds=100)
        new = s.ask("新的", ttl_seconds=600)
        removed = s.purge_expired(older_than_seconds=50)
        assert removed == 1
        assert s.get(old.credential) is None
        assert s.get(new.credential) is not None


# =============================================================================
class TestSettledCardsForTask:
    """V1.88：收尾要拿 (message_id, subject, decision, kind) 拼终态收据。

    它是 ``card_message_ids_for_task`` 的替代 —— 撤回改原地改写后，
    光有 id 不够，还得知道**这张卡是什么、结论是什么**。
    """

    def test_returns_cards_with_subject_and_decision(self, store):
        s, _ = store
        item = s.ask("opencode 想 shell", task_id="t1")
        s.record_card(item.credential, "om_1")
        s.resolve(item.credential, "allow")
        got = s.settled_cards_for_task("t1")
        assert got == (("om_1", "opencode 想 shell", "allow", "approval"),)

    def test_empty_task_id_returns_empty(self, store):
        s, _ = store
        assert s.settled_cards_for_task("") == ()

    def test_unmatched_task_returns_empty(self, store):
        s, _ = store
        item = s.ask("x", task_id="t1")
        s.record_card(item.credential, "om_1")
        assert s.settled_cards_for_task("other") == ()

    def test_null_kind_reads_as_approval(self, store):
        """旧行 kind 为 NULL（迁移只加列不回填）：读侧按「授权」处理。"""
        s, _ = store
        item = s.ask("x", task_id="t1")
        s.record_card(item.credential, "om_1")
        s._conn.execute("UPDATE pending_approvals SET kind = NULL")
        s._conn.commit()
        got = s.settled_cards_for_task("t1")
        assert got[0][3] == "approval"

    def test_deny_decision_is_carried_not_interpreted(self, store):
        """查询层不做语义翻译 —— 「拒绝还是过期」是渲染层（_verdict_line）的事。"""
        s, _ = store
        item = s.ask("x", task_id="t1")
        s.record_card(item.credential, "om_1")
        s.resolve(item.credential, "deny")
        got = s.settled_cards_for_task("t1")
        assert got[0][2] == "deny"


# =============================================================================
class TestDecidedVia:
    """V1.92：**结论是怎么来的**要落下来，别让读侧拿「谁」猜「怎么」。"""

    def test_default_is_blank_not_card(self, store):
        """不传 ``via`` 就是空（未记）。

        **不能默认成卡片**：那会把所有忘了传的调用方冒名成「人手点过」，
        而「人手点过」正是审计视图唯一当成治理信号的那一类。
        """
        s, _ = store
        item = s.ask("读目录 D:/x")
        s.resolve(item.credential, "allow", decided_by="u1")
        assert s.get(item.credential).decided_via == ""

    def test_card_is_recorded(self, store):
        s, _ = store
        item = s.ask("读目录 D:/x")
        s.resolve(item.credential, "allow", decided_by="u1", via="card")
        assert s.get(item.credential).decided_via == "card"

    def test_stop_is_recorded_even_when_expired(self, store):
        """迟到的点击也记来源：「他点了」是事实，「卡已过期」是另一件事。"""
        s, clock = store
        item = s.ask("读目录 D:/x")
        clock.advance(minutes=11)
        s.resolve(item.credential, "deny", decided_by="u1", via="stop")
        got = s.get(item.credential)
        assert got.decided_via == "stop"
        assert got.decided_by == "expired", (
            "过期时 decided_by 仍写 expired —— 记下点击者会让人以为那次点击算数"
        )


# =============================================================================
class TestEvidence:
    """V1.92：一条 ``deny`` 其实是三件事 —— 拒绝 / 叫停 / 没人看。

    三个桶分开之后，「拒绝率」才第一次有确定的含义：它的分子只剩
    「他看过了、不同意」这一种。把「我没看见」也加进去，得出的数
    既不能说明提议有问题，也不能说明打扰太多 —— 两个方向都改不动。
    """

    def test_allow_counted(self, store):
        s, _ = store
        item = s.ask("读目录 D:/x")
        s.resolve(item.credential, "allow", decided_by="u1", via="card")
        rep = s.evidence()
        assert (rep.total, rep.allow) == (1, 1)

    def test_explicit_deny_is_its_own_bucket(self, store):
        s, _ = store
        item = s.ask("读目录 D:/x")
        s.resolve(item.credential, "deny", decided_by="u1", via="card")
        rep = s.evidence()
        assert (rep.deny, rep.timeout, rep.late) == (1, 0, 0)

    def test_ignored_card_is_timeout_not_deny(self, store):
        """**本次改动的重点。** 没人看的卡不算拒绝。

        拆之前两者都是 ``decision='deny'`` —— 一次「我没看」和一次
        「我不同意」被加进同一个分子，而这两件事该改的东西完全相反。
        """
        s, clock = store
        s.ask("读目录 D:/x")
        clock.advance(minutes=11)
        rep = s.evidence()
        assert (rep.timeout, rep.deny) == (1, 0)

    def test_stop_is_not_deny(self, store):
        s, _ = store
        item = s.ask("派到 freeagent")
        s.resolve(item.credential, "deny", decided_by="u1", via="stop")
        s.request_stop(item.credential, by="u1")
        rep = s.evidence()
        assert (rep.stop, rep.deny) == (1, 0)

    def test_late_click_is_not_deny(self, store):
        s, clock = store
        item = s.ask("读目录 D:/x")
        clock.advance(minutes=11)
        s.resolve(item.credential, "allow", decided_by="u1", via="card")
        rep = s.evidence()
        assert (rep.late, rep.allow) == (1, 0), "过期后才点的允许不能算允许"

    def test_open_is_not_yet_anything(self, store):
        s, _ = store
        s.ask("读目录 D:/x")
        rep = s.evidence()
        assert (rep.open, rep.timeout) == (1, 0)

    def test_questions_are_excluded(self, store):
        """提问没有 allow/deny。混进来会把「没人看」这一列稀释掉 ——
        而那一列恰恰是用来判断「提醒是不是太吵」的。"""
        s, _ = store
        s.request_question("用哪个模型？", questions=["a", "b"])
        assert s.evidence().total == 0

    def test_waiting_seconds_measured_from_asked(self, store):
        s, clock = store
        item = s.ask("读目录 D:/x")
        clock.advance(minutes=3)
        s.resolve(item.credential, "allow", decided_by="u1", via="card")
        entry = s.evidence().entries[0]
        assert entry.waited_seconds == 180

    def test_timeout_waiting_equals_ttl(self, store):
        """没人答的「等了多久」= TTL，不是 ``now - asked``。

        用 ``now - asked`` 的话，这个数会随着你看命令的时间点一直变大 ——
        而事实是「这张卡只等了 10 分钟就过期了」。
        """
        s, clock = store
        s.ask("读目录 D:/x")
        clock.advance(minutes=30)
        assert s.evidence().entries[0].waited_seconds == 600

    def test_entries_capped_but_total_counts_all(self, store):
        s, _ = store
        for i in range(5):
            item = s.ask(f"任务 {i}")
            s.resolve(item.credential, "allow", decided_by="u1", via="card")
        rep = s.evidence(limit=2)
        assert rep.total == 5, "计数看全量"
        assert len(rep.entries) == 2, "明细按上限裁"

    def test_window_excludes_older_rows(self, store):
        s, clock = store
        item = s.ask("旧的一条")
        s.resolve(item.credential, "allow", decided_by="u1", via="card")
        clock.advance(days=100)
        assert s.evidence(since_days=90).total == 0
        assert s.evidence(since_days=180).total == 1
