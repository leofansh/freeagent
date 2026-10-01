"""Layers 3+4: the executor loop answers a question, end to end.

Covers the loop in `run_with_tool_gate` and the buttonless question card in
`feishu/sender.py`.

## Why the closed loop matters more than the units

The dangerous failure for this feature is a **half-wired loop**: events
parsed, card sent, nothing ever delivered to opencode. Static tests would
still be green. So the central test here drives the real loop against a fake
server and a real `ApprovalStore`, and asserts on what opencode *received*.

## The property worth defending

**A typed message must never grant a permission.** So:

- text -> `put_answer` (questions) -- the only text path
- buttons -> `resolve` (allow/deny) -- the only grant path

There is deliberately no route from one to the other. `test_typed_text_cannot_grant`
is the test that would fail first if someone wired it up carelessly.
"""
from __future__ import annotations

import datetime
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from freeagent.app import build_app  # noqa: E402
from freeagent.delegate import run_with_tool_gate  # noqa: E402
from freeagent.feishu.sender import _question_card  # noqa: E402
from freeagent.services.approval import ApprovalPolicy, ApprovalStore  # noqa: E402
from freeagent.services.clock import FrozenClock  # noqa: E402
from freeagent.services.delegate import DelegationPolicy  # noqa: E402


@pytest.fixture(autouse=True)
def _short_ttl(monkeypatch):
    """把「remote」档 TTL 压到 2 秒。

    这个 TTL 真实值是 1800 秒，所以任何**意外的**等待都会把整个测试套件
    挂在 30 分钟上 —— 我已经为此付了两次 10 分钟。

    压短之后，一个本该立刻返回的等待会在 2 秒内失败并给出断言，而不是让
    人在超时里猜发生了什么。**测试环境该让失败快速且可读。**
    """
    real = ApprovalPolicy.for_context

    def patched(context: str) -> ApprovalPolicy:
        got = real(context)
        if got.ttl_seconds > 2:
            return ApprovalPolicy(got.name, 2, may_run_unattended=False)
        return got

    monkeypatch.setattr(ApprovalPolicy, "for_context", staticmethod(patched))


def _dt(y, m, d, h=12):
    return datetime.datetime(y, m, d, h)


class _FakeServer:
    """Records what opencode was actually told."""

    def __init__(self, events) -> None:
        self._events = list(events)
        self.question_replies: list[tuple[str, Any]] = []
        self.permission_replies: list[tuple[str, str]] = []
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
        self.permission_replies.append((request_id, decision))

    def reply_question(self, request_id, answers, *, directory=None) -> None:
        self.question_replies.append((request_id, answers))

    def abort(self, session_id) -> None:
        self.aborted.append(session_id)


class _AnsweringSender:
    """Sends the card, then answers the question the way a human would.

    One answer per card, so a two-question event exercises the round loop.

    **Runs out of answers -> raise.** The "remote" TTL is 1800s, so a
    mismatch between the number of answers and the number of questions used
    to hang the suite for 10 minutes before anyone learned anything. Failing
    immediately turns that into a readable assertion.
    """

    def __init__(self, store: ApprovalStore, *, answers: list[str]) -> None:
        self.store = store
        self.answers = list(answers)
        self.cards: list[dict[str, Any]] = []

    def send_question_card(self, *, open_id, subject, detail, credential,
                           ttl_seconds) -> str:
        self.cards.append({"open_id": open_id, "subject": subject,
                           "detail": detail, "credential": credential,
                           "ttl": ttl_seconds})
        if not self.answers:
            raise AssertionError(
                "答复已用尽，但执行器还在发卡 —— 问的次数与给的答复数不匹配"
            )
        self.store.put_answer(credential, self.answers.pop(0),
                              answered_by=open_id)
        return "om_q"

    # permission cards -- must NOT be used by the question path
    def send_tool_card(self, *, open_id, subject, detail, credential,
                       ttl_seconds) -> str:
        raise AssertionError("提问路径不该发授权卡")


class _NoSender:
    def send_question_card(self, **kw) -> str:
        raise AssertionError("不该走到发卡")


def _asked(*questions, rid="que_1", options=None):
    props: dict[str, Any] = {"id": rid, "sessionID": "ses_fake",
                             "questions": list(questions)}
    if options is not None:
        props["options"] = options
    return ("question.asked", props)


def _app(tmp_path):
    return build_app(tmp_path / "a.db", clock=FrozenClock(_dt(2026, 10, 1)))


def _store(app):
    return ApprovalStore(app.conn, clock=app.clock.now)


class TestSingleQuestion:
    def test_one_answer_reaches_opencode(self, tmp_path):
        app = _app(tmp_path)
        try:
            oc = _FakeServer([_asked("用哪个库?", rid="que_1"),
                              ("session.idle", {})])
            sender = _AnsweringSender(_store(app), answers=["Postgres"])
            got = run_with_tool_gate(
                object(), Path(str(tmp_path)), "干活",
                policy=DelegationPolicy(), store=_store(app),
                sender=sender, approver="ou_Alice",
                server_factory=lambda *a, **k: oc,
            )
            assert oc.question_replies == [("que_1", [["Postgres"]])]
            assert got.ok is True
        finally:
            app.close()

    def test_card_goes_to_the_requester(self, tmp_path):
        app = _app(tmp_path)
        try:
            oc = _FakeServer([_asked("问题"), ("session.idle", {})])
            sender = _AnsweringSender(_store(app), answers=["答案"])
            run_with_tool_gate(
                object(), Path(str(tmp_path)), "干活",
                policy=DelegationPolicy(), store=_store(app),
                sender=sender, approver="ou_Alice",
                server_factory=lambda *a, **k: oc,
            )
            assert sender.cards[0]["open_id"] == "ou_Alice"
        finally:
            app.close()

    def test_card_has_no_buttons(self, tmp_path):
        """**核心属性**：提问卡上不能有任何 action 元素。

        有按钮就意味着「点一下」，而提问没有「批准答复」这个动词 ——
        挂一个按钮在那里只会让人以为点了有用。
        """
        app = _app(tmp_path)
        try:
            oc = _FakeServer([_asked("问题"), ("session.idle", {})])
            sender = _AnsweringSender(_store(app), answers=["答案"])
            run_with_tool_gate(
                object(), Path(str(tmp_path)), "干活",
                policy=DelegationPolicy(), store=_store(app),
                sender=sender, approver="ou_Alice",
                server_factory=lambda *a, **k: oc,
            )
            from freeagent.feishu import sender as sender_mod
            card = sender_mod._question_card("t", "d", 600)
            tags = [e.get("tag") for e in card["elements"]]
            assert "action" not in tags, f"提问卡不该有按钮，实际 {tags}"
        finally:
            app.close()


class TestTwoQuestionsAskOneAtATime:
    def test_rounds_accumulate_into_nested_array(self, tmp_path):
        """两问两答 -> 一次 POST，载荷是 [[a1], [a2]]。

        不是两次 POST（agent 只等一次），也不是扁平 ["a1", "a2"]。
        """
        app = _app(tmp_path)
        try:
            oc = _FakeServer([_asked("第一问?", "第二问?"),
                              ("session.idle", {})])
            sender = _AnsweringSender(_store(app), answers=["a1", "a2"])
            got = run_with_tool_gate(
                object(), Path(str(tmp_path)), "干活",
                policy=DelegationPolicy(), store=_store(app),
                sender=sender, approver="ou_Alice",
                server_factory=lambda *a, **k: oc,
            )
            assert len(oc.question_replies) == 1, "两问必须攒成一次答复"
            rid, answers = oc.question_replies[0]
            assert answers == [["a1"], ["a2"]]
            assert len(sender.cards) == 2, "每个问题各发一张卡（一次问一个）"
            assert got.ok is True
        finally:
            app.close()

    def test_card_body_names_the_remaining_questions(self, tmp_path):
        """第 1 张卡要让人知道后面还有几问 —— 否则他不知道要答几次。"""
        app = _app(tmp_path)
        try:
            oc = _FakeServer([_asked("第一问?", "第二问?", "第三问?"),
                              ("session.idle", {})])
            sender = _AnsweringSender(_store(app), answers=["a", "b", "c"])
            run_with_tool_gate(
                object(), Path(str(tmp_path)), "干活",
                policy=DelegationPolicy(), store=_store(app),
                sender=sender, approver="ou_Alice",
                server_factory=lambda *a, **k: oc,
            )
            first = sender.cards[0]["detail"]
            assert "第二问?" in first and "第三问?" in first
        finally:
            app.close()


class TestUnanswered:
    def test_timeout_fails_the_dispatch(self, tmp_path):
        """**核心判断**：无人作答 -> 报**失败**，不是报完成。

        一次「无人回答却记录成完成」的委派，在记录里和正常完成长得一样。
        """
        app = _app(tmp_path)
        try:
            oc = _FakeServer([_asked("问题?"), ("session.idle", {})])
            got = run_with_tool_gate(
                object(), Path(str(tmp_path)), "干活",
                policy=DelegationPolicy(), store=_store(app),
                sender=_NoSender(), approver="ou_Alice",
                server_factory=lambda *a, **k: oc,
            )
            assert got.ok is False
        finally:
            app.close()

    def test_no_empty_answer_is_ever_sent(self, tmp_path):
        """绝不发空答复。空数组是**另一句话**：「人没话说」—— 而事实不是。"""
        app = _app(tmp_path)
        try:
            oc = _FakeServer([_asked("问题?"), ("session.idle", {})])
            _ = run_with_tool_gate(
                object(), Path(str(tmp_path)), "干活",
                policy=DelegationPolicy(), store=_store(app),
                sender=_NoSender(), approver="ou_Alice",
                server_factory=lambda *a, **k: oc,
            )
            assert oc.question_replies == [], "超时不许发任何答复"
        finally:
            app.close()

    def test_card_failure_does_not_self_answer(self, tmp_path):
        """**核心属性**：发卡失败时**绝不**自己代答。

        那等于伪造一份人的答案去驱动 agent 干活 —— 而且会静默成功。
        """
        app = _app(tmp_path)
        try:
            oc = _FakeServer([_asked("问题?"), ("session.idle", {})])
            class _Boom:
                def send_question_card(self, **kw):
                    raise RuntimeError("飞书挂了")
            got = run_with_tool_gate(
                object(), Path(str(tmp_path)), "干活",
                policy=DelegationPolicy(), store=_store(app),
                sender=_Boom(), approver="ou_Alice",
                server_factory=lambda *a, **k: oc,
            )
            assert oc.question_replies == []
            assert got.ok is False
        finally:
            app.close()


class TestTextCannotGrantPermission:
    def test_typed_text_cannot_grant(self, tmp_path):
        """**这条属性是整个特性的安全地基**。

        文本只走 put_answer。allow/deny 只由按钮走 resolve。
        两者之间**没有**任何连线，且这条测试在有人误连上时会第一个红。
        """
        app = _app(tmp_path)
        try:
            store = _store(app)
            oc = _FakeServer([_asked("问题?"), ("session.idle", {})])
            sender = _AnsweringSender(store, answers=["这是我的答案"])
            run_with_tool_gate(
                object(), Path(str(tmp_path)), "干活",
                policy=DelegationPolicy(), store=store,
                sender=sender, approver="ou_Alice",
                server_factory=lambda *a, **k: oc,
            )
            # 一次权限批准都没发生
            assert oc.permission_replies == []
            for _rid, answers in oc.question_replies:
                for row in answers:
                    assert row != ["allow"] and row != ["deny"]
        finally:
            app.close()

    def test_unrecognised_question_event_is_logged_not_answered(self, tmp_path):
        """认不出形状时：不发卡、不答复、不谎称已答。"""
        app = _app(tmp_path)
        try:
            oc = _FakeServer([("question.asked", {"id": "q", "questions": [{"x": 1}]}),
                              ("session.idle", {})])
            got = run_with_tool_gate(
                object(), Path(str(tmp_path)), "干活",
                policy=DelegationPolicy(), store=_store(app),
                sender=_NoSender(), approver="ou_Alice",
                server_factory=lambda *a, **k: oc,
            )
            assert oc.question_replies == []
            assert got.ok is True      # 没发生任何事，不算失败
        finally:
            app.close()


class TestQuestionCardShape:
    def test_is_json_1_0(self):
        """必须是 JSON 1.0 —— 与另外两张卡同版本（官方码 200830）。"""
        card = _question_card("t", "d", 600)
        assert set(card) >= {"config", "header", "elements"}

    def test_states_the_escape_hatch(self):
        """卡上必须写「可以不答」。

        只说「回答这个」而没说「不回答也行」就是一条死路：agent 干等、
        TTL 走完，而人根本不知道自己本可以拒绝。
        """
        note = " ".join(
            str(e.get("content", "")) for e in _question_card("t", "d", 600)["elements"]
            if e.get("tag") == "note" for e in e.get("elements", [])
        )
        assert "skip-question" in note

    def test_shows_ttl(self):
        """不写时限，人会以为不限时。"""
        card = _question_card("t", "d", 1234)
        blob = str(card)
        assert "1234" in blob

    def test_options_are_not_called_this_questions_options(self):
        """``options`` 的粒度没验过，所以只能叫「可选项」。

        写成「这题的选项」是一个**靠猜**的声称：若它实际是按题分组的，
        第 1 张卡列出的就是第 2 题的选项。
        """
        from freeagent.services.executors import ToolQuestion
        from freeagent.delegate import _question_detail
        detail = _question_detail(
            ToolQuestion(request_id="q", questions=("Q1",), options=("A", "B")),
            0, ttl_seconds=600,
        )
        assert "可选项" in detail
        assert "这题的选项" not in detail
        assert "本题的选项" not in detail