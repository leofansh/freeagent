"""工具调用步数硬上限 —— 防「无限循环」唯一有效的部件（设计文档 12.7.2）。

## 为什么审批本身不够

第一次想清楚这件事是因为一个反直觉的结论：**逐次授权防不住循环**。

每一次询问都可能拿到「允许」，所以一个跑偏的 agent 或一次失败的重试，
会**反复**来问 —— 而人被同意**淹没**，不是被拦住。审批是「问」，不是「限」。

所以这个数必须由**我们自己**数、自己停。请 agent「别无限重试」是把安全押在
一个可以忽略它的东西上（prompt）。

## 三个设计决定，都钉在测试里

**1. 检查放在问人之前。** 否则会发出一张卡，人正在点它，而我们会立刻中止
   会话 —— 那张卡点了没有任何后果，**比不发更让人困惑**。

**2. 超限必须主动 ``abort``。** 否则 opencode 那边挂着一个没人会回答的
   授权请求干等 —— 那正是「卡死」的定义，而不是「防住了」。

**3. 报成失败，且说清「这不是失败，是这件事太大」。** 装成「完成」会让
   一次被上限拦下、代码只改了一半的委派在记录里看起来和正常完成一样。

替身形状照抄 ``tests/test_tool_gate.py`` 的 C 组（已验证），不另造一套。
"""
from __future__ import annotations

import datetime
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from freeagent.app import build_app  # noqa: E402
from freeagent.delegate import DEFAULT_MAX_TOOL_CALLS, run_with_tool_gate  # noqa: E402
from freeagent.services.approval import ApprovalPolicy, ApprovalStore, Decision  # noqa: E402
from freeagent.services.clock import FrozenClock  # noqa: E402
from freeagent.services.delegate import DelegationPolicy  # noqa: E402


@pytest.fixture(autouse=True)
def _short_ttl(monkeypatch):
    """把 remote 档 TTL 压到 2 秒。

    真实值 1800 秒 —— 任何**意外的**等待都会把整个套件挂在 30 分钟上，
    而人在超时里什么也学不到（``test_question_loop.py`` 为此付过两次 10 分钟）。
    压短之后该立刻返回的等待会在 2 秒内失败成一条可读的断言。
    """
    real = ApprovalPolicy.for_context

    def patched(context: str) -> ApprovalPolicy:
        got = real(context)
        if got.ttl_seconds > 2:
            return ApprovalPolicy(got.name, 2, may_run_unattended=False)
        return got

    monkeypatch.setattr(ApprovalPolicy, "for_context", staticmethod(patched))


@pytest.fixture()
def store(tmp_path):
    app = build_app(tmp_path / "a.db", clock=FrozenClock(
        datetime.datetime(2026, 10, 1, 12)))
    try:
        yield ApprovalStore(app.conn, clock=app.clock.now)
    finally:
        app.close()


def _asked(rid: str, *, perm: str = "edit", diff: str = "+42") -> tuple[str, Any]:
    return ("permission.asked", {
        "id": rid, "sessionID": "ses_fake", "permission": perm,
        "patterns": ["C:/p/x.txt"],
        "metadata": {"filepath": "C:/p/x.txt", "diff": diff},
        "always": ["*"],
    })


class _FakeServer:
    """照抄 ``test_tool_gate.py`` 的形状 + **加了 ``abort``**。

    加 abort 是必要的：上限生效时我们会主动中止会话，而旧替身没有这个方法 ——
    于是超限路径会抛 ``AttributeError``，把「上限生效了」伪装成「实现坏了」。
    """

    def __init__(self, events) -> None:
        self._events = events
        self.replies: list[tuple[str, str]] = []
        self.aborted: list[str] = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.closed = True

    def create_session(self) -> str:
        return "ses_fake"

    def prompt_async(self, session_id, brief, *, model="", directory=None) -> None:
        pass

    def events(self, **kw):
        yield from self._events

    def reply_permission(self, request_id, decision, *, directory=None) -> None:
        self.replies.append((request_id, decision))

    def abort(self, session_id) -> None:
        self.aborted.append(session_id)


class _AutoApprovingSender:
    """每次发卡就自动「允许」—— 模拟一个什么都点的用户。

    这正是最危险的场景：**人不是被拦住，而是被同意淹没**。
    上限必须在这个前提下依然生效。
    """

    def __init__(self, *, auto: Decision = "allow") -> None:
        self.auto: Decision = auto
        self.cards: list[dict[str, Any]] = []
        self._store: ApprovalStore | None = None

    def bind(self, store: ApprovalStore) -> None:
        self._store = store

    def send_tool_card(self, *, open_id, subject, detail,
                       credential, ttl_seconds) -> str:
        self.cards.append({"open_id": open_id, "credential": credential})
        if self._store is not None and self.auto:
            self._store.resolve(credential, self.auto, decided_by=open_id)
        return "om_test"


class _StubTask:
    id = "task_fake"
    project_path = "C:/p"


def _run(server, sender, store, *, max_tool_calls=None, approver="ou_owner"):
    sender.bind(store)
    kwargs: dict[str, Any] = {}
    if max_tool_calls is not None:
        kwargs["max_tool_calls"] = max_tool_calls
    return run_with_tool_gate(
        _StubTask(), Path("C:/p"), "做点事",
        policy=DelegationPolicy(projects=("C:/p",), model="opencode/big-pickle"),
        store=store, sender=sender, approver=approver,
        server_factory=lambda project, command: server,
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# 上限生效
# --------------------------------------------------------------------------- #
def test_stops_at_the_cap_even_when_user_approves_everything(store):
    """**什么都点也不该无限跑** —— 这是本项存在的全部理由。"""
    events = [_asked(f"per_{i}") for i in range(10)] + [("session.idle", {})]
    oc = _FakeServer(events)
    sender = _AutoApprovingSender(auto="allow")

    got = _run(oc, sender, store, max_tool_calls=3)

    assert got.ok is False, "超限必须报失败，不能装成完成"
    assert len(sender.cards) == 3, (
        f"发了 {len(sender.cards)} 张卡 —— 上限没生效，"
        "这正是「人被同意淹没」的场景"
    )


def test_aborts_the_session_on_cap(store):
    """超限**必须主动 abort**：否则 opencode 挂着一个没人会答的请求干等。"""
    events = [_asked(f"per_{i}") for i in range(10)] + [("session.idle", {})]
    oc = _FakeServer(events)

    _run(oc, _AutoApprovingSender(auto="allow"), store, max_tool_calls=2)

    assert oc.aborted == ["ses_fake"], (
        f"没有 abort：{oc.aborted} —— opencode 会一直挂着等授权，那就是卡死"
    )


def test_no_card_is_sent_for_the_request_that_trips_the_cap(store):
    """**检查在问人之前** —— 不发一张点了没后果的卡。

    一张被立刻作废的卡比不发更糟：人正在点它，而它什么都不会发生。
    """
    events = [_asked(f"per_{i}") for i in range(10)] + [("session.idle", {})]
    oc = _FakeServer(events)
    sender = _AutoApprovingSender(auto="allow")

    _run(oc, sender, store, max_tool_calls=2)

    # 2 张卡对应第 1、2 次请求；第 3 次（触发上限那次）不该发卡
    assert len(sender.cards) == 2
    assert len(oc.replies) == 2, (
        f"回了 {len(oc.replies)} 次 —— 触发上限那次也去问了人，"
        "那就是发了一张点了不作废的卡"
    )


def test_cap_message_says_to_split_the_task(store):
    """报错必须说清「**这件事该拆开**」并给出路。

    只说「超限」是在制造一个不知道该干什么的用户。
    """
    events = [_asked(f"per_{i}") for i in range(5)] + [("session.idle", {})]
    got = _run(_FakeServer(events), _AutoApprovingSender(auto="allow"),
               store, max_tool_calls=2)

    summary = got.summary or ""
    assert "上限" in summary
    assert "拆开" in summary, f"必须告诉用户下一步怎么做：{summary!r}"


def test_records_what_had_been_done(store):
    """超限时**已做的改动要留下记录** —— 它们是真的发生了。"""
    events = [_asked(f"per_{i}") for i in range(5)] + [("session.idle", {})]
    got = _run(_FakeServer(events), _AutoApprovingSender(auto="allow"),
               store, max_tool_calls=2)
    assert got.tool_calls, "已完成的部分要留在记录里，否则用户以为什么都没发生"


# --------------------------------------------------------------------------- #
# 不许误伤：上限之下行为不变
# --------------------------------------------------------------------------- #
def test_under_the_cap_is_unaffected(store):
    """一次正常授权必须照旧成功 —— 修的不能是把好路径弄坏。"""
    oc = _FakeServer([_asked("per_1"), ("session.idle", {})])
    got = _run(oc, _AutoApprovingSender(auto="allow"), store)
    assert got.ok is True, got.summary
    assert len(oc.replies) == 1


def test_default_cap_is_a_sane_number():
    """默认值要有依据，且**不能小到会误伤正常任务**。

    一条真实的多文件改动会用到几十次工具调用；默认 40 低于这个量，
    高于「一个正常任务」该有的次数。
    """
    assert 10 <= DEFAULT_MAX_TOOL_CALLS <= 100, (
        f"默认上限 {DEFAULT_MAX_TOOL_CALLS} 不合理："
        "太小会误伤正常改动，太大等于没有"
    )


def test_zero_disables_the_cap(store):
    """``0`` = 关闭上限（逃生舱）。

    必须存在这个出口，否则一个确实需要很多步的任务**没有任何合法的走法** ——
    用户只能去改源码。
    """
    events = [_asked(f"per_{i}") for i in range(6)] + [("session.idle", {})]
    oc = _FakeServer(events)
    sender = _AutoApprovingSender(auto="allow")
    got = _run(oc, sender, store, max_tool_calls=0)
    assert len(sender.cards) == 6, "上限关了就不该拦"
    assert oc.aborted == [], "上限关了就不该 abort"
    assert got.ok is True


# --------------------------------------------------------------------------- #
# 失败不许被吞
# --------------------------------------------------------------------------- #
def test_abort_failure_still_reports_the_cap(store):
    """``abort`` 自己失败**不许改变结论** —— 结论是「超限停下」。

    会话已经会被 ``with`` 关掉，而我们要报的是超限，不是「中止失败」。
    反过来报「中止失败」会把一个**成功的保护**说成一次故障。
    """

    class _AbortFails(_FakeServer):
        def abort(self, session_id) -> None:
            raise OSError("abort 炸了")

    events = [_asked(f"per_{i}") for i in range(5)] + [("session.idle", {})]
    got = _run(_AbortFails(events), _AutoApprovingSender(auto="allow"),
               store, max_tool_calls=2)
    assert got.ok is False
    assert "上限" in (got.summary or ""), (
        f"结论被 abort 的失败带偏了：{got.summary!r}"
    )