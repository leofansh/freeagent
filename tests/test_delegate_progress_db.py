"""执行期进度的**落库投影**（设计文档 12.8 / V1.35）。

## 它与 ``test_delegate_progress.py`` 的分工

那个文件守**飞书卡**（发卡、sequence、节流、收尾）；这个文件守**落库**。
两者的接线点在同一个函数（``_update_progress``）上，但**症状完全不同**：

* 卡片坏了 → 飞书里进度不更新（用户投诉「不动」）
* 落库坏了 → WEB 面板永远空（用户以为没派出去）

## 这里最该守的一条

**落库不能依赖飞书卡存在。** 纯终端部署没有 ``progress_sender``，
而它照样开着 Web 界面 —— 早先那句 ``if progress_sender is None: return``
会让那种部署的 WEB 面板永远是空的。守它的是
:class:`TestProgressProjection` 里的 ``test_writes_without_any_feishu_card``。

## 全部离线

时间由注入的假时钟驱动，所以节流是确定性可测的，不 sleep。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

from freeagent.app import build_app  # noqa: E402
from freeagent.delegate import (  # noqa: E402
    MIN_PROGRESS_INTERVAL,
    _TaskRepoProgress,
    run_with_tool_gate,
)
from freeagent.services.approval import ApprovalStore  # noqa: E402
from freeagent.services.delegate import DelegationPolicy  # noqa: E402
from freeagent.services.opencode_server import ServerError  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_delegate_progress import (  # noqa: E402
    FakeClock,
    _asked,
    _FakeProgressSender,
    _FakeServer,
    _FakeToolSender,
    _StubTask,
    _tool,
)


class _Repo:
    """``ProgressRepo`` 的可观测替身：记下每次调用。"""

    def __init__(self) -> None:
        self.writes: list[dict[str, Any]] = []
        self.clears: list[str] = []
        self.fail_on_write = False

    def write_delegate_progress(self, **kw: Any) -> None:
        if self.fail_on_write:
            raise RuntimeError("数据库被占")
        self.writes.append(kw)

    def clear_delegate_progress(self, task_id: str) -> None:
        self.clears.append(task_id)


class _Boom:
    """事件流一读就炸 —— 验异常路径也要清行。"""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def create_session(self) -> str:
        return "ses_boom"

    def prompt_async(self, *a, **kw) -> None:
        return None

    def events(self, **kw):
        raise ServerError("服务挂了")
        yield  # pragma: no cover - 让它成为生成器

    def abort(self, session_id) -> None:
        return None


def _run(server, *, progress=None, repo=None, task=None, clock=None, **kw):
    """跑一次委派，用**一次性临时库**。

    :func:`TestTaskIdThreading` 里那些要事后查库断言的用例不共用这条 ——
    它们把 app 传进来（见 ``_run_in``），因为「写进 A 库、读 B 库」
    的断言永远为假，而那种假绿比红更难查。
    """
    with tempfile.TemporaryDirectory() as d:
        app = build_app(Path(d) / "a.db")
        try:
            return _run_in(app, server, progress=progress, repo=repo,
                           task=task, clock=clock, **kw)
        finally:
            app.close()


def _run_in(app, server, *, progress=None, repo=None, task=None, clock=None,
            sender=None, **kw):
    store = ApprovalStore(app.conn)
    return run_with_tool_gate(
        task if task is not None else _StubTask(),
        Path("C:/p"), "做点事",
        policy=DelegationPolicy(projects=("C:/p",), model="opencode/x"),
        store=store,
        sender=sender or _FakeToolSender(store),
        progress_sender=progress,
        approver="ou_owner",
        server_factory=lambda project, command: server,
        now=(clock or FakeClock()),
        progress_repo=repo,
        **kw,
    )


class TestProgressProjection:
    def test_start_line_is_written_immediately(self):
        """起跑线：prompt_async 之后立刻写一行「正在启动」。

        少了它，从「开始做」到第一个工具事件之间（可能十几秒）
        WEB 面板**显示不出这条委派** —— 与「没派出去」长得一样。"""
        repo = _Repo()
        _run(_FakeServer([("session.idle", {})]), repo=repo)
        assert repo.writes[0]["status"] == "正在启动"
        assert repo.writes[0]["recent"] == ""

    def test_writes_without_any_feishu_card(self):
        """**没配飞书也照写。** 纯终端部署照样开着 Web 界面，
        而早先那句 `if progress_sender is None: return` 会让它的面板永远空。"""
        repo = _Repo()
        _run(_FakeServer([_tool("read"), ("session.idle", {})]),
             progress=None, repo=repo)
        assert repo.writes, "没配飞书就没落库 —— Web 面板会永远是空的"
        assert any("read" in w["recent"] for w in repo.writes)

    def test_row_carries_the_same_numbers_as_the_card(self):
        """同一份数据：计数必须与飞书卡上的那两个数一致。
        两个面板显示不同的「调用了 N 次」，用户会以为其中一边在骗人。"""
        repo = _Repo()
        prog = _FakeProgressSender()
        _run(_FakeServer([_asked(), ("session.idle", {})]),
             progress=prog, repo=repo)
        last = repo.writes[-1]
        assert last["tool_calls"] == 1 and last["allowed"] == 1

    def test_throttle_applies_to_the_db_too(self):
        """节流**不为数据库豁免**：工具调用密度高时逐次写会把 SQLite
        写放大到事件密度（12.8 明令禁止）。"""
        repo = _Repo()
        clock = FakeClock()
        _run(_FakeServer([_tool("read"), _tool("grep"), _tool("read"),
                          ("session.idle", {})]), repo=repo, clock=clock)
        # 起跑线 1 次 + 第一个工具事件 1 次（force）+ 后续被节流掉
        assert len(repo.writes) == 2, [w["recent"] for w in repo.writes]

    def test_writes_again_after_the_window(self):
        repo = _Repo()
        clock = FakeClock()

        def _slow():
            yield _tool("read")
            clock.advance(MIN_PROGRESS_INTERVAL + 0.1)
            yield _tool("grep")
            yield ("session.idle", {})

        _run(_FakeServer(_slow()), repo=repo, clock=clock)
        assert [w["recent"] for w in repo.writes][-1].endswith("grep")

    def test_feishu_card_untouched_by_the_new_code(self):
        """**回归守**：落库的加入不许多推一张卡。

        起跑线早期走了 ``_update_progress``，于是多推一次与建卡正文
        完全相同的内容 —— 飞书不报错，只是白花一次 sequence，
        而「sequence 为什么跳了一格」会让人以为吞了一次更新。"""
        prog = _FakeProgressSender()
        _run(_FakeServer([_tool("read"), ("session.idle", {})]),
             progress=prog, repo=_Repo())
        assert len(prog.updates) == 1, [u["text"][:40] for u in prog.updates]
        assert prog.updates[0]["sequence"] == 2

    def test_write_failure_never_breaks_the_run(self):
        """落库失败只记日志：展示层的命比不上这次委派的命。"""
        repo = _Repo()
        repo.fail_on_write = True
        out = _run(_FakeServer([_tool("read"), ("session.idle", {})]), repo=repo)
        assert out.ok is True, "数据库写失败被报成了委派失败"

    def test_row_is_cleared_when_the_run_ends(self):
        """跑完就该消失：留着会变成「面板上永远挂着一条僵尸进度」。"""
        repo = _Repo()
        task = _StubTask()
        _run(_FakeServer([_tool("read"), ("session.idle", {})]),
             repo=repo, task=task)
        assert repo.clears == [task.id]

    def test_row_is_cleared_on_server_error(self):
        repo = _Repo()
        task = _StubTask()
        _run(_Boom(), repo=repo, task=task)
        assert repo.clears == [task.id], "异常路径漏清 —— 面板上会留僵尸行"

    def test_clearing_is_swallowed(self):
        """清行失败不许把一次成功的委派报成失败。"""
        class _BadClear(_Repo):
            def clear_delegate_progress(self, task_id):
                raise RuntimeError("锁住了")

        out = _run(_FakeServer([("session.idle", {})]), repo=_BadClear())
        assert out.ok is True


class TestRepoAdapter:
    def test_adapter_uses_wall_clock_not_the_monotonic_counter(self):
        """**这条坑值得单独立一条测试**：``run_with_tool_gate`` 的
        ``now`` 是 monotonic 计数器（给超时判定用），拿它写时间戳会写出
        「开机后多少秒」，而 Web 拿它与真实时间比陈旧，永远算出
        「刚刚才动过」—— 三天前的僵尸进度会永远显示「正在进行」。"""
        with tempfile.TemporaryDirectory() as d:
            app = build_app(Path(d) / "a.db")
            try:
                adapter = _TaskRepoProgress(app.task_repo)
                adapter.write_delegate_progress(
                    task_id="t1", status="正在执行", recent="x",
                    tool_calls=1, allowed=1, project="p",
                )
                row = app.task_repo.delegate_progress("t1")
                assert row is not None
                # ISO 串以年份开头：monotonic 计数会写出「0.0」或「12345.0」
                assert row["updated_at"][:4] == str(__import__("datetime")
                                                    .datetime.now().year)
                adapter.clear_delegate_progress("t1")
                assert app.task_repo.delegate_progress("t1") is None
            finally:
                app.close()

    def test_adapter_round_trips_every_field(self):
        with tempfile.TemporaryDirectory() as d:
            app = build_app(Path(d) / "a.db")
            try:
                _TaskRepoProgress(app.task_repo).write_delegate_progress(
                    task_id="t1", status="等待授权", recent="edit a.py",
                    tool_calls=4, allowed=3, project="D:/p",
                )
                row = app.task_repo.delegate_progress("t1")
                assert row == {
                    "task_id": "t1", "status": "等待授权",
                    "recent": "edit a.py", "tool_calls": 4, "allowed": 3,
                    "project": "D:/p", "updated_at": row["updated_at"],
                }
            finally:
                app.close()

    def test_second_write_overwrites_rather_than_duplicating(self):
        """UPSERT：一次委派写几十次，「删+插」每次多一条事务。"""
        with tempfile.TemporaryDirectory() as d:
            app = build_app(Path(d) / "a.db")
            try:
                a = _TaskRepoProgress(app.task_repo)
                for i in range(3):
                    a.write_delegate_progress(
                        task_id="t1", status="正在执行", recent=str(i),
                        tool_calls=i, allowed=i, project="p")
                assert len(app.task_repo.open_delegate_progress()) == 1
                assert app.task_repo.delegate_progress("t1")["recent"] == "2"
            finally:
                app.close()


class TestTaskIdThreading:
    """待决项要能指回**哪条委派**（12.8）—— 飞书那条链路靠锚点卡提供上下文，
    WEB 面板没有锚点卡，只能从库里自己认。"""

    @pytest.fixture
    def env(self):
        with tempfile.TemporaryDirectory() as d:
            app = build_app(Path(d) / "a.db")
            yield app, ApprovalStore(app.conn)
            app.close()

    def test_tool_call_carries_the_task(self, env):
        app, store = env
        task = _StubTask()
        _run_in(app, _FakeServer([_asked(), ("session.idle", {})]),
                task=task, repo=_Repo())
        rows = app.conn.execute(
            "SELECT task_id FROM pending_approvals").fetchall()
        assert any(r["task_id"] == task.id for r in rows), \
            "授权卡没记归属 —— WEB 面板不知道它属于哪条委派"

    def test_question_carries_the_task(self, env):
        app, store = env
        task = _StubTask()

        def _q():
            # 载荷形状照 **V1 实测**：questions 是**字符串数组**
            # （照抄 test_delegate_progress 的那条 —— 形状写错的表现是
            #  解析器整条放弃，于是断言会看到「一条卡都没发」）。
            yield ("question.asked", {
                "id": "que_1", "sessionID": "ses_fake",
                "questions": ["改哪个？"],
            })
            yield ("session.idle", {})

        _run_in(app, _FakeServer(_q()), task=task, repo=_Repo(),
                sender=_FakeToolSender(store, answers=("只改 README",)))
        rows = app.conn.execute(
            "SELECT task_id, kind FROM pending_approvals").fetchall()
        assert any(r["task_id"] == task.id and r["kind"] == "question"
                   for r in rows), "提问没记归属"

    def test_ask_accepts_task_id_directly(self, env):
        _app, store = env
        p = store.ask("标题", task_id="task-1")
        assert store.get(p.credential).task_id == "task-1"

    def test_task_id_defaults_to_none(self, env):
        """默认 None = 旧调用方行为不变（本地操作类待批本来就没有归属）。"""
        _app, store = env
        assert store.ask("标题").task_id is None


class TestPendingForWeb:
    @pytest.fixture
    def store(self):
        with tempfile.TemporaryDirectory() as d:
            app = build_app(Path(d) / "a.db")
            yield ApprovalStore(app.conn)
            app.close()

    def test_returns_undecided_and_unexpired(self, store):
        a = store.ask("A", ttl_seconds=600)
        store.resolve(a.credential, "allow")
        keep = store.ask("B", ttl_seconds=600)
        gone = store.ask("C", ttl_seconds=0)
        got = {i.credential for i in store.pending_for_web()}
        assert got == {keep.credential}, "已决定的或过期的混进来了"

    def test_newest_first(self, store):
        first = store.ask("先", ttl_seconds=600)
        import time as _t
        _t.sleep(0.001)
        second = store.ask("后", ttl_seconds=600)
        got = [i.credential for i in store.pending_for_web()]
        assert got[0] == second.credential, "用户先看到最老那条"

    def test_partly_answered_question_is_still_pending(self, store):
        q = store.request_question(subject="q", questions=["A?", "B?"],
                                   ttl_seconds=600)
        store.put_answer(q.credential, "答A", answered_by="")
        assert [i.credential for i in store.pending_for_web()] == [q.credential]
