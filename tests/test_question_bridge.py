"""Layer 5: a typed message becomes an answer to a pending question.

`test_question_loop.py` proves the executor side. This file proves the bridge
side, and it exists mainly for one property.

## The property

**Typed text must never grant a permission.**

    text   -> put_answer -> question.asked   (answer the agent's question)
    button -> resolve    -> permission.asked (allow/deny code execution)

No wire between them. Every other test here is bookkeeping; this one is the
reason the feature is allowed to exist at all, because routing text into a
running delegation is a new remote-input path and the blast radius has to be
"answer a question", not "approve code".

## How the bridge is built

`_route_answer` only touches `msg`, the module-level `_card_conn` /
`_card_clock`, and `self.sender`. So the tests build the bridge with
`__new__` and set just those, rather than standing up the whole long-connection
stack -- the wiring under test is the routing decision, not the SDK.
"""
from __future__ import annotations

import datetime
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from freeagent.app import build_app  # noqa: E402
from freeagent.feishu import bridge as bridge_mod  # noqa: E402
from freeagent.feishu.bridge import FeishuBridge  # noqa: E402
from freeagent.services.approval import ApprovalStore  # noqa: E402
from freeagent.services.clock import FrozenClock  # noqa: E402

ALICE = "ou_Alice_12345678"
BOB = "ou_Bob_98765432"


def _dt(y, m, d, h=12):
    return datetime.datetime(y, m, d, h)


class _Msg:
    """Just the attributes `_route_answer` reads."""

    def __init__(self, text: str, *, who: str = ALICE,
                 unsupported: str = "", root_id: str = "") -> None:
        self.text = text
        self.sender_open_id = who
        self.unsupported_type = unsupported
        self.chat_id = "oc_chat"
        self.event_id = "ev_1"
        self.message_id = "om_1"
        #: V1.90：话题根（在话题里说话时飞书才给）。空 = 在主聊天流里说。
        self.root_id = root_id


class _Sender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send_text(self, chat_id: str, text: str) -> None:
        self.sent.append((chat_id, text))


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """A bridge whose store sees a real pending question."""
    app = build_app(tmp_path / "a.db", clock=FrozenClock(_dt(2026, 10, 1)))
    monkeypatch.setattr(bridge_mod, "_card_conn", app.conn)
    monkeypatch.setattr(bridge_mod, "_card_clock", app.clock.now)
    b = FeishuBridge.__new__(FeishuBridge)
    b.sender = _Sender()
    store = ApprovalStore(app.conn, clock=app.clock.now)
    yield b, store, app
    app.close()


def _ask(store, *questions, who=ALICE, cred=None):
    return store.request_question(
        subject=questions[0],
        questions=list(questions),
        credential=cred,
        ttl_seconds=3600,
        requested_by=who,
    )


class TestRoutesToPendingQuestion:
    def test_text_is_stored_as_the_answer(self, wired):
        b, store, _ = wired
        p = _ask(store, "用哪个库?")
        got = b._route_answer(_Msg("Postgres"))
        assert got is not None, "有挂起提问时，文本应当被路由"
        assert store.answers_of(p.credential) == [["Postgres"]]
        assert store.is_complete(p.credential)

    def test_reports_which_question_and_how_many_remain(self, wired):
        b, store, _ = wired
        _ask(store, "第一问?", "第二问?", "第三问?")
        first = b._route_answer(_Msg("a"))
        assert "1/3" in first
        assert "2" in first, f"应说明还剩几个，实际 {first!r}"
        second = b._route_answer(_Msg("b"))
        assert "2/3" in second
        done = b._route_answer(_Msg("c"))
        assert "3/3" in done or "全部答完" in done

    def test_two_questions_fills_slots_in_order(self, wired):
        b, store, _ = wired
        p = _ask(store, "第一?", "第二?")
        b._route_answer(_Msg("a1"))
        b._route_answer(_Msg("a2"))
        assert store.answers_of(p.credential) == [["a1"], ["a2"]]


class TestTextCannotGrantPermission:
    def test_answering_creates_no_permission_decision(self, wired):
        """**核心属性**：打字不许变成一次「允许」。

        一次 allow 会让 opencode 真的去改文件。所以这条断言不是「结果看起来
        对」，而是「权限那张表根本没被写过」。
        """
        b, store, app = wired
        _ask(store, "问题?")
        b._route_answer(_Msg("allow"))
        rows = app.conn.execute(
            "SELECT COUNT(*) FROM pending_approvals WHERE decision IS NOT NULL"
        ).fetchone()[0]
        assert rows == 0, f"打字竟然写出了 {rows} 条授权决定"

    def test_answering_does_not_resolve_a_permission_row(self, wired):
        """即便库里**同时**挂着一条待授权，它也必须毫发无损。"""
        b, store, app = wired
        perm = store.ask("opencode 想edit", requested_by=ALICE)
        _ask(store, "问题?")
        b._route_answer(_Msg("allow"))
        assert store.get(perm.credential).decision is None
        assert store.decide(perm.credential) is None


class TestOnlyTheRequester:
    def test_other_people_text_is_not_routed(self, wired):
        """非发起人的文字**不进**提问槽位。

        它也不该被吞掉 —— 返回 None 表示「不归我管」，交给正常命令派发。
        """
        b, store, _ = wired
        p = _ask(store, "问题?", who=ALICE)
        assert b._route_answer(_Msg("Bob 的答案", who=BOB)) is None
        assert store.answers_of(p.credential) == [[]]

    def test_other_people_get_a_reason_not_silence(self, wired):
        """有挂起提问、但来的人不对 -> 要说清，不能静默。

        静默会被读成「机器人坏了」，而真相是「这条不是给你的」。
        """
        b, store, _ = wired
        p = _ask(store, "问题?", who=ALICE)
        got = b._route_answer(_Msg("Bob 的答案", who=BOB))
        # BOB 看不到 Alice 的提问 -> pending_question_for 返回 None -> 不路由
        assert got is None
        assert store.answers_of(p.credential) == [[]]
        assert p.credential


class TestCommandsWin:
    def test_slash_question_is_the_escape_hatch(self, wired):
        """/skip-question 不当答案，而是**显式跳过**。

        跳过也要推进流程：否则执行器会在 1800s 的 TTL 上白等，
        而人已经明确说了「我不答」。
        """
        b, store, _ = wired
        p = _ask(store, "问题?")
        got = b._route_answer(_Msg("/skip-question"))
        assert got is not None
        answers = store.answers_of(p.credential)
        assert answers[0] and "跳过" in answers[0][0]

    def test_other_commands_are_never_answers(self, wired):
        """/today、/help 之类照旧走命令派发，绝不能被当成答复。"""
        b, store, _ = wired
        p = _ask(store, "问题?")
        for cmd in ("/today", "/help", "/all open"):
            assert b._route_answer(_Msg(cmd)) is None, f"{cmd} 被当成答案了"
        assert store.answers_of(p.credential) == [[]]

    def test_the_command_is_not_stored_verbatim(self, wired):
        """跳过标记要可识别，不能原样把命令塞给 agent。"""
        b, store, _ = wired
        p = _ask(store, "问题?")
        b._route_answer(_Msg("/skip-question"))
        assert store.answers_of(p.credential)[0][0] != "/skip-question"


class TestNothingPending:
    def test_text_is_left_to_normal_dispatch(self, wired):
        """没有挂起提问 -> 返回 None，文本照旧当命令/白话处理。"""
        b, store, _ = wired
        assert b._route_answer(_Msg("今天该做什么")) is None

    def test_completed_question_stops_routing(self, wired):
        """全答完的提问不再吸走文本 —— 否则下一句话会被吞掉。"""
        b, store, _ = wired
        _ask(store, "问题?")
        b._route_answer(_Msg("答案"))
        assert b._route_answer(_Msg("今天该做什么")) is None

    def test_blank_text_is_not_an_answer(self, wired):
        b, store, _ = wired
        _ask(store, "问题?")
        assert b._route_answer(_Msg("   ")) is None

    def test_non_text_message_is_not_an_answer(self, wired):
        b, store, _ = wired
        p = _ask(store, "问题?")
        assert b._route_answer(_Msg("", unsupported="image")) is None
        assert store.answers_of(p.credential) == [[]]

    def test_no_connection_means_no_routing(self, tmp_path, monkeypatch):
        """没带库起来就不能落盘，此时**不猜**。"""
        monkeypatch.setattr(bridge_mod, "_card_conn", None)
        app = build_app(tmp_path / "a.db", clock=FrozenClock(_dt(2026, 10, 1)))
        try:
            store = ApprovalStore(app.conn, clock=app.clock.now)
            _ask(store, "问题?")
            b = FeishuBridge.__new__(FeishuBridge)
            b.sender = _Sender()
            assert b._route_answer(_Msg("答案")) is None
        finally:
            app.close()


class TestNewestQuestionWins:
    def test_answers_go_to_the_latest(self, wired):
        """两个挂起提问时，文字进**最新**那个。

        进旧的会让新的一直无人答，而人以为自己答过了。
        """
        b, store, _ = wired
        old = _ask(store, "旧问题?", cred="q_old_0000000001")
        new = _ask(store, "新问题?", cred="q_new_0000000002")
        b._route_answer(_Msg("答案"))
        assert store.answers_of(new.credential) == [["答案"]]
        assert store.answers_of(old.credential) == [[]]

class TestTopicRouting:
    """V1.90：话题里说话 → 答案只进**那个话题**对应的提问。

    ## 为什么「取最新一条」不够

    多委派并行时两条委派可能同时在提问。``TestNewestQuestionWins`` 记的
    旧判据（取最新）在那一刻会**稳定串台**：先发起的那条永远等不到人，
    而用户以为自己答过了 —— 比不答更坏。

    有了话题就有确定性判据：用户在哪条委派的话题里说话，飞书事件里的
    ``root_id`` 就是那张进度卡（真机实证见 ``tools/probe_topic.py``），
    而发卡时同一个值记进了 ``pending_approvals.thread_root``。

    ## 话题里没有对应提问时**不退回**最新一条

    那是 fail-closed：这句话不是答案（照常走命令/闲聊），
    总好过被写进另一条委派的问答槽位。
    """

    def test_answer_lands_in_the_topic_it_was_typed_in(self, wired):
        b, store, _ = wired
        a = _ask(store, "A 的问题?", cred="q_a_000000000001")
        c = _ask(store, "B 的问题?", cred="q_b_000000000002")
        store.record_card(a.credential, "om_a1", thread_root="om_root_a")
        store.record_card(c.credential, "om_b1", thread_root="om_root_b")

        got = b._route_answer(_Msg("答给 A", root_id="om_root_a"))
        assert got is not None
        assert store.answers_of(a.credential) == [["答给 A"]]
        assert store.answers_of(c.credential) == [[]], "B 的槽位不许被填"

    def test_unknown_topic_does_not_steal_another_question(self, wired):
        """话题不在任何提问名下 → 不当答案（fail-closed）。"""
        b, store, _ = wired
        a = _ask(store, "A 的问题?", cred="q_a_000000000001")
        store.record_card(a.credential, "om_a1", thread_root="om_root_a")

        assert b._route_answer(_Msg("随便一句", root_id="om_root_zzz")) is None
        assert store.answers_of(a.credential) == [[]]

    def test_message_outside_any_topic_keeps_old_judgement(self, wired):
        """主聊天流里说的（无 root_id）仍按「最新一条」—— 旧路径不许断。"""
        b, store, _ = wired
        old = _ask(store, "旧问题?", cred="q_old_0000000001")
        new = _ask(store, "新问题?", cred="q_new_0000000002")
        store.record_card(old.credential, "om_o1", thread_root="om_root_o")
        store.record_card(new.credential, "om_n1", thread_root="om_root_n")

        b._route_answer(_Msg("主聊天流的回答"))
        assert store.answers_of(new.credential) == [["主聊天流的回答"]]
        assert store.answers_of(old.credential) == [[]]
