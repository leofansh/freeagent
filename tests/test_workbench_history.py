"""「最近那件事」得能点回去看 —— 轮次落库与回读（21.4 / V1.115）。

V1.114 的一轮只活在进程内存里：关掉网页再打开，刚才那件事就没了。这一版
把「我说了什么 / 它回了什么 / 停在哪一步」落进 ``workbench_turns``。

这里守的是**四条容易做错的**：

* id 必须跨进程唯一（``t1`` 重启重号会把上一行顶掉）；
* 「同刻两轮」按**插入序**定序（``started_at`` 是秒级文本，同刻等于随机）；
* 上次进程留下的 ``running`` 要收口成 ``failed`` 并写明原因
  （否则列表里一直转着一个永远不会结束的圈）；
* JSON 读坏要**说出来**，不许显示成「那轮什么都没做」。
"""

from __future__ import annotations

from datetime import datetime

from freeagent.services import agent_turn
from freeagent.services.clock import FrozenClock
from freeagent.storage.repos import WorkbenchTurnRepo


class _Host:
    """按脚本吐事件的最小宿主（不连网络、不起进程）。"""

    def __init__(self, script: list[tuple[str, object]]) -> None:
        self.script = script
        self.prompts: list[str] = []
        self.aborts = 0

    def ensure(self, **kwargs: object) -> str:
        return "ses-1"

    def prompt(self, text: str, **kw: str) -> None:
        self.prompts.append(text)

    def events(self, *, timeout: float = 600.0):  # noqa: ANN201
        return iter(self.script)

    def abort(self) -> None:
        self.aborts += 1

    def reply_permission(self, request_id: str, decision: str) -> None:
        return None

    def reply_question(self, request_id: str, answers: list[list[str]]) -> None:
        return None


def _svc(script: list[tuple[str, object]], repo: WorkbenchTurnRepo | None,
         clock: FrozenClock) -> agent_turn.AgentTurnService:
    return agent_turn.AgentTurnService(_Host(script), repo=repo, clock=clock)


class TestRepo:
    def test_row_round_trip(self, app) -> None:  # noqa: ANN001
        repo = WorkbenchTurnRepo(app.conn)
        repo.add(
            turn_id="w1", brief="改一下 app.py", directory="D:\\p",
            mode="ask", started_at=datetime(2026, 9, 26, 9, 0), model="m",
        )
        row = repo.get("w1")
        assert row is not None
        assert row["brief"] == "改一下 app.py"
        assert row["status"] == "running"
        assert row["directory"] == "D:\\p"

    def test_recent_orders_by_insertion_when_seconds_collide(self, app) -> None:  # noqa: ANN001
        # 连点两次「交给它」会落在同一秒。按 started_at 定序等于随机 ——
        # 所以定序用 rowid（V1.113 的那条纪律）。
        repo = WorkbenchTurnRepo(app.conn)
        same = datetime(2026, 9, 26, 9, 0)
        repo.add(turn_id="w1", brief="先", directory="D:\\p", mode="ask",
                 started_at=same)
        repo.add(turn_id="w2", brief="后", directory="D:\\p", mode="ask",
                 started_at=same)
        assert [r["id"] for r in repo.recent(2)] == ["w2", "w1"]

    def test_reap_marks_stale_running_and_waiting_as_failed(self, app) -> None:  # noqa: ANN001
        repo = WorkbenchTurnRepo(app.conn)
        at = datetime(2026, 9, 26, 9, 0)
        repo.add(turn_id="w1", brief="跑到一半被杀", directory="D:\\p",
                 mode="ask", started_at=at, status="running")
        repo.add(turn_id="w2", brief="卡在问答上", directory="D:\\p",
                 mode="ask", started_at=at, status="waiting")
        repo.add(turn_id="w3", brief="早就结束了", directory="D:\\p",
                 mode="ask", started_at=at, status="done")
        assert repo.reap(at, reason="FreeAgent 重启了") == 2
        assert repo.get("w1")["status"] == "failed"  # type: ignore[index]
        assert repo.get("w1")["reason"] == "FreeAgent 重启了"  # type: ignore[index]
        assert repo.get("w3")["status"] == "done"  # type: ignore[index]
        # 幂等：收口过的不会再被改。
        assert repo.reap(at, reason="FreeAgent 重启了") == 0


class TestServicePersists:
    def test_a_turn_is_written_at_start_and_finish(self, app) -> None:  # noqa: ANN001
        repo = WorkbenchTurnRepo(app.conn)
        svc = _svc([("message.part.delta", {"delta": "改完了"}),
                    ("session.idle", {})], repo, app.clock)
        view = svc.start(text="改一下 app.py", directory="D:\\p", model="m")
        svc.wait(view.turn_id, timeout=10)
        row = repo.get(view.turn_id)
        assert row is not None
        assert row["status"] == "done"
        assert row["ok"] == 1
        assert row["text"] == "改完了"
        assert row["brief"] == "改一下 app.py"

    def test_ids_are_unique_across_restarts(self, app) -> None:  # noqa: ANN001
        # 两轮分属两个「进程」（两个服务实例），id 不能撞 —— 撞了就会
        # 把上一行顶掉，主人点开历史看到的是另一件事的正文。
        repo = WorkbenchTurnRepo(app.conn)
        first = _svc([("session.idle", {})], repo, app.clock)
        v1 = first.start(text="第一件事", directory="D:\\p")
        first.wait(v1.turn_id, timeout=10)
        second = _svc([("session.idle", {})], repo, app.clock)
        v2 = second.start(text="第二件事", directory="D:\\p")
        second.wait(v2.turn_id, timeout=10)
        assert v1.turn_id != v2.turn_id
        assert repo.get(v1.turn_id)["brief"] == "第一件事"  # type: ignore[index]

    def test_construction_reaps_what_the_last_process_left(self, app) -> None:  # noqa: ANN001
        repo = WorkbenchTurnRepo(app.conn)
        repo.add(turn_id="w-old", brief="上一次没跑完", directory="D:\\p",
                 mode="ask", started_at=datetime(2026, 9, 26, 8, 0))
        _svc([("session.idle", {})], repo, app.clock)  # 建服务即收口
        assert repo.get("w-old")["status"] == "failed"  # type: ignore[index]
        assert "重启了" in str(repo.get("w-old")["reason"])  # type: ignore[index]


class TestReadBack:
    def test_view_after_restart_comes_from_the_database(self, app) -> None:  # noqa: ANN001
        repo = WorkbenchTurnRepo(app.conn)
        first = _svc([("message.part.delta", {"delta": "改完了"}),
                      ("session.idle", {})], repo, app.clock)
        tid = first.start(text="改一下 app.py", directory="D:\\p").turn_id
        first.wait(tid, timeout=10)
        second = _svc([], repo, app.clock)  # 新进程：内存里没有这一轮
        view = second.view(tid)
        assert view is not None
        assert view.status == agent_turn.DONE
        assert view.text == "改完了"
        assert view.brief == "改一下 app.py"
        assert second.latest() is not None

    def test_steps_survive_a_restart(self, app) -> None:  # noqa: ANN001
        repo = WorkbenchTurnRepo(app.conn)
        first = _svc([("tool", {"tool": "read"}),
                      ("message.part.updated", {"part": {
                          "type": "tool", "tool": "edit",
                          "state": {"status": "completed",
                                    "input": {"filePath": "a.py"}}}}),
                      ("session.idle", {})], repo, app.clock)
        tid = first.start(text="改一下 a.py", directory="D:\\p").turn_id
        first.wait(tid, timeout=10)
        view = _svc([], repo, app.clock).view(tid)  # 新进程
        assert view is not None
        assert [s.name for s in view.steps] == ["read", "edit"]
        assert view.steps[1].target == "a.py"

    def test_history_without_a_repo_says_it_has_none(self) -> None:
        # 「没有历史能力」与「历史是空的」是两件事：前者要能说清。
        svc = _svc([], None, FrozenClock(datetime(2026, 9, 26, 9, 0)))
        assert svc.has_history() is False
        assert svc.history() == ()

    def test_broken_json_is_reported_not_shown_as_empty(self, app) -> None:  # noqa: ANN001
        repo = WorkbenchTurnRepo(app.conn)
        repo.add(turn_id="w1", brief="坏行", directory="D:\\p", mode="ask",
                 started_at=datetime(2026, 9, 26, 9, 0))
        app.conn.execute("UPDATE workbench_turns SET notes_json='{oops'"
                         " WHERE id='w1'")
        app.conn.commit()
        svc = _svc([], repo, app.clock)
        view = svc.view("w1")
        assert view is not None
        assert any("读不全" in n for n in view.notes)
