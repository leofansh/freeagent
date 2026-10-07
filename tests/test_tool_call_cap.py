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
from freeagent.delegate import (  # noqa: E402
    DEFAULT_MAX_DISPATCH_SECONDS,
    DEFAULT_MAX_TOOL_CALLS,
    run_with_tool_gate,
)
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

    def prompt_async(self, session_id, brief, *, model="", agent="",
                       variant="", directory=None) -> None:
        # agent / variant：飞书四段选择接进来的（2026-10-07），
        # 签名跟 :meth:`OpenCodeServer.prompt_async` 走。
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


# --------------------------------------------------------------------------- #
# 时长上限：与步数上限**互补**，不是重复
# --------------------------------------------------------------------------- #
#
# 步数上限管「做了太多件事」；时长上限管「这件事花了太久」。
# 一个一直有动静的 agent（持续报进度、反复读写但不问授权）次数上不去，
# 时间却能拖很久 —— 而 ``events()`` 那个 ``timeout=600`` 是 **socket 空闲
# 超时**，只在「600 秒没动静」时抛异常，**「一直有动静」正是它管不到的**。
#
# 所以两条断言必须区分「原因」和「给的路」：混成同一句话会让人往错的方向
# 拆需求 —— 而「拆需求」对一件卡住的事完全没用。


class _BusyServer(_FakeServer):
    """一直吐无关事件，但**从不需要授权**。

    这正是步数上限抓不到、而时长上限必须抓的那一类：次数不涨，时间在走。
    """

    def __init__(self, n: int = 50) -> None:
        super().__init__([("message.part.updated", {"i": i}) for i in range(n)]
                         + [("session.idle", {})])


class _Ticker:
    """每次调用就往前跳 ``step`` 秒的假时钟。

    **为什么不用「传一个很小的 max_seconds 然后等它过期」**：
    我第一版就那么写的（``max_seconds=0.001``），而假 server 建会话只要微秒 ——
    循环第一次转起来时 1ms **还没到**，于是上限没触发，测试红成了一个
    不存在的 bug。靠 sleep 修不好：调大就慢、调小就 flaky。
    注入时钟让上限**确定性**可测，这也正是本仓库 ``FrozenClock`` 的做法
    （README：「时间由可注入的 FrozenClock 驱动」）。
    """

    def __init__(self, step: float) -> None:
        self.t = 0.0
        self.step = step

    def __call__(self) -> float:
        self.t += self.step
        return self.t


#: 跳得足够快，让第一个事件就落在期限之后
_FAST = 100.0
#: 跳得足够慢，期限内不会越过
_SLOW = 0.001


def _run_timed(server, sender, store, *, max_seconds, step=_FAST):
    sender.bind(store)
    return run_with_tool_gate(
        _StubTask(), Path("C:/p"), "做点事",
        policy=DelegationPolicy(projects=("C:/p",), model="opencode/big-pickle"),
        store=store, sender=sender, approver="ou_owner",
        server_factory=lambda project, command: server,
        max_seconds=max_seconds,
        now=_Ticker(step),
    )


def test_time_cap_stops_a_busy_but_silent_agent(store):
    """一直吐事件但从不问授权 —— 次数不涨，**必须被时长上限拦住**。"""
    oc = _BusyServer()
    sender = _AutoApprovingSender(auto="allow")

    # 快时钟：第一个事件就落在期限之后，于是立刻触发。
    got = _run_timed(oc, sender, store, max_seconds=30, step=_FAST)

    assert got.ok is False, "超时必须报失败"
    assert oc.aborted == ["ses_fake"], f"没 abort：{oc.aborted}"
    assert sender.cards == [], "不该发授权卡 —— 它一次都没问过"


def test_time_cap_does_not_fire_under_a_generous_budget(store):
    """预算够时**不许误伤** —— 修的不能是把好路径弄坏。"""
    oc = _FakeServer([_asked("per_1"), ("session.idle", {})])
    sender = _AutoApprovingSender(auto="allow")

    got = _run_timed(oc, sender, store, max_seconds=600, step=_SLOW)

    assert got.ok is True, got.summary
    assert len(sender.cards) == 1
    assert oc.aborted == []


def test_zero_seconds_disables_the_time_cap(store):
    """``0`` = 关闭时长上限（逃生舱）。

    与步数上限同理由：必须有这个出口，否则一个确实要跑很久的任务
    没有任何合法的走法。
    """
    oc = _BusyServer(n=6)
    got = _run_timed(oc, _AutoApprovingSender(auto="allow"), store, max_seconds=0)
    assert got.ok is True, "上限关了就不该拦"
    assert oc.aborted == []


def test_time_cap_message_says_stuck_not_too_many(store):
    """时长超限必须说「**卡住了**」而不是「该拆开」。

    这条是本组最要紧的断言：对一件卡住的事说「拆成小份」，用户会去拆需求 ——
    而那完全没用，因为他真正需要知道的是「它卡住了」。
    """
    got = _run_timed(_BusyServer(), _AutoApprovingSender(auto="allow"),
                     store, max_seconds=30, step=_FAST)
    summary = got.summary or ""
    assert "时长" in summary or "秒" in summary
    assert "卡住" in summary, f"必须说清是卡住而不是太多：{summary!r}"
    assert "拆开" not in summary, (
        f"不该建议「拆开」—— 对卡住的事没用：{summary!r}"
    )


def test_time_cap_records_partial_progress(store):
    """超限时已做的部分要留在记录里 —— 它们真的发生了。"""
    oc = _FakeServer([_asked("per_1"), ("session.idle", {})])
    sender = _AutoApprovingSender(auto="allow")

    # 先正常跑一次拿到 tool_calls 的形状，再用极短预算跑一次带事件的
    got_ok = _run_timed(oc, sender, store, max_seconds=600)
    assert got_ok.tool_calls

    busy = _BusyServer()
    got = _run_timed(busy, _AutoApprovingSender(auto="allow"),
                     store, max_seconds=0.001)
    assert got.session_id == "ses_fake"


def test_default_time_budget_is_sane():
    """默认时长要有依据：够一次真实的多文件改动跑完，又不至于无限拖。

    30 分钟。这个值**必须小于**人愿意等的极限 —— 超过就该让人主动去喊停，
    而那需要终止接口（尚未实现）。
    """
    assert 300 <= DEFAULT_MAX_DISPATCH_SECONDS <= 3600, (
        f"默认时长 {DEFAULT_MAX_DISPATCH_SECONDS}s 不合理："
        "太短会误伤真实改动，太长等于没有"
    )


def test_abort_failure_on_time_cap_still_reports_the_cap(store):
    """``abort`` 失败**不许改变结论** —— 结论是「超限停下」。"""

    class _AbortFails(_FakeServer):
        def abort(self, session_id) -> None:
            raise OSError("abort 炸了")

    got = _run_timed(_AbortFails(_BusyServer()._events),
                     _AutoApprovingSender(auto="allow"), store,
                     max_seconds=30, step=_FAST)
    assert got.ok is False
    assert "卡住" in (got.summary or ""), (
        f"结论被 abort 的失败带偏了：{got.summary!r}"
    )
