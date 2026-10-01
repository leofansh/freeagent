"""「待答问题」落库、逐轮积累与跨进程往返的守卫。

## 方案：一次问一个、逐轮积累

`answers` 的线格式是 ``string[][]``（三份外部项目 + 我今天的 spec 实测
四个独立来源一致）。一条飞书消息无法可靠对应到多个问题，所以：

    执行器发「问题 1/N」 -> 用户答 -> 执行器发「问题 2/N」 -> … ->
    攒齐 -> POST /question/{id}/reply {answers: [[a1], [a2]]}

N=1（实测到的常见情形）与 N>1 走**同一条路径**，只是轮数不同。

## 这一层锁住的判断

- 迁移：新列真的加上；重复跑不炸
- **向后兼容**：加列前的历史行没有 ``kind``，读出来必须是 ``approval``
- **空槽不补齐**：没答的那一问在载荷里是 ``[]`` 而不是 ``[""]`` ——
  补成空串会被当成「用户答了空的」，agent 就拿到一个空答案
- ``is_complete`` 与 ``text_answer`` 在 N>1 时的区别：前者才代表「全答完」
- 只有发起人能答，且**每轮都重验**（第 2 轮是另一条消息）
- 过期：既答不了、也等不到
- 空白答复**不落库**
- 跨连接（两个连接 = 本地能测的跨进程代理）：等待方被另一连接写入叫醒
- 授权路径**完全不受影响**
"""
from __future__ import annotations

import datetime
import threading

import pytest

from freeagent.services.approval import ApprovalStore, new_credential
from freeagent.storage.db import SCHEMA_VERSION, connect, init_schema, migrate


def _fresh(path):
    c = connect(path)
    init_schema(c)
    migrate(c)
    return c


@pytest.fixture
def conn(tmp_path):
    """**必须走生产那两步**：``init_schema`` 只建表（表定义里没有后来加的
    列），``migrate`` 才按版本补列。少调一步，测试就在一个「永远缺列」的
    库上跑 —— 那种红是自造的（第一版就这么栽过）。"""
    c = _fresh(tmp_path / "a.db")
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def store(conn):
    return ApprovalStore(conn)


class TestMigration:
    def test_version(self):
        assert SCHEMA_VERSION == 11

    def test_columns_exist(self, conn):
        cols = {r[1] for r in conn.execute("PRAGMA table_info(pending_approvals)")}
        assert {"kind", "answer_text", "question_spec"} <= cols

    def test_rerun_is_idempotent(self, tmp_path):
        """库已是 v11 但版本号被手工退回 10 —— 重复迁移不能炸。

        第一版我造的是「版本号说 9、但 8→9 没做」，那不真实，
        断言正确地告诉我 ``kind`` 永远不会出现。**测试自己撒谎时，
        断言先抓它。**
        """
        p = tmp_path / "b.db"
        c = _fresh(p)
        c.execute("PRAGMA user_version = 10")
        c.commit()
        c.close()
        c2 = connect(p)
        migrate(c2)
        cols = {r[1] for r in c2.execute("PRAGMA table_info(pending_approvals)")}
        assert "question_spec" in cols
        c2.close()

    def test_old_row_without_kind_reads_as_approval(self, conn):
        """加列前的历史行没有 ``kind``，读出来必须是 approval。

        反过来把空当 question，那些行会突然开始拦「打字」并期待文本
        答复 —— 而它们全是授权。
        """
        now = datetime.datetime.now().isoformat()
        cred = new_credential()
        conn.execute(
            "INSERT INTO pending_approvals"
            " (credential, subject, asked_at, expires_at) VALUES (?, ?, ?, ?)",
            (cred, "老行", now, now),
        )
        conn.commit()
        item = ApprovalStore(conn).get(cred)
        assert item is not None
        assert item.is_question is False

    def test_broken_spec_degrades_instead_of_raising(self, conn, store):
        """坏 JSON **不许抛**。

        这一列是我们自己写进去的，而一个写坏了的待答项不该把整条消息
        处理搞崩 —— 那等于让一条坏数据掀翻桥接。它退化成「没有问题」。
        """
        item = store.request_question("q", questions=["问题一"])
        conn.execute(
            "UPDATE pending_approvals SET question_spec = ? WHERE credential = ?",
            ("{不是合法 JSON", item.credential),
        )
        conn.commit()
        got = store.get(item.credential)
        assert got is not None
        assert got.spec == {"questions": [], "answers": []}
        assert got.next_question is None if hasattr(got, "next_question") else True


class TestRequestQuestion:
    def test_is_question(self, store):
        item = store.request_question("写到哪个文件？", questions=["文件名?"])
        assert item.is_question is True
        assert item.kind == "question"
        assert item.is_answered is False

    def test_questions_are_kept(self, store):
        item = store.request_question("q", questions=["第一个?", "第二个?"])
        assert store.next_question(item.credential) == "第一个?"
        assert store.is_complete(item.credential) is False

    def test_ask_is_still_an_approval(self, store):
        item = store.ask("允许改这个文件？")
        assert item.is_question is False
        assert store.next_question(item.credential) is None
        assert store.answers_of(item.credential) == []

    def test_credential_prefix_differs(self, store):
        """只是日志里一眼能分，**判定不靠前缀**。"""
        assert store.request_question("q").credential.startswith("q")
        assert store.ask("a").credential.startswith("ap")


class TestSingleQuestion:
    def test_round_trip(self, store):
        item = store.request_question("写到哪个文件？", requested_by="ou_a",
                                      questions=["文件名?"])
        assert store.put_answer(item.credential, "out.md",
                                answered_by="ou_a") == 0
        assert store.is_complete(item.credential) is True
        assert store.answers_of(item.credential) == [["out.md"]]
        assert store.text_answer(item.credential) == "out.md"

    def test_non_initiator_rejected(self, store):
        item = store.request_question("q", requested_by="ou_a",
                                      questions=["文件名?"])
        assert store.put_answer(item.credential, "out.md",
                                answered_by="ou_evil") is None
        assert store.is_complete(item.credential) is False

    def test_old_row_without_initiator_can_be_answered(self, store):
        item = store.request_question("q", questions=["q?"])
        assert store.put_answer(item.credential, "out.md",
                                answered_by="ou_anyone") == 0

    def test_whitespace_answer_is_not_stored(self, store):
        item = store.request_question("q", requested_by="ou_a",
                                      questions=["q?"])
        for blank in ("", "   ", "\n", "\t ", "\r\n", "　"):
            assert store.put_answer(item.credential, blank,
                                    answered_by="ou_a") is None, repr(blank)
        assert store.get(item.credential).answer_text is None
        assert store.is_complete(item.credential) is False
        # 还能正常答 —— 这是「不落库」的关键后果
        assert store.put_answer(item.credential, "out.md",
                                answered_by="ou_a") == 0

    def test_answered_by_recorded(self, store):
        item = store.request_question("q", requested_by="ou_a", questions=["q?"])
        store.put_answer(item.credential, "out.md", answered_by="ou_a")
        got = store.get(item.credential)
        assert got.decided_by == "ou_a"
        assert got.decided_at is not None

    def test_answering_an_approval_is_refused(self, store):
        item = store.ask("允许？", requested_by="ou_a")
        assert store.put_answer(item.credential, "out.md",
                                answered_by="ou_a") is None


class TestMultipleQuestions:
    """一次问一个、逐轮积累 —— N>1 与 N=1 走同一条路径。"""

    def _two(self, store):
        return store.request_question("两问", requested_by="ou_a",
                                      questions=["写到哪个文件?", "要几行?"])

    def test_fills_slots_in_order(self, store):
        item = self._two(store)
        assert store.put_answer(item.credential, "out.md", answered_by="ou_a") == 0
        assert store.next_question(item.credential) == "要几行?"
        assert store.is_complete(item.credential) is False
        assert store.put_answer(item.credential, "三行", answered_by="ou_a") == 1
        assert store.next_question(item.credential) is None
        assert store.is_complete(item.credential) is True

    def test_payload_is_nested_array(self, store):
        """最终载荷形状必须是 ``[[a1], [a2]]`` —— 与线格式一致。"""
        item = self._two(store)
        store.put_answer(item.credential, "out.md", answered_by="ou_a")
        store.put_answer(item.credential, "三行", answered_by="ou_a")
        assert store.answers_of(item.credential) == [["out.md"], ["三行"]]

    def test_unanswered_slot_is_not_padded(self, store):
        """没答的那一问在载荷里是 ``[]``，**不补成 ``[""]``**。

        补成空串会被当成「用户答了空的」而让 agent 拿到一个空答案 ——
        那是把「没答」说成「答了」。
        """
        item = self._two(store)
        store.put_answer(item.credential, "out.md", answered_by="ou_a")
        assert store.answers_of(item.credential) == [["out.md"], []]

    def test_third_answer_refused_when_only_two(self, store):
        item = self._two(store)
        store.put_answer(item.credential, "a", answered_by="ou_a")
        store.put_answer(item.credential, "b", answered_by="ou_a")
        assert store.put_answer(item.credential, "c", answered_by="ou_a") is None
        assert store.answers_of(item.credential) == [["a"], ["b"]]

    def test_every_round_rechecks_initiator(self, store):
        """第 2 轮是**另一条消息**，不能因为第 1 轮验过就放行。"""
        item = self._two(store)
        assert store.put_answer(item.credential, "a", answered_by="ou_a") == 0
        assert store.put_answer(item.credential, "b", answered_by="ou_evil") is None
        assert store.is_complete(item.credential) is False

    def test_text_answer_is_partial_but_is_complete_is_false(self, store):
        """``text_answer`` 已收到东西、``is_complete`` 却仍是 False ——
        两者在 N>1 时**必须分开**，否则调用方会误判「全答完」。"""
        item = self._two(store)
        store.put_answer(item.credential, "out.md", answered_by="ou_a")
        assert store.text_answer(item.credential) == "out.md"
        assert store.is_complete(item.credential) is False

    def test_flat_rendering_joins_answers(self, store):
        item = self._two(store)
        store.put_answer(item.credential, "out.md", answered_by="ou_a")
        store.put_answer(item.credential, "三行", answered_by="ou_a")
        assert store.text_answer(item.credential) == "out.md\n三行"


class TestExpiry:
    def _expired(self, store):
        return store.request_question("q", requested_by="ou_a",
                                      questions=["q?"], ttl_seconds=-1)

    def test_cannot_answer_after_expiry(self, store):
        item = self._expired(store)
        assert store.put_answer(item.credential, "out.md",
                                answered_by="ou_a") is None

    def test_is_expired_visible(self, store):
        assert self._expired(store).is_expired is True

    def test_wait_complete_gives_up_on_expiry(self, store):
        item = self._expired(store)
        assert store.wait_complete(item.credential, poll_seconds=0.01,
                                   timeout_seconds=0.2) is None

    def test_wait_complete_times_out_when_nobody_answers(self, store):
        item = store.request_question("q", questions=["q?"])
        assert store.wait_complete(item.credential, poll_seconds=0.01,
                                   timeout_seconds=0.3) is None


class TestCrossConnection:
    """**两个连接** —— 本地能测的「跨进程」代理。"""

    def test_answer_from_another_connection_is_visible(self, tmp_path):
        p = tmp_path / "c.db"
        waiter_conn = _fresh(p)
        waiter = ApprovalStore(waiter_conn)
        item = waiter.request_question("q", requested_by="ou_a",
                                       questions=["文件名?"])

        writer_conn = connect(p)
        ApprovalStore(writer_conn).put_answer(item.credential, "out.md",
                                              answered_by="ou_a")
        writer_conn.close()

        assert waiter.answers_of(item.credential) == [["out.md"]]
        waiter_conn.close()

    def test_wait_complete_is_woken_by_the_other_connection(self, tmp_path):
        """等待中的执行器被「另一个连接写入」叫醒 —— 这正是要验的那条。"""
        p = tmp_path / "d.db"
        waiter_conn = _fresh(p)
        waiter = ApprovalStore(waiter_conn)
        item = waiter.request_question("q", requested_by="ou_a",
                                       questions=["第一?", "第二?"])

        got: list = []

        def waiting() -> None:
            got.append(waiter.wait_complete(item.credential, poll_seconds=0.05,
                                            timeout_seconds=15))

        th = threading.Thread(target=waiting, daemon=True)
        th.start()
        threading.Event().wait(0.4)

        writer_conn = connect(p)
        w = ApprovalStore(writer_conn)
        w.put_answer(item.credential, "第一答", answered_by="ou_a")
        writer_conn.close()
        threading.Event().wait(0.4)      # 只答了第一问 —— 还不该返回
        assert not got, "只答了一问就返回了 —— 会把后面的问题丢掉"

        writer_conn = connect(p)
        ApprovalStore(writer_conn).put_answer(item.credential, "第二答",
                                              answered_by="ou_a")
        writer_conn.close()

        th.join(timeout=15)
        assert got == [[["第一答"], ["第二答"]]]
        waiter_conn.close()


class TestPurge:
    def test_purge_keeps_unexpired_questions(self, store):
        """还没过期的行**留着** —— 删了正在等的问题等于把任务判死。"""
        item = store.request_question("q", requested_by="ou_a",
                                      questions=["q?"])
        store.purge_expired(older_than_seconds=0)
        assert store.get(item.credential) is not None

    def test_purge_clears_expired(self, store):
        item = store.request_question("q", requested_by="ou_a",
                                      questions=["q?"], ttl_seconds=-1)
        store.put_answer(item.credential, "out.md", answered_by="ou_a")
        assert store.get(item.credential) is not None      # 过期但还在
        store.purge_expired(older_than_seconds=0)
        assert store.get(item.credential) is None
