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
from freeagent.storage.db import connect, init_schema


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
    init_schema(conn)
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
