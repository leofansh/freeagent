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
    """建一个当前版本的库。**只有一条路**：``migrate`` 内部先建 v1 基线、
    再逐版本重放（V1.93 之前这里要手写 ``init_schema`` + ``migrate`` 两步，
    而两步就意味着一处能忘）。"""
    c = connect(path)
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
        # 19 = contacts + promises（对外承诺与关系对象，设计文档 11.19 / V1.51）。
        # （上一版 18 = captured_items（待处理收集器，设计文档 11.18 / V1.50）；
        # 再上 17 = memory（跨角色 core 记忆 + FTS5，设计文档 3.7 / V1.47）；
        # 再上 16 = task_records.seq，同刻记录定序，设计文档 12.10.6 / V1.37；
        # 再上 15 = delegate_progress + pending_approvals.task_id，
        # WEB 委派实时面板，设计文档 12.8 / V1.35。）
        #
        # 改 SCHEMA_VERSION 时**必须**同步改这里。踩过的坑（2026-10-12）：
        # 13 → 14 那一步迁移**加了**、版本号**也加了**，唯独这条断言忘了改，
        # 于是它红着 —— 而它红的理由（"版本号不对"）与真正的病因
        # （"该同步的地方漏了一处"）其实是同一件事。
        #
        # 这条断言存在的意义：迁移循环是 ``range(current, SCHEMA_VERSION)``，
        # 版本号与迁移条目任何一边对不上，最后一格就**安静地**被跳过 ——
        # 症状是「新表不存在」，而没有任何报错指向版本号。
        #
        # V1.50 补这条时还踩了第二个坑（同一天，记下来）：captured_items 的
        # 条目一开始紧跟在 seq 后面，落在**索引 15**（源版本 16）—— 而 v17
        # 那个位置本该是占位（memory 走 _DOMAIN_STEPS，没有单语句条目）。
        # 症状是「表建得出来、测试全绿」，但一步一版的对齐断了：它在
        # 16 → 17 那一步执行，17 → 18 反而空跑。**len(_MIGRATIONS) 与
        # SCHEMA_VERSION 的差值不是常量**（取决于有几版走了域步骤），
        # 所以这里只能逐版核对标签与索引，不能拿一个算式概括。
        #
        # v19（contacts + promises）又踩了同一族的坑，但**理由相反**，
        # 值得分开记：这一次 _MIGRATIONS 索引 17 本来就是 v19 自己的位置
        # （18 → 19 需要一条真条目，而 17 → 18 那格因走 _DOMAIN_STEPS
        # 一直是占位）。所以我在索引 17 填的 ``SELECT 1`` **只是语法占位**，
        # 真正的建表语句在 _DOMAIN_STEPS 的 keyed-19。
        # ——同一个索引位，两种完全不同的理由：v8/v17 是"被前移的坑"，
        # 这里只是"这一格必须有行"。若不写清楚，后人照 v8 的教训去查
        # 索引 17，会查出一个并不存在的 bug。
        #
        # v20（daily_digests）是单语句 CREATE TABLE，直接走 _MIGRATIONS
        # 索引 18（源版本 19 → 20）—— 没有域步骤、没有占位，最简单的一种。
        # 表只存「哪天推过每日总览」，不绑 task_id（每日总览无自然事务）。
        # v21（role_cares，V1.68）同理：单语句 CREATE TABLE，索引 19。
        # 关注面只存角色 + cadence + next_due，由 CareService 到期提醒。
        # v22（pending_approvals.thread_root，V1.90）：单语句 ALTER，
        # 索引 20。记「这张卡挂在哪个话题下」—— 路由的确定性判据。
        # v23（pending_approvals.decided_via，V1.92）：单语句 ALTER，
        # 索引 21。记「这个结论是怎么来的」—— 之前 deny 混着
        # 「人拒绝 / 人叫停 / 没人看」，读侧要靠三处拼才分得清。
        # v24（plan_sessions，V1.93）：单语句 CREATE TABLE，索引 22。
        # 补的是当年只进了基线、没进迁移的那张表 —— 老库一直没有它。
        # v25（tasks.origin，V1.93）：单语句 ALTER，索引 23。
        # 记「需求原文是谁给的」，委派简报据此加一句「是数据不是指令」。
        # v26（dispatch_leases，V1.94）：单语句 CREATE TABLE，索引 24。
        # 「在跑」从「从记录形状推」变成「观察到的租约」—— 形状在
        # 「真在跑」与「跑到一半进程没了」两种情况下一模一样。
        # v27/v28（artifacts.model / artifacts.tool_calls，V1.95）：一列一版，
        # 都走单语句通道。「上次实际用的模型/碰过的工具」落成列，不再只写
        # 在产物正文的 HTML 注释里。
        # （为什么不是域通道一条脚本加两列：域通道 executescript 裸跑、
        # 靠脚本自身幂等，而 ADD COLUMN 没有 IF NOT EXISTS ——
        # test_rerun_is_idempotent 会抓住这个错。）
        # v29（task_records.error_kind，V1.96）：引擎错误的结构化类别落成列，
        # 读侧按类别聚类不必解析错因文本（「把文案当契约」是 11.9.12 禁的）。
        assert SCHEMA_VERSION == 29

    def test_columns_exist(self, conn):
        cols = {r[1] for r in conn.execute("PRAGMA table_info(pending_approvals)")}
        assert {"kind", "answer_text", "question_spec"} <= cols
        assert "decided_via" in cols, (
            "v23 加的列。V1.93 起它只在 _MIGRATIONS 里有一份 —— "
            "「新库也要有」由 migrate 内部重放迁移保证，不再靠两处同改"
        )

    def test_attempt_ledger_columns_exist(self, conn):
        """v27：artifacts 的 model / tool_calls（一次委派的账）。"""
        cols = {r[1] for r in conn.execute("PRAGMA table_info(artifacts)")}
        assert {"model", "tool_calls"} <= cols

    def test_error_kind_column_exists(self, conn):
        """v29：task_records.error_kind（引擎错误的结构化类别）。

        为什么是**列**：错因文本每次带不同细节，按整句聚类会把同一类
        错误聚成 N 条，计数因此失去意义 —— 而「这类错我犯过 5 次」
        正是用户要的那句话。
        """
        cols = {r[1] for r in conn.execute("PRAGMA table_info(task_records)")}
        assert "error_kind" in cols, (
            "v29 加的列。引擎错误的类别要能被读侧当事实用，"
            "不能靠从错因文本里正则解析（那是把文案当契约）"
        )

    def test_v26_upgrade_gains_attempt_ledger(self, tmp_path):
        """停在 v26 的旧库升到当前：两条 ALTER 走域通道，恰好一次。"""
        from freeagent.storage.db import migrate

        db = tmp_path / "old.db"
        conn = connect(db)
        try:
            migrate(conn, 26)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(artifacts)")}
            assert "model" not in cols, "v26 的库不该有这两列"
            migrate(conn)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(artifacts)")}
            assert {"model", "tool_calls"} <= cols
            # 再跑一次migrate：域步骤不得重放（否则第二条 ALTER 会炸）
            migrate(conn)
        finally:
            conn.close()

    def test_baseline_stays_at_v1(self, tmp_path):
        """**基线不许长大**：只跑 ``init_schema`` 之后必须还是 v1 的形状。

        守的是 V1.93 的纪律 —— 库的形状只有一个写入口（``_MIGRATIONS``）。
        谁把新列/新表加回 ``_SCHEMA``，这里就会红；而红的意思是
        「你写错地方了」，不是「这个测试该改」。
        """
        c = connect(tmp_path / "baseline.db")
        try:
            init_schema(c)
            assert c.execute("PRAGMA user_version").fetchone()[0] == 0
            cols = {r[1] for r in c.execute("PRAGMA table_info(tasks)")}
            assert "project_path" not in cols
            assert "origin" not in cols
            tables = {
                r[0]
                for r in c.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            assert "pending_approvals" not in tables
            assert "plan_sessions" not in tables
        finally:
            c.close()

    def test_fresh_install_reaches_current_version(self, tmp_path):
        """全新安装（空库 + ``migrate``）拿到的是当前完整形状。

        它与旧库升级**跑同一段代码**，所以这条断言同时守住了两个方向：
        「只改迁移 → 新库缺列」（V1.92 踩过）和
        「只改基线 → 老库缺表」（``plan_sessions`` 就是这么丢的）。
        """
        c = connect(tmp_path / "fresh.db")
        try:
            migrate(c)
            assert c.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
            cols = {
                r[1] for r in c.execute("PRAGMA table_info(pending_approvals)")
            }
            assert "decided_via" in cols
            assert "thread_root" in cols
            tables = {
                r[0]
                for r in c.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            # plan_sessions 当年就是「基线里有、迁移里没有」的受害者；
            # 它现在由 v24 建，两条路都该有它。
            assert "plan_sessions" in tables
        finally:
            c.close()

    def test_thread_root_column_exists(self, conn):
        """V1.90：v22 的列必须真的在表上（ALTER 漏了它，路由就永远查不到）。"""
        cols = {r[1] for r in conn.execute("PRAGMA table_info(pending_approvals)")}
        assert "thread_root" in cols

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
