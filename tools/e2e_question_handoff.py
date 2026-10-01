# -*- coding: utf-8 -*-
"""End-to-end: does the two-process question contract hold?

The unit tests split this in half -- the loop knows nothing about the bridge,
the bridge knows nothing about the loop. That split is right for unit tests but
it hides the one thing that matters: **the handoff**.

So every scenario here uses **two separate SQLite connections to the same
file**, which is what "two processes" actually means. No shared memory, no
shared objects -- only the database, exactly like production.

    executor process                        bridge process
    --------------                          --------------
    request_question()   --row-->
    send question card
    wait_answer(0)       <--put_answer()--
    send card 2
    wait_answer(1)       <--put_answer()--
    reply_question([[a1],[a2]])

If any arrow is wrong the executor blocks, so a pass is real evidence rather
than a restatement of the unit tests.

## Isolation is per-database, not per-thread

An earlier version shared one database across scenarios and cleaned up by
joining threads. That leaked: `pending_question_for` returns the **newest**
unfinished question, so a still-running executor from a previous scenario
stole the next scenario's answers. Threads are a weak isolation boundary;
separate database files are not.

## What this script has already caught

`/skip-question` filled only one slot, so with a two-question prompt the
executor stayed blocked in `wait_answer(1)` while the card promised a way out.
The single-question unit test passed throughout. That is why this exists.

Run: ``python tools/e2e_question_handoff.py``
"""
from __future__ import annotations

import datetime
import pathlib
import sqlite3
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from freeagent.app import build_app
from freeagent.delegate import run_with_tool_gate
from freeagent.feishu import bridge as bridge_mod
from freeagent.feishu.bridge import FeishuBridge
from freeagent.services.approval import ApprovalStore
from freeagent.services.clock import FrozenClock
from freeagent.services.delegate import DelegationPolicy

ALICE = "ou_Alice_12345678"
BOB = "ou_Bob_98765432"
Q1, Q2 = "用哪个数据库?", "要不要开缓存?"
DEADLINE = 25.0
NOW = datetime.datetime(2026, 10, 1, 12, 0)

fails: list[str] = []


def check(label: str, cond: bool, extra: str = "") -> bool:
    print(f"  {'ok ' if cond else '!! '} {label}" + (f"  [{extra}]" if extra else ""))
    if not cond:
        fails.append(label)
    return cond


class FakeServer:
    def __init__(self, *questions):
        self.replies: list[tuple[str, object]] = []
        self._events = [
            ("question.asked", {"id": "que_1", "sessionID": "ses_fake",
                                "questions": list(questions),
                                "options": ["Postgres", "SQLite"]}),
            ("session.idle", {}),
        ]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def create_session(self):
        return "ses_fake"

    def prompt_async(self, sid, brief, *, model="", directory=None):
        pass

    def events(self, **kw):
        yield from self._events

    def reply_question(self, request_id, answers, *, directory=None):
        self.replies.append((request_id, answers))

    def reply_permission(self, request_id, decision, *, directory=None):
        raise AssertionError("提问路径不该发权限答复")

    def abort(self, session_id):
        raise AssertionError("不该 abort")


class CardSink:
    def __init__(self):
        self.cards: list[dict] = []

    def send_question_card(self, *, open_id, subject, detail, credential,
                           ttl_seconds) -> str:
        self.cards.append({"open_id": open_id, "subject": subject,
                           "detail": detail, "credential": credential})
        return f"om_card_{len(self.cards)}"

    def send_tool_card(self, **kw) -> str:
        raise AssertionError("提问路径不该发授权卡")


class Msg:
    def __init__(self, text, who=ALICE):
        self.text = text
        self.sender_open_id = who
        self.unsupported_type = ""
        self.chat_id = "oc_chat"
        self.event_id = f"ev_{abs(hash(text)) % 9999}"
        self.message_id = f"om_{abs(hash(text)) % 9999}"


class World:
    """One scenario's world: its own DB, two connections, one thread."""

    def __init__(self, name: str) -> None:
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="fa-e2e-"))
        self.db = self.dir / f"{name}.db"
        self.app = build_app(self.db, clock=FrozenClock(NOW))
        # the executor's store, on the app's connection
        self.store = ApprovalStore(self.app.conn, clock=lambda: NOW)
        # the bridge's own connection -> a different process, in effect
        self.bconn = sqlite3.connect(self.db, timeout=5)
        self.bconn.row_factory = sqlite3.Row
        self.bridge = FeishuBridge.__new__(FeishuBridge)
        self.bridge.sender = object()      # _route_answer does not send
        self._saved = (bridge_mod._card_conn, bridge_mod._card_clock)
        bridge_mod._card_conn = self.bconn
        bridge_mod._card_clock = lambda: NOW
        self.sink = CardSink()
        self.server = FakeServer(Q1, Q2)
        self.result: list = []
        self._th: threading.Thread | None = None

    def start(self) -> None:
        def run() -> None:
            self.result.append(run_with_tool_gate(
                object(), self.dir, "干活",
                policy=DelegationPolicy(), store=self.store, sender=self.sink,
                approver=ALICE, server_factory=lambda *a, **k: self.server,
            ))

        self._th = threading.Thread(target=run, daemon=True)
        self._th.start()

    def say(self, text: str, who: str = ALICE):
        return self.bridge._route_answer(Msg(text, who))

    def await_question(self, timeout: float = 8.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = self.store.pending_question_for(ALICE)
            if found is not None:
                return found
            time.sleep(0.05)
        return None

    def join(self, timeout: float = DEADLINE) -> bool:
        assert self._th is not None
        self._th.join(timeout=timeout)
        return not self._th.is_alive()

    def close(self) -> None:
        # Release the executor **before** closing the database. Scenario 1
        # deliberately leaves its question unanswered, and tearing the DB down
        # under a thread that is still in `wait_answer` produces a
        # "closed database" traceback -- harness noise that could hide a real
        # failure in the same run.
        if self.store.pending_question_for(ALICE) is not None:
            self.say("/skip-question")
            if self._th is not None:
                self._th.join(timeout=3)
        if self._th is not None:
            self._th.join(timeout=2)
        bridge_mod._card_conn, bridge_mod._card_clock = self._saved
        self.app.close()
        self.bconn.close()


# ── scenario 1: the row is visible across the boundary ────────────────────
def scenario_row() -> None:
    print("\n① 执行器登记的提问，桥接用**另一个连接**看得到")
    w = World("row")
    try:
        w.start()
        pend = w.await_question()
        if not check("库里有 question 行", pend is not None):
            return
        row = w.bconn.execute(
            "SELECT requested_by, question_spec FROM pending_approvals"
            " WHERE kind='question'").fetchone()
        check("记的是发起人 Alice", row["requested_by"] == ALICE, row["requested_by"])
        spec = row["question_spec"].replace(" ", "")
        check("两问都存了", f'["{Q1}","{Q2}"]' in spec, spec[:76])
        check("答题槽位是空的", '"answers":[[],[]]' in spec, spec[-30:])
        check("命令不被当成答案", w.say("/today") is None)
    finally:
        w.close()


# ── scenario 2: the bug this script was written to find ──────────────────
def scenario_skip() -> None:
    print("\n② /skip-question 必须**真的放人走**（不是只填一格）")
    w = World("skip")
    try:
        w.start()
        pend = w.await_question()
        if not check("提问已登记", pend is not None):
            return
        said = w.say("/skip-question")
        check("/skip-question 被识别", said is not None, repr(said))
        released = w.join()
        check("执行器被释放（没卡在第 2 个空槽）", released)
        check("跳过也让 opencode 收到了答复（一次）", len(w.server.replies) == 1,
              f"{len(w.server.replies)} 次")
        if w.server.replies:
            _rid, answers = w.server.replies[0]
            check("两个槽都记为跳过",
                  answers == [["跳过标记"]] * 2 or
                  all(a and "跳过" in a[0] for a in answers), repr(answers))
        check("回执说「跳过」而非「全部答完」",
              bool(said) and "跳过" in said and "全部答完" not in said, repr(said))
        check("跳过后问题不再挂着", w.store.pending_question_for(ALICE) is None)
        check("跳过后是成功（不是失败）",
              bool(w.result) and w.result[0].ok is True,
              w.result[0].summary if w.result else "no result")
    finally:
        w.close()


# ── scenario 3: the real round trip ──────────────────────────────────────
def scenario_answers() -> None:
    print("\n③ 别人的文字不进槽位（只有发起人能答）")
    w = World("answers")
    try:
        w.start()
        pend = w.await_question()
        if not check("提问已登记", pend is not None):
            return
        check("Bob 的文字不被路由", w.say("Bob 插一句", who=BOB) is None)
        check("槽位仍空", w.store.answers_of(pend.credential) == [[], []])

        print("\n④ 发起人逐问回答 -> 一次 POST 送回嵌套数组")
        r1 = w.say("Postgres")
        check("第 1 问收下", r1 is not None and "1/2" in r1, repr(r1))
        check("第 1 槽已填、第 2 槽仍空",
              w.store.answers_of(pend.credential) == [["Postgres"], []])
        r2 = w.say("要开")
        check("第 2 问收下", r2 is not None, repr(r2))
        check("全部答完", w.store.is_complete(pend.credential))

        check("执行器没挂住", w.join())
        check("回 opencode 恰好一次", len(w.server.replies) == 1,
              f"{len(w.server.replies)} 次")
        if w.server.replies:
            rid, answers = w.server.replies[0]
            check("载荷是 [[a1],[a2]]", answers == [["Postgres"], ["要开"]],
                  repr(answers))
        check("结果是成功", bool(w.result) and w.result[0].ok is True,
              w.result[0].summary if w.result else "no result")

        print("\n⑤ 问完之后文本恢复正常")
        check("普通文本不再被吸走", w.say("今天该做什么") is None)
        check("命令也照旧", w.say("/help") is None)

        print("\n⑥ 无按钮提问卡，收件人是发起人")
        check("发了两张卡（一次问一个）", len(w.sink.cards) == 2,
              f"{len(w.sink.cards)} 张")
        check("收件人都是 Alice", all(c["open_id"] == ALICE for c in w.sink.cards))
        from freeagent.feishu.sender import _question_card
        check("卡里没有 action 元素",
              "action" not in [e.get("tag")
                               for e in _question_card("t", "d", 60)["elements"]])
    finally:
        w.close()


print("=== 跨进程提问闭环（每场景独立库）===")
for fn in (scenario_row, scenario_skip, scenario_answers):
    try:
        fn()
    except AssertionError as exc:
        fails.append(f"{fn.__name__}: {exc}")
        print(f"  !! {exc}")

print("\n" + "=" * 52)
print(f"FAILURES: {len(fails)}")
for f in fails:
    print(f"  - {f}")
raise SystemExit(1 if fails else 0)