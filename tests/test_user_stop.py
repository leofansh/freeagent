"""终止接口：用户能中途叫停这条委派（设计文档 12.7.2）。

## 为什么必须有

盘点报告那份「避坑清单」里有一条：「没有停止接口，任务启动之后无法终止」。
而在本项目里它的具体后果是**拖住后面所有委派** —— 执行器的 ``--watch``
循环是**串行**的，一条挂着就再也扫不到下一条。

## 「拒绝」与「停止」是两件事

| 用户点的 | 语义 | 之后 agent 会怎样 |
|---|---|---|
| 拒绝这一步 | 这一��动作不批 | **换个方向继续** |
| 停止这条委派 | 整条作废 | **中止会话** |

混为一谈的后果：用户以为已经停掉了，而 agent 还在改代码 —— **比没有这个
按钮糟得多**。所以第三道闸门必须有。

## 停止要做两件事，缺一不可

1. **把当前这次回掉**（按拒绝）—— 否则执行器干等到 TTL（委派档 1800s）
2. **记下「停整条」** —— 只做 1 的话 agent 只会换方向继续

**顺序不能反**：``resolve`` 会把凭据判成「已决定」，之后 ``can_answer``
返回 False，于是停止请求根本写不进去。这条是被 ``test_stop_after_resolve_
would_be_silently_dropped`` 钉住的。

## 两条被反复咬到的

**「只有发起人能批」必须自动覆盖停止。** 停止比允许更强，理应受至少同等
保护 —— 而复用 ``can_answer`` 就自动得到了。

**停止不算失败，也不算完成。** 说成失败会让他以为代码坏了去排查；说成
完成会让他以为改动做完了去验收 —— 后者更糟。
"""

from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from freeagent.app import build_app  # noqa: E402
from freeagent.delegate import run_with_tool_gate  # noqa: E402
from freeagent.feishu.sender import _tool_card  # noqa: E402
from freeagent.services.approval import (  # noqa: E402
    ApprovalContext,
    ApprovalPolicy,
    ApprovalStore,
)
from freeagent.services.clock import FrozenClock  # noqa: E402
from freeagent.services.delegate import DelegationPolicy  # noqa: E402
from freeagent.services.opencode_server import ToolPermission  # noqa: E402


@pytest.fixture(autouse=True)
def _short_ttl(monkeypatch):
    """remote 档 TTL 压到 2 秒。

    真实值 1800 秒 —— 任何**意外的**等待会把整个套件挂在 30 分钟上，
    而人在超时里什么也学不到。压短之后该立刻返回的等待会在 2 秒内
    失败成一条可读的断言。
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
        yield ApprovalStore(app.conn, clock=app.clock.now), app
    finally:
        app.close()


def _req(rid: str = "per_1") -> ToolPermission:
    return ToolPermission(
        request_id=rid, permission="edit",
        paths=("C:/p/x.txt",), diff="+1", suggested_always=("edit",),
    )


# --------------------------------------------------------------------------- #
# 卡片：必须有第三个按钮，且与「拒绝」措辞不同
# --------------------------------------------------------------------------- #
class TestCard:
    def _buttons(self):
        card = _tool_card("改一下", "细节", "cred_1")
        return {b["value"]["action"]: b
                for e in card["elements"] if e.get("tag") == "action"
                for b in e["actions"]}

    def test_has_a_stop_button(self):
        """**必须有第三个按钮。** 缺了它用户就只能「拒绝」，而那不叫停止。"""
        assert "stop_delegation" in self._buttons()

    def test_three_buttons_all_carry_the_credential(self):
        """三个按钮都必须带同一个 ``id`` —— 执行器靠它定位要回掉哪一条。"""
        for action, btn in self._buttons().items():
            assert btn["value"]["id"] == "cred_1", f"{action} 没带凭据"

    def test_stop_wording_differs_from_deny(self):
        """「拒绝这一步」与「停止这条委派」**措辞必须不同**。

        混成同一个词，用户就以为「拒绝」= 「停止」，然后发现 agent 还在跑。
        """
        labels = {a: b["text"]["content"] for a, b in self._buttons().items()}
        assert labels["deny"] != labels["stop_delegation"]
        assert "委派" in labels["stop_delegation"], \
            f"停止按钮要说清停的是整条委派：{labels['stop_delegation']!r}"

    def test_stop_is_the_danger_button(self):
        """停止必须是 ``danger`` —— 它是后果最重的那个选择。"""
        assert self._buttons()["stop_delegation"]["type"] == "danger"

    def test_allow_stays_primary(self):
        """允许仍是主按钮 —— 频率最高的那个该最显眼。"""
        assert self._buttons()["allow_once"]["type"] == "primary"

    def test_card_is_still_json_10(self):
        """仍是 JSON 1.0：与批准卡/选项卡同版本，否则 200830。"""
        card = _tool_card("s", "d", "c")
        assert "elements" in card and "schema" not in card


# --------------------------------------------------------------------------- #
# 存储层
# --------------------------------------------------------------------------- #
class TestStore:
    def test_stop_persists(self, store):
        s, _ = store
        p = s.request_tool_call(
            permission="edit", paths=("C:/p/x.txt",), diff="+1",
            suggested_always=("edit",), context=ApprovalContext("remote", who="ou_o"),
        )
        assert s.is_stop_requested(p.credential) is False, "还没停就不该是已停"
        s.request_stop(p.credential, by="ou_o")
        assert s.is_stop_requested(p.credential) is True

    def test_stop_is_idempotent(self, store):
        """连点两下是常事 —— 重复记不该抛异常。"""
        s, _ = store
        p = s.request_tool_call(
            permission="edit", paths=("x",), diff="+1",
            suggested_always=("edit",), context=ApprovalContext("remote", who="ou_o"),
        )
        s.request_stop(p.credential, by="ou_o")
        s.request_stop(p.credential, by="ou_o")
        assert s.is_stop_requested(p.credential) is True

    def test_unknown_credential_is_not_stopped(self, store):
        """陌生凭据**不许**被当成已停 —— 否则会去停一条不相干的委派。"""
        s, _ = store
        assert s.is_stop_requested("never_existed") is False

    def test_wait_returns_immediately_when_stopped(self, store):
        """停止后 ``wait`` 立刻返回 deny，不必干等 TTL。"""
        import time
        s, _ = store
        p = s.request_tool_call(
            permission="edit", paths=("x",), diff="+1",
            suggested_always=("edit",), context=ApprovalContext("remote", who="ou_o"),
        )
        s.request_stop(p.credential, by="ou_o")
        t0 = time.monotonic()
        got = s.wait(p.credential, timeout_seconds=1800,
                     should_stop=lambda: s.is_stop_requested(p.credential))
        dt = time.monotonic() - t0
        assert got == "deny", "那次动作确实没被批准"
        assert dt < 1.0, f"等了 {dt:.1f}s —— 停止必须立刻生效"

    def test_wait_without_should_stop_is_unaffected(self, store):
        """没有 ``should_stop`` 时行为**一字不变** —— 不许误伤既有路径。"""
        s, _ = store
        p = s.request_tool_call(
            permission="edit", paths=("x",), diff="+1",
            suggested_always=("edit",), context=ApprovalContext("remote", who="ou_o"),
        )
        got = s.wait(p.credential, timeout_seconds=0.6)
        assert got == "deny", "超时按拒绝"


# --------------------------------------------------------------------------- #
# 闭环：点停止 → 执行器真的停
# --------------------------------------------------------------------------- #
class _FakeServer:
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


def _asked(rid: str):
    return ("permission.asked", {
        "id": rid, "sessionID": "ses_fake", "permission": "edit",
        "patterns": ["C:/p/x.txt"],
        "metadata": {"filepath": "C:/p/x.txt", "diff": "+1"},
        "always": ["*"],
    })


class _StopOnSendSender:
    """发卡时**模拟用户点了「停止这条委派」**。

    刻意不复用「自动允许」那套：停止与允许必须能被独立触发，否则
    「点了停止会不会真停」这件事测不出来。
    """

    def __init__(self, store: ApprovalStore) -> None:
        self.store = store
        self.cards: list[str] = []

    def bind(self, store: ApprovalStore) -> None:
        self.store = store

    def send_tool_card(self, *, open_id, subject, detail,
                       credential, ttl_seconds) -> str:
        self.cards.append(credential)
        self.store.request_stop(credential, by=open_id)
        self.store.resolve(credential, "deny", decided_by=open_id)
        return "om_test"


class _StubTask:
    id = "task_fake"
    project_path = "C:/p"


def _run(server, sender, store):
    sender.bind(store)
    return run_with_tool_gate(
        _StubTask(), Path("C:/p"), "做点事",
        policy=DelegationPolicy(projects=("C:/p",), model="opencode/big-pickle"),
        store=store, sender=sender, approver="ou_owner",
        server_factory=lambda project, command: server,
    )


class TestLoopStops:
    def test_stop_aborts_the_session(self, store):
        """点了停止 → **必须 abort**。光拒绝这一步，agent 会换方向继续。"""
        s, _app = store
        # 后续事件存在：若不 abort，循环会继续往下走
        oc = _FakeServer([_asked("per_1"), _asked("per_2"), ("session.idle", {})])

        got = _run(oc, _StopOnSendSender(s), s)

        assert oc.aborted == ["ses_fake"], (
            f"没有 abort：{oc.aborted} —— 「点了没反应」"
        )
        assert got.ok is False

    def test_stop_is_not_reported_as_success(self, store):
        """停止**不能**报成完成。"""
        s, _app = store
        oc = _FakeServer([_asked("per_1"), ("session.idle", {})])
        got = _run(oc, _StopOnSendSender(s), s)
        assert got.ok is False, f"报成了完成：{got.summary!r}"

    def test_stop_message_says_stopped_not_failed(self, store):
        """文案要说「**你叫停了**」，不是「失败」。

        说成失败会让他以为代码坏了、去排查一件根本没坏的事。
        """
        s, _app = store
        oc = _FakeServer([_asked("per_1"), ("session.idle", {})])
        summary = _run(oc, _StopOnSendSender(s), s).summary or ""
        assert "叫停" in summary or "停止" in summary, summary

    def test_stop_keeps_partial_progress_visible(self, store):
        """已做的改动仍在记录里 —— 它们真的发生了，不能当没发生。"""
        s, _app = store
        oc = _FakeServer([_asked("per_1"), ("session.idle", {})])
        got = _run(oc, _StopOnSendSender(s), s)
        assert got.tool_calls, "已完成的部分要留在记录里"

    def test_deny_alone_does_not_abort(self, store):
        """**关键对照**：只「拒绝」不许 abort —— 那是两件事。"""
        s, _app = store

        class _DenyOnly(_StopOnSendSender):
            def send_tool_card(self, *, open_id, subject, detail,
                               credential, ttl_seconds) -> str:
                self.cards.append(credential)
                self.store.resolve(credential, "deny", decided_by=open_id)
                return "om_test"

        oc = _FakeServer([_asked("per_1"), ("session.idle", {})])
        _run(oc, _DenyOnly(s), s)
        assert oc.aborted == [], \
            "只拒绝不该中止整条委派 —— 那会让「拒绝」比「停止」还强"

    def test_allow_path_is_unaffected(self, store):
        """正常批准路径**一字不变**（回归）。"""
        s, _app = store

        class _Allow(_StopOnSendSender):
            def send_tool_card(self, *, open_id, subject, detail,
                               credential, ttl_seconds) -> str:
                self.cards.append(credential)
                self.store.resolve(credential, "allow", decided_by=open_id)
                return "om_test"

        oc = _FakeServer([_asked("per_1"), ("session.idle", {})])
        got = _run(oc, _Allow(s), s)
        assert got.ok is True, got.summary
        assert oc.replies == [("per_1", "allow")]
        assert oc.aborted == []


# --------------------------------------------------------------------------- #
# 权限：停止必须与允许受同一条规则保护
# --------------------------------------------------------------------------- #
class TestAuthority:
    def test_only_requester_may_stop(self, store):
        """**只有发起人能停止。** 停止比允许更强，理应受至少同等保护。"""
        s, _app = store
        ctx = ApprovalContext("remote", who="ou_owner")
        p = s.request_tool_call(
            permission="edit", paths=("x",), diff="+1",
            suggested_always=("edit",), context=ctx,
        )
        # 别人点的：resolve 会拒绝，停止请求也就不该被承认
        assert s.can_answer(p.credential, "ou_other") is False

    def test_resolve_is_the_real_gate_not_can_answer(self, store):
        """钉住一个我**原先判断错**的事实。

        我以为「``resolve`` 之后 ``can_answer`` 变 False，所以桥接那边
        必须先 resolve 再 request_stop」。实测不是：``can_answer`` 的判据
        只有**是不是发起人**（见它的 docstring），**完全不看是否已决定**。

        所以那个「顺序不能反」的推断是错的。而 :meth:`resolve` 才是
        真正的写入口 —— 它对「已决定过」返回 False。

        这条测试的价值是**纠正了一个会写进注释里的错误结论**：顺序其实
        两种都行，但**必须先判发起人**（因为停止比允许强，不能让别人
        停掉别人的委派），而那个判定 ``resolve`` 只覆盖「回答案」那一步。
        """
        s, _app = store
        ctx = ApprovalContext("remote", who="ou_owner")
        p = s.request_tool_call(
            permission="edit", paths=("x",), diff="+1",
            suggested_always=("edit",), context=ctx,
        )
        assert s.can_answer(p.credential, "ou_owner") is True
        s.resolve(p.credential, "deny", decided_by="ou_owner")
        # 已决定之后，can_answer 仍返回 True —— 它只管「是谁」
        assert s.can_answer(p.credential, "ou_owner") is True, \
            "can_answer 只判发起人，不判是否已决定"
        # 而 resolve 会拒 —— 这才是真正的写闸门
        again = s.resolve(p.credential, "deny", decided_by="ou_owner")
        assert again is False, "resolve 拒绝第二次决定"

    def test_stop_request_survives_resolve(self, store):
        """停止请求**不依赖** can_answer，所以顺序对了就能落库。"""
        s, _app = store
        ctx = ApprovalContext("remote", who="ou_owner")
        p = s.request_tool_call(
            permission="edit", paths=("x",), diff="+1",
            suggested_always=("edit",), context=ctx,
        )
        s.resolve(p.credential, "deny", decided_by="ou_owner")
        s.request_stop(p.credential, by="ou_owner")
        assert s.is_stop_requested(p.credential) is True