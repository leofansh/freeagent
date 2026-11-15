"""工作台端点（21.4 / V1.114）的形状守卫。

服务层有 44 条测试（`test_engine_host` / `test_agent_router` /
`test_agent_turn`），但**端点这一层此前没人守**：真起 HTTP 服务去测要拖一个
引擎进来（`test_prompt_payload` 就是这么变成环境类红点的）。这里改成直接调
端点函数 —— 守的是**接口形状**（返回什么、拒绝什么），不是浏览器里长什么样。

三条断言的理由：

1. **判为「不用动手」时一轮都不许起** —— 这是「自主判断」的全部价值。若端点
   还是先 `start()` 再问，规则就成了装饰。
2. **`force` 要能推翻规则** —— 规则可被反驳，否则人只能绕过界面去终端。
3. **不认识的 POST 动作要报，不许静默 200** —— 静默的后果是界面「点了没反应」，
   而那正是本仓 12.7.2 反复治的那类症状。
"""

from __future__ import annotations

import threading
import types

import pytest

from freeagent.domain import FreeAgentError
from freeagent.services import agent_turn
from freeagent.web import endpoints_agent, endpoints_agent_workspace


class _FakeTurns:
    """记下有没有人被叫起来 —— 断言「没起」比断言返回体更关键。"""

    def __init__(self, *, view: agent_turn.TurnView | None = None):
        self.started: list[dict] = []
        self.decided: list[tuple] = []
        self.stopped: list[str] = []
        self._view = view

    def start(self, **kwargs):
        self.started.append(kwargs)
        return self._view or agent_turn.TurnView(
            turn_id="t1", status="running", **{k: v for k, v in kwargs.items()
                                              if k in ("text", "directory", "model")})

    def latest(self):
        return self._view

    def view(self, turn_id):
        return self._view if (self._view and turn_id == self._view.turn_id) else None

    def decide(self, turn_id, *, allow, answer=""):
        self.decided.append((turn_id, allow, answer))
        return turn_id == "t1"

    def stop(self, turn_id):
        self.stopped.append(turn_id)
        return turn_id == "t1"


def _app(tmp_path, turns=None):
    """假 app。``lock`` 用 ``RLock`` —— 与真的 ``App.lock`` 同类型。

    曾经这里写的是普通 ``Lock``，结果 ``_turns`` 里再取一次就**死锁**：
    测试挂住、没有报错，看起来像「端点很慢」。这也正是真 ``App`` 的
    那个类型选择为什么值得在注释里留住。
    """
    return types.SimpleNamespace(
        lock=threading.RLock(),
        engine=None,
        agent_turns=turns,
        config=types.SimpleNamespace(home=str(tmp_path)),
    )


class TestStateAndTurn:
    def test_state_reports_stopped_engine_and_the_two_modes(self, tmp_path):
        """没打开工作台时引擎该是 stopped —— 打开网页不等于同意起进程。"""
        app = _app(tmp_path)
        data = endpoints_agent.agent_state(app)
        assert data["kind"] == "agent_state"
        assert data["engine"]["status"] == "stopped"
        assert data["engine"]["pid"] == 0
        assert data["turn"] is None
        assert [m["key"] for m in data["modes"]] == ["ask", "auto_dir"]

    def test_prefs_survive_a_round_trip(self, tmp_path):
        app = _app(tmp_path)
        endpoints_agent_workspace.save_prefs(app, {"directory": "D:/x", "mode": "ask"})
        assert endpoints_agent_workspace.load_prefs(app)["directory"] == "D:/x"

    def test_unreadable_prefs_are_treated_as_no_prefs(self, tmp_path):
        """写坏了也不许让工作台打不开 —— 它不是事实，是便利。"""
        app = _app(tmp_path)
        (tmp_path / "workbench.json").write_text("{坏掉的 json", encoding="utf-8")
        assert endpoints_agent_workspace.load_prefs(app) == {}

    def test_unknown_turn_id_says_so(self, tmp_path):
        app = _app(tmp_path, _FakeTurns())
        with pytest.raises(FreeAgentError, match="没有这一轮"):
            endpoints_agent.agent_turn_get(app, {"id": ["t99"]})


class TestDirs:
    def test_lists_only_directories(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "a.txt").write_text("x", encoding="utf-8")
        data = endpoints_agent_workspace.agent_dirs(
            _app(tmp_path), {"path": [str(tmp_path)]})
        assert data["kind"] == "agent_dirs"
        assert data["items"] == ["src"]

    def test_relative_path_is_refused(self, tmp_path):
        """相对路径的解析基准随通道而变 —— 闸门会因此判错目录。"""
        with pytest.raises(FreeAgentError, match="绝对路径"):
            endpoints_agent_workspace.agent_dirs(_app(tmp_path), {"path": ["src"]})


class TestStart:
    def test_no_directory_is_refused(self, tmp_path):
        turns = _FakeTurns()
        with pytest.raises(FreeAgentError, match="工作目录"):
            endpoints_agent_workspace.agent_start(
                _app(tmp_path, turns), {"text": "改一下 README"}, turns)

    def test_local_question_does_not_start_a_turn(self, tmp_path):
        """判为不用动手 → 一轮都不许起，且要说清理由。"""
        turns = _FakeTurns()
        data = endpoints_agent_workspace.agent_start(
            _app(tmp_path, turns),
            {"text": "今天我还有什么事", "directory": str(tmp_path)}, turns)
        assert data["routed"] is False
        assert data["turn"] is None
        assert data["decision"]["needs_engine"] is False
        assert data["decision"]["reason"]
        assert turns.started == [], "规则说不用动手，却把引擎叫起来了"

    def test_path_in_the_sentence_starts_a_turn(self, tmp_path):
        turns = _FakeTurns()
        data = endpoints_agent_workspace.agent_start(
            _app(tmp_path, turns),
            {"text": "把 src/freeagent/app.py 里的字段顺序改一下",
             "directory": str(tmp_path)}, turns)
        assert data["routed"] is True
        assert len(turns.started) == 1
        assert data["turn"]["status"] == "running"
        assert data["decision"]["reason"]

    def test_force_overrides_the_rule_and_says_so(self, tmp_path):
        turns = _FakeTurns()
        data = endpoints_agent_workspace.agent_start(
            _app(tmp_path, turns),
            {"text": "今天我还有什么事", "directory": str(tmp_path),
             "force": True}, turns)
        assert data["routed"] is True
        assert "强制" in data["decision"]["reason"], "推翻规则必须留痕"


class TestContextInjection:
    """上下文注入的端点这一半（21.5 / V1.115）。"""

    def test_off_switch_says_its_own_sentence(self, tmp_path):
        turns = _FakeTurns()
        endpoints_agent_workspace.agent_start(
            _app(tmp_path, turns),
            {"text": "把 src/freeagent/app.py 里的字段顺序改一下",
             "directory": str(tmp_path), "inject": False}, turns)
        call = turns.started[0]
        assert call["context"] == "", "关掉了就别偷偷带上"
        # 空串会被显示成「这一版没这个能力」—— 那与「你关掉了」是两件事。
        assert "关掉了" in call["context_note"]

    def test_a_broken_read_is_reported_not_silently_dropped(self, tmp_path):
        # 假 app 没有 today / promise / memory —— 读上下文必然抛，端点
        # 必须把它说成「没读到某某」，不许吞成「今天没事」。
        turns = _FakeTurns()
        endpoints_agent_workspace.agent_start(
            _app(tmp_path, turns),
            {"text": "把 src/freeagent/app.py 里的字段顺序改一下",
             "directory": str(tmp_path)}, turns)
        note = turns.started[0]["context_note"]
        assert "没读到：今天的活" in note
        assert "has no attribute 'today'" in note, "失败要给得出原因"
        assert turns.started[0]["context"] == "", "读不到就别带半截"

    def test_todays_tasks_are_carried_and_counted(self, tmp_path):
        turns = _FakeTurns()
        app = _app(tmp_path, turns)
        app.today = types.SimpleNamespace(
            scheduled_for=lambda day: [types.SimpleNamespace(
                task=types.SimpleNamespace(title="写周报"))])
        app.clock = types.SimpleNamespace(today=lambda: "2026-11-16")
        app.promise = types.SimpleNamespace(open_promises=lambda: [])
        app.memory = types.SimpleNamespace(
            retrieve=lambda *a, **kw: types.SimpleNamespace(hits=(), degraded=False))
        endpoints_agent_workspace.agent_start(
            app, {"text": "把 src/freeagent/app.py 里的字段顺序改一下",
                  "directory": str(tmp_path)}, turns)
        assert "写周报" in turns.started[0]["context"]
        assert "今天的活 1 条" in turns.started[0]["context_note"]


class TestPost:
    def test_unknown_action_is_refused(self, tmp_path):
        with pytest.raises(FreeAgentError, match="不认识的动作"):
            endpoints_agent.agent_post(_app(tmp_path, _FakeTurns()), "explode",
                                       lambda **kw: {})

    def test_decide_and_stop_go_through_the_table(self, tmp_path):
        turns = _FakeTurns()
        app = _app(tmp_path, turns)
        body = {"id": "t1", "allow": True, "answer": ""}
        endpoints_agent.agent_post(app, "decide", lambda **kw: body)
        endpoints_agent.agent_post(app, "stop", lambda **kw: {"id": "t1"})
        assert turns.decided == [("t1", True, "")]
        assert turns.stopped == ["t1"]

    def test_decide_on_a_finished_turn_explains_why_not(self, tmp_path):
        turns = _FakeTurns()
        app = _app(tmp_path, turns)
        with pytest.raises(FreeAgentError, match="没有待回答的卡"):
            endpoints_agent.agent_post(app, "decide",
                                       lambda **kw: {"id": "t9", "allow": True})


class _TurnsWithHistory:
    """只带历史能力的最小替身（端点只调 ``history`` / ``has_history``）。"""

    def __init__(self, views: tuple[agent_turn.TurnView, ...], *, available=True):
        self._views = views
        self._available = available

    def history(self, limit: int):
        return self._views[:limit]

    def has_history(self) -> bool:
        return self._available


class TestTurnsList:
    def test_without_history_capability_it_says_so(self, tmp_path):
        """「这台机器没有历史能力」与「历史是空的」是两件事。"""
        turns = _TurnsWithHistory((), available=False)
        data = endpoints_agent.agent_turns_list(_app(tmp_path, turns), {})
        assert data["kind"] == "agent_turns"
        assert data["available"] is False
        assert data["turns"] == []

    def test_items_carry_what_a_list_needs_and_nothing_more(self, tmp_path):
        view = agent_turn.TurnView(
            turn_id="w1", status="done", brief="改一下 app.py",
            started_at="2026-09-26T09:00:00", elapsed=12.5, tool_calls=3,
            directory="D:\\p", model="m", ok=True,
        )
        data = endpoints_agent.agent_turns_list(
            _app(tmp_path, _TurnsWithHistory((view,))), {})
        assert data["available"] is True
        item = data["turns"][0]
        # 列表要的是「这是哪件事、成没成、多久」；正文不进列表 ——
        # 二十条长文本会把列表变成一堵墙。
        assert item["brief"] == "改一下 app.py"
        assert item["status"] == "done"
        assert item["elapsed"] == 12.5
        assert "text" not in item

    def test_limit_is_clamped(self, tmp_path):
        views = tuple(agent_turn.TurnView(turn_id=f"w{i}", status="done")
                      for i in range(30))
        data = endpoints_agent.agent_turns_list(
            _app(tmp_path, _TurnsWithHistory(views)), {"limit": ["999"]})
        assert len(data["turns"]) == 30, "上限应被夹到 50 以内，而不是报错"
        data = endpoints_agent.agent_turns_list(
            _app(tmp_path, _TurnsWithHistory(views)), {"limit": ["abc"]})
        assert len(data["turns"]) == 20, "读不懂的 limit 回落到 20，不猜"


class TestFootprint:
    """一轮的「事后可见」（21.5 / V1.116）：改了哪些文件、跑了哪些命令。"""

    def test_turn_dict_carries_files_and_commands(self, tmp_path):
        view = agent_turn.TurnView(
            turn_id="w1", status="done",
            steps=(
                agent_turn.StepView("read", "D:/a.py", agent_turn.STEP_DONE),
                agent_turn.StepView("edit", "D:/a.py", agent_turn.STEP_DONE),
                agent_turn.StepView("bash", "pytest -q", agent_turn.STEP_DONE),
            ),
        )
        data = endpoints_agent.turn_dict(view)
        assert data["files"] == ["D:/a.py"]
        assert data["commands"] == ["pytest -q"]

    def test_a_turn_without_tools_reports_empty_lists_not_none(self):
        # 空列表 = 「这一轮没动文件」；None 会被前端读成「这一版没这个能力」，
        # 而那是两句话。
        data = endpoints_agent.turn_dict(
            agent_turn.TurnView(turn_id="w1", status="done"))
        assert data["files"] == []
        assert data["commands"] == []
