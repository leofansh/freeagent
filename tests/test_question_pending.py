"""「待答问题」落库与跨进程往返的守卫。

## 为什么要有这一层测试

执行器与桥接**是两个进程**，答复只能靠库传递。所以真正要锁住的
不是「方法返回了什么」，而是**另一个连接能不能把答复送进去、
等待的一方能不能看见**。这里用**两个独立连接**做本地代理 ——
它不是真的多进程，但跨连接可见性与跨进程可见性在 SQLite 上
只差一个文件锁，而剩下那条锁是 opencode 那侧的既有机制。

## 锁住的判断

- 迁移：新列真的加上；重复跑不炸（旧库上人手改过版本号的情况）
- **向后兼容**：加列前的历史行没有 ``kind``，读出来必须是 ``approval``
  ——反过来把空当 question，那些行会突然开始拦「打字」
- 只有发起人能答（提问同样受这条约束）
- 过期：既答不了、也读不出答复
- 空白答复**不落库** —— 否则会把 agent 从「还在等」推到「拿到空答案」
- 授权路径**完全不受影响**（这一层是加在既有表上的，不是另起一套）
"""
from __future__ import annotations

import datetime
import pathlib

import pytest

from freeagent.services.approval import ApprovalStore, new_credential
from freeagent.storage.db import SCHEMA_VERSION, connect, init_schema, migrate


@pytest.fixture
def conn(tmp_path):
    """**必须走生产那两步**：``init_schema`` 只建表（表定义里没有后来加的列），
    ``migrate`` 才按版本补列。少调一步，测试就会在一个「永远缺列」的库上跑 ——
    那种红是自造的，不是产品的问题（第一版就这么栽过）。"""
    c = connect(tmp_path / "a.db")
    init_schema(c)
    migrate(c)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def store(conn):
    return ApprovalStore(conn)


class TestMigration:
    def test_version_bumped(self):
        assert SCHEMA_VERSION == 10

    def test_columns_exist(self, conn):
        cols = {r[1] for r in conn.execute("PRAGMA table_info(pending_approvals)")}
        assert {"kind", "answer_text"} <= cols

    def test_rerun_is_idempotent(self, tmp_path):
        """旧库上重复迁移不能炸 —— 用户的版本号可能被人手工改过。

        模拟的是**真实**情形：库已经是 v10（列都在），但版本号被手工退回 9。
        第一版我造的是「版本号说 9、但 8→9 那步没做」—— 那不真实，
        结果断言正确地告诉我 ``kind`` 永远不会出现。**测试自己撒谎时，
        断言会先抓它。**
        """
        p = tmp_path / "b.db"
        c = connect(p)
        init_schema(c)
        migrate(c)
        c.execute("PRAGMA user_version = 9")
        c.commit()
        c.close()

        c2 = connect(p)
        migrate(c2)                    # 不抛就是过（_column_exists 兜底）
        cols = {r[1] for r in c2.execute("PRAGMA table_info(pending_approvals)")}
        assert {"kind", "answer_text"} <= cols
        c2.close()

    def test_old_row_without_kind_reads_as_approval(self, conn):
        """加列前的历史行**没有** kind，读出来必须是 approval。

        这是最容易搞坏的一条：反过来把空当 question，那些行会
        突然开始拦「打字」并期待一段文本答复 —— 而它们全是授权。
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
        assert item.is_answered is False


class TestRequestQuestion:
    def test_is_question(self, store):
        item = store.request_question("写到哪个文件？")
        assert item.is_question is True
        assert item.kind == "question"
        assert item.is_answered is False

    def test_answer_text_is_none_before_answering(self, store):
        assert store.request_question("q").answer_text is None

    def test_ask_is_still_an_approval(self, store):
        """**授权路径不受影响** —— 这一层是加在既有表上的。"""
        item = store.ask("允许改这个文件？")
        assert item.is_question is False
        assert item.is_decided is False

    def test_credential_prefix_is_distinct(self, store):
        """提问用 ``q`` 前缀 —— 只是为了日志里一眼能分，**判定不靠它**。"""
        q = store.request_question("q")
        a = store.ask("a")
        assert q.credential.startswith("q")
        assert a.credential.startswith("ap")


class TestAnswer:
    def test_round_trip(self, store):
        item = store.request_question("写到哪个文件？", requested_by="ou_a")
        assert store.answer_text(item.credential, "out.md",
                                 answered_by="ou_a") is True
        assert store.text_answer(item.credential) == "out.md"
        assert store.get(item.credential).is_answered is True

    def test_non_initiator_rejected(self, store):
        """提问也受「只有发起人能答」约束 —— 否则别人能往你的会话塞答案。"""
        item = store.request_question("q", requested_by="ou_a")
        assert store.answer_text(item.credential, "out.md",
                                 answered_by="ou_evil") is False
        assert store.text_answer(item.credential) is None
        assert store.get(item.credential).is_answered is False

    def test_old_row_without_initiator_can_be_answered(self, store):
        """没有 ``requested_by`` 的行**不拦** —— 加列前的历史不该点不动。"""
        item = store.request_question("q")
        assert store.answer_text(item.credential, "out.md",
                                 answered_by="ou_anyone") is True

    def test_whitespace_answer_is_not_stored(self, store):
        """空白答复**不落库**：否则 agent 从「还在等」被推到「拿到空答案」。"""
        item = store.request_question("q", requested_by="ou_a")
        for blank in ("", "   ", "\n", "\t ", "\r\n", "　"):
            assert store.answer_text(item.credential, blank,
                                     answered_by="ou_a") is False, repr(blank)
        assert store.get(item.credential).answer_text is None
        assert store.get(item.credential).is_answered is False
        # 还能正常答 —— 这是「不落库」的关键后果
        assert store.answer_text(item.credential, "out.md",
                                 answered_by="ou_a") is True

    def test_second_answer_does_not_overwrite(self, store):
        """已答不覆盖 —— 第一遍是谁答的要留住。"""
        item = store.request_question("q", requested_by="ou_a")
        assert store.answer_text(item.credential, "第一版",
                                 answered_by="ou_a") is True
        assert store.answer_text(item.credential, "第二版",
                                 answered_by="ou_a") is False
        assert store.text_answer(item.credential) == "第一版"

    def test_answered_by_is_recorded(self, store):
        """「谁答的」必须事后查得到 —— 与授权的 decided_by 同一个位置。"""
        item = store.request_question("q", requested_by="ou_a")
        store.answer_text(item.credential, "out.md", answered_by="ou_a")
        got = store.get(item.credential)
        assert got.decided_by == "ou_a"
        assert got.decided_at is not None

    def test_answering_an_approval_is_refused(self, store):
        """授权行不能被当提问答 —— 那是两条通道，混了就说明调用方搞错了。"""
        item = store.ask("允许？", requested_by="ou_a")
        assert store.answer_text(item.credential, "out.md",
                                 answered_by="ou_a") is False

    def test_text_answer_on_approval_is_none(self, store):
        assert store.text_answer(store.ask("a").credential) is None


class TestExpiry:
    def _expired(self, store):
        item = store.request_question("q", requested_by="ou_a", ttl_seconds=-1)
        return item

    def test_cannot_answer_after_expiry(self, store):
        item = self._expired(store)
        assert store.answer_text(item.credential, "out.md",
                                 answered_by="ou_a") is False

    def test_text_answer_is_none_after_expiry(self, store):
        item = self._expired(store)
        assert store.text_answer(item.credential) is None

    def test_is_expired_is_visible(self, store):
        """过期本身要能单独看出来 —— 调用方要分「没人答」与「还在等」，
        而这两件事给用户看的文案不一样。"""
        assert self._expired(store).is_expired is True

    def test_wait_text_gives_up_on_expiry(self, store):
        item = self._expired(store)
        assert store.wait_text(item.credential, poll_seconds=0.01,
                               timeout_seconds=0.2) is None


class TestCrossConnection:
    """**两个连接** —— 本地能测的「跨进程」代理。"""

    def test_answer_written_by_another_connection_is_visible(self, tmp_path):
        p = tmp_path / "c.db"
        waiter_conn = connect(p)
        init_schema(waiter_conn)
        migrate(waiter_conn)
        waiter = ApprovalStore(waiter_conn)

        item = waiter.request_question("写到哪个文件？", requested_by="ou_a")

        # 另一个连接 = 另一个进程会做的事
        writer_conn = connect(p)
        writer = ApprovalStore(writer_conn)
        assert writer.answer_text(item.credential, "out.md",
                                  answered_by="ou_a") is True
        writer_conn.close()

        assert waiter.text_answer(item.credential) == "out.md"
        waiter_conn.close()

    def test_wait_text_is_woken_by_the_other_connection(self, tmp_path):
        """等待中的执行器被「另一个连接写入」叫醒 —— 这正是要验的那条。"""
        import threading

        p = tmp_path / "d.db"
        waiter_conn = connect(p)
        init_schema(waiter_conn)
        migrate(waiter_conn)
        waiter = ApprovalStore(waiter_conn)
        item = waiter.request_question("写到哪个文件？", requested_by="ou_a")

        got: list[str | None] = []

        def waiting() -> None:
            got.append(waiter.wait_text(item.credential, poll_seconds=0.05,
                                        timeout_seconds=10))

        th = threading.Thread(target=waiting, daemon=True)
        th.start()
        threading.Event().wait(0.4)      # 让它真的进入等待

        writer_conn = connect(p)
        ApprovalStore(writer_conn).answer_text(item.credential, "out.md",
                                               answered_by="ou_a")
        writer_conn.close()

        th.join(timeout=10)
        assert got == ["out.md"]
        waiter_conn.close()

    def test_wait_text_returns_none_when_nobody_answers(self, store):
        item = store.request_question("q")
        assert store.wait_text(item.credential, poll_seconds=0.01,
                               timeout_seconds=0.3) is None


class TestPurge:
    def test_purge_also_clears_answered_questions(self, store):
        """已答的提问行会被清掉 —— 否则库里堆的都是「已经没用的文本」。

        第一版我建的是 TTL 600 秒的行就期待它被 purge 掉 —— 那当然删不掉：
        ``purge_expired`` 判的是 **已过期**，不是「已答复」。它的语义是
        「过期的行留着也没用」，而不是「有结论的行就删」。按真实语义重写。
        """
        item = store.request_question("q", requested_by="ou_a", ttl_seconds=-1)
        store.answer_text(item.credential, "out.md", answered_by="ou_a")
        assert store.get(item.credential) is not None      # 过期但还在
        store.purge_expired(older_than_seconds=0)
        assert store.get(item.credential) is None

    def test_purge_keeps_unexpired_questions(self, store):
        """还没过期的行**留着** —— 删了正在等的问题就等于把任务判死。"""
        item = store.request_question("q", requested_by="ou_a")
        store.purge_expired(older_than_seconds=0)
        assert store.get(item.credential) is not None
