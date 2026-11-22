"""一轮执行的规矩：终态、授权、叫停、超限、失败都要说清（21.3 / V1.114）。

替身是 ``ScriptedHost``：它按脚本吐事件，不连网络、不起进程。
**注意**：这里 monkeypatch 掉 ``current_adapter``，因为载荷解析属于
执行器适配器那一层的知识 —— 这些测试要验的是**这一轮怎么收口**，
不是「真机载荷长什么样」（那在 ``test_opencode_server`` / 真机矩阵里）。
"""

from __future__ import annotations

import time
from datetime import datetime

import pytest

from freeagent.services import agent_turn as turn_mod
from freeagent.services.clock import FrozenClock
from freeagent.services.executors import ToolPermission, ToolQuestion
from freeagent.services.opencode_server import ServerError


class FakeAdapter:
    """认得出的载荷原样返回，认不出的返回 ``None``（真适配器也这么报）。"""

    def parse_request(self, payload: object) -> object:
        return payload if isinstance(payload, ToolPermission) else None

    def parse_question(self, payload: object) -> object:
        return payload if isinstance(payload, ToolQuestion) else None


@pytest.fixture(autouse=True)
def _adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(turn_mod, "current_adapter", lambda: FakeAdapter())


class ScriptedHost:
    def __init__(self, script: list[tuple[str, object]], *,
                 ensure_error: Exception | None = None,
                 reply_error: Exception | None = None) -> None:
        self.script = script
        self.ensure_error = ensure_error
        #: 送答复失败（V1.137）：用来验证「送不到不许假装成功」。
        self.reply_error = reply_error
        self.ensure_kwargs: dict[str, object] = {}
        self.prompts: list[tuple[str, dict[str, str]]] = []
        self.replies: list[tuple[str, str]] = []
        self.aborts = 0
        self.pending: list[object] = []
        self.pending_calls = 0
        #: 事件流是否先于指令建立（SSE volatile，晚挂会漏终态帧）。
        self.events_opened = False
        self.events_before_prompt = True

    def ensure(self, **kwargs: object) -> str:
        if self.ensure_error is not None:
            raise self.ensure_error
        self.ensure_kwargs = kwargs
        return "ses-1"

    def prompt(self, text: str, **kw: str) -> None:
        if not self.events_opened:
            self.events_before_prompt = False
        self.prompts.append((text, dict(kw)))

    def events(self, *, timeout: float = 600.0):  # noqa: ANN201
        self.events_opened = True
        return iter(self.script)

    def abort(self) -> None:
        self.aborts += 1

    def pending_permissions(self, *, directory: str = "") -> list[object]:
        self.pending_calls += 1
        return list(self.pending)

    def reply_permission(self, request_id: str, decision: str) -> None:
        if self.reply_error is not None:
            raise self.reply_error
        self.replies.append((request_id, decision))

    def reply_question(self, request_id: str, answers: list[list[str]]) -> None:
        if self.reply_error is not None:
            raise self.reply_error
        self.replies.append((request_id, answers[0][0] if answers else ""))


def _service(script: list[tuple[str, object]], *, max_tool_calls: int = 40,
             ensure_error: Exception | None = None) -> tuple[turn_mod.AgentTurnService, ScriptedHost]:
    host = ScriptedHost(script, ensure_error=ensure_error)
    return turn_mod.AgentTurnService(
        host, max_tool_calls=max_tool_calls,
        clock=FrozenClock(datetime(2026, 9, 26, 9, 0)),
    ), host


def _start(svc: turn_mod.AgentTurnService, **kw: object) -> str:
    view = svc.start(text="改一下 app.py", directory="D:\\p", **kw)  # type: ignore[arg-type]
    return view.turn_id


def _wait_for_ask_id(svc: turn_mod.AgentTurnService, tid: str, want_id: str,
                     *, timeout: float = 10.0):
    """等**指定 request_id** 的卡，而不是「任意一张卡」。

    ``wait_for_ask`` 只 ``ask_event.wait``、**不清事件**，连续等第二张时
    会被上一张遗留的置位骗到（当场返回旧的 ``view.ask``，而 worker 还没
    处理完上一张的决定、还没挂新卡）。测试里要辨认「换动作仍挂卡」，必须
    按 id 轮询到目标卡出现 —— 用事件 + 短超时等待，不裸 ``sleep``。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        view = svc.view(tid)
        if view is not None and view.ask is not None \
                and view.ask.request_id == want_id:
            return view.ask
        turn = svc._turns[tid]
        turn.ask_event.wait(0.05)
        turn.ask_event.clear()
    return None


class TestHappyPath:
    def test_stream_then_idle_is_done(self) -> None:
        svc, host = _service([
            ("message.part.delta", {"delta": "好的，"}),
            ("message.part.delta", {"delta": "我改完了"}),
            ("tool", {"tool": "read"}),
            ("session.idle", {}),
        ])
        tid = _start(svc)
        view = svc.wait(tid, timeout=10)
        assert view is not None
        assert view.status == turn_mod.DONE
        assert view.text == "好的，我改完了"
        assert view.tool_calls == 1
        assert view.tools == ("read",)
        assert view.ok is True

    def test_uploaded_part_replaces_instead_of_appending(self) -> None:
        # 整块事件给的是**整段**；拼接会让同一段出现两遍（正文越来越长）。
        svc, _ = _service([
            ("message.part.delta", {"delta": "abc"}),
            ("message.part.updated", {"part": {"text": "abd"}}),
            ("session.idle", {}),
        ])
        view = svc.wait(_start(svc), timeout=10)
        assert view is not None and view.text == "abd"

    def test_prompt_is_folded_to_one_line(self) -> None:
        svc, host = _service([("session.idle", {})])
        svc.start(text="第一行\n第二行", directory="D:\\p")
        svc.wait(svc.latest().turn_id, timeout=10)  # type: ignore[union-attr]
        assert host.prompts[0][0] == "第一行 第二行"


class TestPermission:
    def test_ask_mode_surfaces_a_card_and_sends_the_decision(self) -> None:
        perm = ToolPermission("per-1", "edit", ("src/app.py",))
        svc, host = _service([("permission.asked", perm), ("session.idle", {})])
        tid = _start(svc)
        card = svc.wait_for_ask(tid, timeout=10)
        assert card is not None
        assert card.kind == "permission" and card.request_id == "per-1"
        assert svc.view(tid).status == turn_mod.WAITING  # type: ignore[union-attr]
        assert svc.decide(tid, allow=True) is True
        view = svc.wait(tid, timeout=10)
        assert view is not None and view.status == turn_mod.DONE
        assert host.replies == [("per-1", "allow")]

    def test_deny_is_forwarded_as_deny(self) -> None:
        perm = ToolPermission("per-2", "bash", ("rm -rf x",))
        svc, host = _service([("permission.asked", perm), ("session.idle", {})])
        tid = _start(svc)
        assert svc.wait_for_ask(tid, timeout=10) is not None
        svc.decide(tid, allow=False)
        svc.wait(tid, timeout=10)
        assert host.replies == [("per-2", "deny")]

    def test_session_scope_remembers_permission(self) -> None:
        """点「本会话内允许此类」后，同类动作直接放行、不再挂卡。

        事件串行消费：第一条 permission 触发的 ``_ask_and_reply`` 会阻塞等
        decision，第二条要等第一条决定后才被处理 —— 这样第二条才能命中
        ``turn.session_allows``、走「免问直接 reply」那条路。
        """
        e1 = ToolPermission("e1", "edit", ("a.py",))
        e2 = ToolPermission("e2", "edit", ("b.py",))
        svc, host = _service([
            ("permission.asked", e1),
            ("permission.asked", e2),
            ("session.idle", {}),
        ])
        tid = _start(svc)
        card = svc.wait_for_ask(tid, timeout=10)
        assert card is not None and card.request_id == "e1"
        # session 档记住的是「edit」这个动作类型，不是某一条 request_id。
        assert svc.decide(tid, allow=True, scope="session") is True
        view = svc.wait(tid, timeout=10)
        assert view is not None and view.status == turn_mod.DONE
        # 两条都是 allow：第二条没挂卡、没等人决定，直接放行送回引擎。
        assert host.replies == [("e1", "allow"), ("e2", "allow")]

    def test_session_scope_does_not_cover_other_permission(self) -> None:
        """记住 edit 不代表记住 bash —— 换一种动作仍要问（范围宁可小）。"""
        e1 = ToolPermission("e1", "edit", ("a.py",))
        e2 = ToolPermission("e2", "bash", ("ls",))
        svc, host = _service([
            ("permission.asked", e1),
            ("permission.asked", e2),
            ("session.idle", {}),
        ])
        tid = _start(svc)
        assert svc.wait_for_ask(tid, timeout=10) is not None
        assert svc.decide(tid, allow=True, scope="session") is True
        # 第二种动作仍挂卡等人；按 id 轮询，绕开 wait_for_ask 不清事件的坑。
        card2 = _wait_for_ask_id(svc, tid, "e2")
        assert card2 is not None
        assert svc.decide(tid, allow=True) is True
        svc.wait(tid, timeout=10)
        assert host.replies == [("e1", "allow"), ("e2", "allow")]
        assert svc._turns[tid].session_allows == {"edit"}

    def test_empty_permission_never_records(self) -> None:
        """认不出动作名时连 session 档也不记 —— 空串不是「所有动作」。"""
        perm = ToolPermission("e-empty", "", ("a.py",))
        svc, host = _service([("permission.asked", perm), ("session.idle", {})])
        tid = _start(svc)
        assert svc.wait_for_ask(tid, timeout=10) is not None
        assert svc.decide(tid, allow=True, scope="session") is True
        svc.wait(tid, timeout=10)
        turn = svc._turns[tid]
        assert turn.session_allows == set()

    def test_auto_mode_denies_an_unexpected_ask_and_says_so(self) -> None:
        # 自动档下本该一条都不来。来了 = 覆盖层可能没生效 ——
        # 那时唯一安全的动作是拒绝并留一条备注，而不是顺手放行。
        perm = ToolPermission("per-3", "edit", ("a.py",))
        svc, host = _service([("permission.asked", perm), ("session.idle", {})])
        view = svc.wait(_start(svc, mode=turn_mod.ENGINE_AUTO_DIR), timeout=10)
        assert view is not None
        assert host.replies == [("per-3", "deny")]
        assert any("没生效" in n for n in view.notes)

    def test_question_is_answered_from_the_card(self) -> None:
        q = ToolQuestion("q-1", ("用哪个目录？",), ("src", "tests"))
        svc, host = _service([("question.asked", q), ("session.idle", {})])
        tid = _start(svc)
        card = svc.wait_for_ask(tid, timeout=10)
        assert card is not None and card.kind == "question"
        svc.decide(tid, allow=True, answer="src")
        svc.wait(tid, timeout=10)
        assert host.replies == [("q-1", "src")]

    def test_question_without_an_answer_fails_loudly(self) -> None:
        q = ToolQuestion("q-2", ("确认一下？",), ("是", "否"))
        svc, host = _service([("question.asked", q), ("session.idle", {})])
        tid = _start(svc)
        assert svc.wait_for_ask(tid, timeout=10) is not None
        svc.decide(tid, allow=False)
        view = svc.wait(tid, timeout=10)
        assert view is not None
        assert view.status == turn_mod.FAILED
        assert "没有拿到答案" in view.reason
        assert host.aborts >= 1

    def test_reply_failure_fails_the_turn_and_aborts(self) -> None:
        """卡面显示「已允许」却没送回引擎 —— 不许静默，不许报成功。"""
        perm = ToolPermission("per-reply-fail", "edit", ("a.py",))
        host = ScriptedHost(
            [("permission.asked", perm), ("session.idle", {})],
            reply_error=OSError("engine gone"),
        )
        svc = turn_mod.AgentTurnService(
            host, clock=FrozenClock(datetime(2026, 9, 26, 9, 0)),
            reply_sleep=lambda _: None,
        )
        tid = _start(svc)
        assert svc.wait_for_ask(tid, timeout=10) is not None
        assert svc.decide(tid, allow=True) is True
        view = svc.wait(tid, timeout=10)
        assert view is not None
        assert view.status == turn_mod.FAILED
        assert "没能送回引擎" in view.reason
        assert any("没能送回引擎" in n for n in view.notes)
        assert host.aborts == 1
        assert host.replies == []

    def test_answer_reply_failure_fails_the_turn_and_aborts(self) -> None:
        q = ToolQuestion("q-reply-fail", ("用哪个目录？",), ("src",))
        host = ScriptedHost(
            [("question.asked", q), ("session.idle", {})],
            reply_error=OSError("engine gone"),
        )
        svc = turn_mod.AgentTurnService(
            host, clock=FrozenClock(datetime(2026, 9, 26, 9, 0)),
            reply_sleep=lambda _: None,
        )
        tid = _start(svc)
        assert svc.wait_for_ask(tid, timeout=10) is not None
        assert svc.decide(tid, allow=True, answer="src") is True
        view = svc.wait(tid, timeout=10)
        assert view is not None
        assert view.status == turn_mod.FAILED
        assert "没能送回引擎" in view.reason
        assert host.aborts == 1

    def test_unparseable_request_is_denied_not_approved(self) -> None:
        svc, host = _service([("permission.asked", {"weird": True}), ("session.idle", {})])
        view = svc.wait(_start(svc), timeout=10)
        assert view is not None
        assert view.status == turn_mod.FAILED
        assert host.aborts >= 1


class TestStopsAndLimits:
    def test_tool_cap_stops_the_turn(self) -> None:
        script = [("tool", {"tool": "read"}) for _ in range(3)]
        script.append(("session.idle", {}))
        svc, host = _service(script, max_tool_calls=2)
        view = svc.wait(_start(svc), timeout=10)
        assert view is not None
        assert view.status == turn_mod.FAILED
        assert "超限" in view.reason
        assert host.aborts >= 1

    def test_error_event_fails_with_the_engine_message(self) -> None:
        svc, _ = _service([
            ("session.error", {"error": {"data": {"message": "额度用完了"}}}),
        ])
        view = svc.wait(_start(svc), timeout=10)
        assert view is not None
        assert view.status == turn_mod.FAILED
        assert "额度用完了" in view.reason
        assert view.ok is False

    def test_event_stream_is_subscribed_before_prompt(self) -> None:
        # SSE 是 volatile 的：晚一步挂流，启动瞬间就失败的终态帧会漏掉，
        # 轮次只能干等到总时长超时（界面一直转圈、步骤正文全空）。
        svc, host = _service([("session.idle", {})])
        view = svc.wait(_start(svc), timeout=10)
        assert view is not None
        assert host.events_opened is True
        assert host.events_before_prompt is True

    def test_idle_with_failed_outcome_fails_with_engine_message(self) -> None:
        # idle 族里也混着 failed：不看 outcome 就一律记「完成」是静默谎报。
        svc, _ = _service([(
            "session.idle",
            {"outcome": "failed", "error": {"message": "模型不可用：opencode/x"}},
        )])
        view = svc.wait(_start(svc), timeout=10)
        assert view is not None
        assert view.status == turn_mod.FAILED
        assert "模型不可用" in view.reason
        assert view.ok is False

    def test_idle_with_interrupted_outcome_is_stopped(self) -> None:
        svc, _ = _service([("session.idle", {"outcome": "interrupted"})])
        view = svc.wait(_start(svc), timeout=10)
        assert view is not None
        assert view.status == turn_mod.STOPPED

    def test_stream_that_ends_without_idle_is_not_a_success(self) -> None:
        svc, _ = _service([("message.part.delta", {"delta": "看起来干完了"})])
        view = svc.wait(_start(svc), timeout=10)
        assert view is not None
        assert view.status == turn_mod.FAILED
        assert "没有「本轮结束」信号" in view.reason

    def test_stop_while_waiting_for_permission(self) -> None:
        perm = ToolPermission("per-4", "bash", ("ls",))
        svc, host = _service([("permission.asked", perm), ("session.idle", {})])
        tid = _start(svc)
        assert svc.wait_for_ask(tid, timeout=10) is not None
        assert svc.stop(tid) is True
        view = svc.wait(tid, timeout=10)
        assert view is not None
        assert view.status == turn_mod.STOPPED
        assert host.aborts >= 1
        # 停的时候卡还挂着：必须把拒绝送出去，否则引擎一直等到超时。
        assert host.replies == [("per-4", "deny")]

    def test_stop_on_a_finished_turn_is_refused(self) -> None:
        svc, _ = _service([("session.idle", {})])
        tid = _start(svc)
        svc.wait(tid, timeout=10)
        assert svc.stop(tid) is False


class TestSteps:
    """步骤清单（21.3 / V1.115）：计划与进度得能看见到第几步。"""

    @staticmethod
    def _tool_part(name: str, *, status: str | None = None,
                   target: str = "") -> tuple[str, object]:
        state: dict = {}
        if status is not None:
            state["status"] = status
        if target:
            state["input"] = {"filePath": target}
        return ("message.part.updated", {"part": {
            "type": "tool", "tool": name, "state": state}})

    def test_a_tool_part_becomes_a_step_with_its_target(self) -> None:
        svc, _ = _service([self._tool_part("read", status="completed",
                                          target="D:/a.py"), ("session.idle", {})])
        view = svc.wait(_start(svc), timeout=10)
        assert view is not None
        assert len(view.steps) == 1
        assert view.steps[0].name == "read"
        assert view.steps[0].target == "D:/a.py"
        assert view.steps[0].status == turn_mod.STEP_DONE

    def test_started_then_completed_is_one_step_not_two(self) -> None:
        # 同一次调用会来两次（开始、结束）。记两行等于说它把同一件事做了两遍。
        svc, _ = _service([
            self._tool_part("read", status="running", target="D:/a.py"),
            self._tool_part("read", status="completed", target="D:/a.py"),
            ("session.idle", {}),
        ])
        view = svc.wait(_start(svc), timeout=10)
        assert view is not None
        assert len(view.steps) == 1
        assert view.steps[0].status == turn_mod.STEP_DONE

    def test_started_without_a_status_field_is_still_running(self) -> None:
        svc, _ = _service([("tool.started", {"tool": "bash"}), ("session.idle", {})])
        view = svc.wait(_start(svc), timeout=10)
        assert view is not None
        assert view.steps[0].status == turn_mod.STEP_RUNNING

    def test_unknown_status_is_treated_as_running_not_done(self) -> None:
        # 往「还没完」倒：往完成倒的表现是「它说做完了、其实没做完」。
        svc, _ = _service([self._tool_part("edit", status="weird"),
                           ("session.idle", {})])
        view = svc.wait(_start(svc), timeout=10)
        assert view is not None
        assert view.steps[0].status == turn_mod.STEP_RUNNING

    def test_steps_keep_their_order(self) -> None:
        svc, _ = _service([
            self._tool_part("read", status="completed", target="a.py"),
            self._tool_part("edit", status="completed", target="a.py"),
            ("session.idle", {}),
        ])
        view = svc.wait(_start(svc), timeout=10)
        assert view is not None
        assert [s.name for s in view.steps] == ["read", "edit"]
        assert view.tools == ("read", "edit")

    def test_step_updates_do_not_double_count_the_tool_budget(self) -> None:
        # 40 次上限只认 ``tool``：把 ``message.part.updated`` 也算进去，
        # 活干到一半就会被叫停，而日志里看着像真超了。
        svc, _ = _service([self._tool_part("read", status="completed")] * 4
                          + [("session.idle", {})], max_tool_calls=1)
        view = svc.wait(_start(svc), timeout=10)
        assert view is not None
        assert view.status == turn_mod.DONE
        assert view.tool_calls == 0


class TestV2CallIdPairing:
    """V2 真机 2.0.29：name 只在 started、参数在 called、结果在 success/failed，
    三段各带同一个 ``id``。缺一段配对，步骤就会目标全空 / 永远进行中。"""

    def test_started_called_success_merge_into_one_row(self) -> None:
        svc, _ = _service([
            ("tool.started", {"tool": "read", "call_id": "c1"}),
            ("tool", {"call_id": "c1", "state": {"input": {"path": "D:/a.py"}}}),
            ("tool.done", {"call_id": "c1", "state": {"status": "completed"}}),
            ("session.idle", {}),
        ])
        view = svc.wait(_start(svc), timeout=10)
        assert view is not None
        assert len(view.steps) == 1
        step = view.steps[0]
        assert step.name == "read"
        assert step.target == "D:/a.py"
        assert step.status == turn_mod.STEP_DONE
        # 一次调用只计一次（只有 called 是 ``tool``）。
        assert view.tool_calls == 1

    def test_failed_call_is_marked_failed(self) -> None:
        svc, _ = _service([
            ("tool.started", {"tool": "shell", "call_id": "c1"}),
            ("tool", {"call_id": "c1", "state": {"input": {"command": "x"},
                                                  "status": "running"}}),
            ("tool.done", {"call_id": "c1", "state": {"status": "error"}}),
            ("session.idle", {}),
        ])
        view = svc.wait(_start(svc), timeout=10)
        assert view is not None
        assert view.steps[0].status == turn_mod.STEP_FAILED

    def test_done_without_started_and_name_is_not_invented(self) -> None:
        # 完成帧丢了开始帧、又认不出名字：不编一个名字硬列一行。
        svc, _ = _service([
            ("tool.done", {"call_id": "c2", "state": {"status": "completed"}}),
            ("session.idle", {}),
        ])
        view = svc.wait(_start(svc), timeout=10)
        assert view is not None
        assert view.steps == ()


class TestReferenceMarkerStrip:
    def test_dangling_reference_marker_is_removed(self) -> None:
        svc, _ = _service([
            ("message.part.updated", {"part": {"type": "text",
                                               "text": "看完现状 @17@，正在跑测试"}}),
            ("session.idle", {}),
        ])
        view = svc.wait(_start(svc), timeout=10)
        assert view is not None
        assert "@17@" not in view.text
        assert view.text == "看完现状，正在跑测试"


class TestPendingRecovery:
    """实时事件漏掉的挂起授权，靠兜底拉取摆成卡（#49）。"""

    def test_recovery_surfaces_a_permission_missed_on_the_stream(self) -> None:
        perm = ToolPermission("per-9", "bash", ("git diff",))
        svc, host = _service([("session.idle", {})])
        host.pending = [perm]
        tid = _start(svc)
        card = svc.wait_for_ask(tid, timeout=10)
        assert card is not None and card.request_id == "per-9"
        assert host.pending_calls >= 1
        assert svc.decide(tid, allow=True) is True
        view = svc.wait(tid, timeout=10)
        assert view is not None and view.status == turn_mod.DONE
        assert host.replies == [("per-9", "allow")]

    def test_recovery_skips_unparseable_and_already_seen(self) -> None:
        perm = ToolPermission("per-9", "bash", ("git diff",))
        svc, host = _service([("permission.asked", perm), ("session.idle", {})])
        # 同一 rid 已走事件流挂过卡；再来一条认不出的坏载荷：兜底不重复摆、
        # 也不把整轮判失败。
        host.pending = [perm, {"id": "junk"}]
        tid = _start(svc)
        card = svc.wait_for_ask(tid, timeout=10)
        assert card is not None and card.request_id == "per-9"
        assert svc.decide(tid, allow=True) is True
        view = svc.wait(tid, timeout=10)
        assert view is not None and view.status == turn_mod.DONE


class TestArtifactAndCommandFootprint:
    """「它碰过哪些文件 / 跑了哪些命令」（21.5 / V1.116）。

    命令这一列是**安全项**：自动档下命令不弹卡、也不进 `pending_approvals`，
    界面上再不留痕，「它刚才跑了什么」就没人知道。
    """

    @staticmethod
    def _step(name: str, target: str, *, kind: str = "message.part.updated"
              ) -> tuple[str, object]:
        return (kind, {"part": {"type": "tool", "tool": name,
                                "state": {"status": "completed",
                                          "input": {"filePath": target}}}})

    def test_edits_are_listed_once_per_file(self) -> None:
        svc, _ = _service([
            self._step("read", "D:/a.py"),
            self._step("edit", "D:/a.py"),
            self._step("edit", "D:/a.py"),   # 同一文件改两次，只列一行
            self._step("write", "D:/b.py"),
            ("session.idle", {}),
        ])
        view = svc.wait(_start(svc), timeout=10)
        assert view is not None
        assert view.touched_files == ("D:/a.py", "D:/b.py")

    def test_reads_do_not_count_as_touching_a_file(self) -> None:
        svc, _ = _service([self._step("read", "D:/a.py"), ("session.idle", {})])
        view = svc.wait(_start(svc), timeout=10)
        assert view is not None
        assert view.touched_files == ()

    def test_shell_commands_are_listed_verbatim(self) -> None:
        svc, _ = _service([
            ("message.part.updated", {"part": {
                "type": "tool", "tool": "bash",
                "state": {"status": "completed", "input": {"command": "pytest -q"}}}}),
            ("session.idle", {}),
        ])
        view = svc.wait(_start(svc), timeout=10)
        assert view is not None
        assert view.commands == ("pytest -q",)

    def test_steps_without_a_target_are_left_out(self) -> None:
        # 空一行的「碰过的文件」会被读成「它动了一个我不知道的文件」。
        svc, _ = _service([
            ("message.part.updated", {"part": {"type": "tool", "tool": "edit",
                                               "state": {"status": "completed"}}}),
            ("session.idle", {}),
        ])
        view = svc.wait(_start(svc), timeout=10)
        assert view is not None
        assert view.touched_files == ()


class TestRefusals:
    def test_ensure_failure_is_reported_verbatim(self) -> None:
        svc, _ = _service([], ensure_error=ServerError("opencode 不在 PATH"))
        view = svc.wait(_start(svc), timeout=10)
        assert view is not None
        assert view.status == turn_mod.FAILED
        assert "opencode 不在 PATH" in view.reason

    def test_second_turn_while_one_is_open_is_refused(self) -> None:
        perm = ToolPermission("per-5", "edit", ("a.py",))
        svc, _ = _service([("permission.asked", perm), ("session.idle", {})])
        tid = _start(svc)
        assert svc.wait_for_ask(tid, timeout=10) is not None
        with pytest.raises(ValueError, match="上一轮"):
            svc.start(text="再来一句", directory="D:\\p")
        svc.decide(tid, allow=False)
        svc.wait(tid, timeout=10)

    def test_empty_text_and_unknown_mode_are_refused(self) -> None:
        svc, _ = _service([("session.idle", {})])
        with pytest.raises(ValueError, match="空"):
            svc.start(text="   ", directory="D:\\p")
        with pytest.raises(ValueError, match="授权档"):
            svc.start(text="干活", directory="D:\\p", mode="yolo")

    def test_view_of_an_unknown_turn_is_none(self) -> None:
        svc, _ = _service([])
        assert svc.view("t999") is None
