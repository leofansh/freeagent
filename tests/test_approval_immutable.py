"""两次点击的守卫 —— 「已决定」不可翻。

背景（实测）：卡片按钮会一直留在那儿，用户能点第二次。第一次「拒绝」、
第二次「允许」，决策被改写，一张用过的卡片变成长效授权。

这与「过期不可翻」是同一条原则：结论在**落库那一刻**定死。
后者更严重，因为它不用等时间。
"""
from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from freeagent.services.approval import ApprovalStore
from freeagent.storage.db import connect, init_schema


class FakeClock:
    def __init__(self, t: datetime.datetime) -> None:
        self.now = t

    def __call__(self) -> datetime.datetime:
        return self.now

    def advance(self, **kw) -> None:
        self.now += datetime.timedelta(**kw)


@pytest.fixture
def store(tmp_path):
    conn = connect(Path(tmp_path) / "a.db")
    init_schema(conn)
    return ApprovalStore(conn, clock=FakeClock(datetime.datetime(2026, 9, 29, 12, 0)))


class TestDecisionIsFinal:
    def test_deny_then_allow_stays_deny(self, store):
        item = store.ask("读目录")
        store.resolve(item.credential, "deny", decided_by="ou_A")
        store.resolve(item.credential, "allow", decided_by="ou_A")
        assert store.decide(item.credential) == "deny", (
            "第二次点击把「拒绝」改成了「允许」—— 用过的卡片会变成长效授权"
        )

    def test_allow_then_deny_stays_allow(self, store):
        """反方向同样锁死。不能靠点两次把自己从「已允许」洗成「已拒绝」，
        再点第三次又变回来 —— 那等于决策可随意摆弄。"""
        item = store.ask("读目录")
        store.resolve(item.credential, "allow", decided_by="ou_A")
        store.resolve(item.credential, "deny", decided_by="ou_A")
        assert store.decide(item.credential) == "allow"

    def test_resolve_returns_false_on_second_click(self, store):
        """返回 False = 「没写成」。调用方据此知道该提示「这张卡已经用过了」。"""
        item = store.ask("读目录")
        assert store.resolve(item.credential, "allow", decided_by="ou_A") is True
        assert store.resolve(item.credential, "allow", decided_by="ou_A") is False

    def test_second_click_does_not_overwrite_who(self, store):
        """第一次是谁批的，就永远记谁 —— 不能被第二次点击改掉。"""
        item = store.ask("读目录")
        store.resolve(item.credential, "allow", decided_by="ou_第一次")
        store.resolve(item.credential, "deny", decided_by="ou_第二次")
        got = store.get(item.credential)
        assert got.decided_by == "ou_第一次", "决策人被第二次点击改写了"

    def test_expired_then_allow_still_deny(self, store):
        """过期降级成 deny 之后，**再点允许也不该翻回来**。"""
        item = store.ask("读目录", ttl_seconds=1)
        store._clock.advance(seconds=5)
        store.resolve(item.credential, "allow", decided_by="ou_A")   # 降级为 deny
        store.resolve(item.credential, "allow", decided_by="ou_A")   # 再点一次
        assert store.decide(item.credential) == "deny"
        assert store.get(item.credential).decided_by == "expired", (
            "降级留痕被第二次点击覆盖了"
        )

    def test_unknown_credential_still_false(self, store):
        assert store.resolve("ap-不存在", "allow") is False


class TestStillWaitsWhenUndecided:
    def test_first_resolve_wins_and_is_recorded(self, store):
        item = store.ask("读目录")
        assert store.resolve(item.credential, "allow", decided_by="ou_A") is True
        got = store.get(item.credential)
        assert got.decision == "allow"
        assert got.decided_by == "ou_A"
        assert got.decided_at is not None
