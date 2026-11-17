"""工作台的状态与「答卡 / 停止」端点（设计文档 21.4 / V1.114）。

## 为什么单独一个模块

``endpoints_read`` 已顶在 250 行上限；工作台又有三种读写姿态（只读快照 /
开一轮 / 答一张卡）。目录与「开一轮」在 :mod:`endpoints_agent_workspace`，
这里管**状态机这一侧**：引擎与一轮此刻是什么样、人点了什么。

## GET 与 POST 的分工

GET 只读快照：鼠标划过去、爬虫顺手抓一下，**都不该起引擎**。
POST 才动手 —— 判据与全仓一致（见 :mod:`endpoints_services` 的模块说明）。
"""

from __future__ import annotations

from typing import Any, Callable

from ..app import App
from ..domain import FreeAgentError
from ..services import agent_turn, engine_host
from . import endpoints_agent_workspace as workspace

__all__ = ["agent_state", "agent_turn_get", "agent_turns_list", "agent_decide",
           "agent_stop", "agent_post", "engine_dict", "turn_dict"]


def _host(app: App) -> Any:
    with app.lock:
        if app.engine is None:
            app.engine = engine_host.EngineHost()
        return app.engine


def _turns(app: App) -> Any:
    with app.lock:
        if app.agent_turns is None:
            # 这里**不**调 ``_host(app)``：那会在同一把锁上再取一次，
            # 而 ``App.lock`` 只是「现在是 RLock」这个偶然事实在兜底。
            # 能在锁里顺手做掉的事，就别指望上层锁可重入。
            if app.engine is None:
                app.engine = engine_host.EngineHost()
            # ``repo`` 是「历史能回访」的那一半（V1.115）：有了它，一轮在
            # 结束时会落库、重启后还能点开；没有它（老装配 / 测试替身）
            # 就退回 V1.114 的纯内存行为，**不装作有历史**。
            app.agent_turns = agent_turn.AgentTurnService(
                app.engine, repo=getattr(app, "turn_repo", None),
            )
        return app.agent_turns


def engine_dict(state: engine_host.EngineState) -> dict[str, Any]:
    return {
        "status": state.status,
        "directory": state.directory,
        "session_id": state.session_id,
        "model": state.model,
        "agent": state.agent,
        "variant": state.variant,
        "mode": state.mode,
        "pid": state.pid,
        "error": state.error,
        "started_at": state.started_at,
    }


def turn_dict(view: agent_turn.TurnView | None) -> dict[str, Any] | None:
    if view is None:
        return None
    return {
        "id": view.turn_id,
        "status": view.status,
        "text": view.text,
        "brief": view.brief,
        # 这一轮带了什么背景（V1.115）：界面要能说「它知道我今天有 3 件事」，
        # 否则「它怎么没看见我刚记的承诺」永远没有答案。
        "context_note": view.context_note,
        "tools": list(view.tools),
        # 逐步清单（V1.115）：``name`` / ``target`` / ``status`` 三样够了 ——
        # 多给的字段界面用不上，反而会让「这一格该谁来算」变成两处。
        "steps": [
            {"name": s.name, "target": s.target, "status": s.status}
            for s in view.steps
        ],
        "tool_calls": view.tool_calls,
        # 从 ``steps`` 现算的两份「事后可见」（V1.116）：它改了哪些文件、
        # 跑了哪些命令。**命令尤其重要** —— 自动档下命令不弹卡，
        # 界面上再不说，人只能翻引擎自己的日志。
        "files": list(view.touched_files),
        "commands": list(view.commands),
        "reason": view.reason,
        "notes": list(view.notes),
        "ok": view.ok,
        "directory": view.directory,
        "model": view.model,
        "mode": view.mode,
        "started_at": view.started_at,
        "elapsed": view.elapsed,
        # 授权卡与提问卡**不同形**：前者两个按钮，后者要一个输入框。
        # 合成一个字段、让前端猜，会养出「点允许但没答案」的空答复。
        "ask": None if view.ask is None else {
            "request_id": view.ask.request_id,
            "kind": view.ask.kind,
            "label": view.ask.label,
            "detail": view.ask.detail,
        },
    }


def agent_state(app: App) -> dict[str, Any]:
    """``GET /api/agent/state`` —— 引擎状态 + 最近一轮 + 上次的选择。"""
    prefs = workspace.load_prefs(app)
    projects = workspace.agent_projects(app)
    return {
        "kind": "agent_state",
        "engine": engine_dict(_host(app).state()),
        "turn": turn_dict(_turns(app).latest()),
        "projects": projects["projects"],
        "prefs": {
            "directory": prefs.get("directory", ""),
            "project_id": prefs.get("project_id", ""),
            "model": prefs.get("model", ""),
            "agent": prefs.get("agent", ""),
            "variant": prefs.get("variant", ""),
            "mode": prefs.get("mode", engine_host.ENGINE_ASK),
        },
        "modes": [
            {"key": engine_host.ENGINE_ASK, "label": "逐次授权（默认）",
             "note": "改文件、跑命令都要你点一下"},
            {"key": engine_host.ENGINE_AUTO_DIR, "label": "本目录内自动",
             "note": "只放宽写文件与 shell；越界、子代理、出网仍然拦"},
        ],
    }


def agent_turn_get(app: App, q: dict[str, list[str]]) -> dict[str, Any]:
    """``GET /api/agent/turn?id=`` —— 一轮的快照（不带 id 取最近一轮）。"""
    turns = _turns(app)
    turn_id = (q.get("id") or [""])[0].strip()
    view = turns.view(turn_id) if turn_id else turns.latest()
    if turn_id and view is None:
        raise FreeAgentError(f"没有这一轮（{turn_id}）—— 可能已过期")
    return {"kind": "agent_turn", "turn": turn_dict(view)}


def agent_turns_list(app: App, q: dict[str, list[str]]) -> dict[str, Any]:
    """``GET /api/agent/turns`` —— 最近若干轮，供「最近的活」列表。

    只给**列表需要的字段**（正文不进列表：一页二十条的长文本会把列表变成
    一堵墙）。点开某一条时再由 ``/api/agent/turn?id`` 取全文。

    ``available`` 是「这台机器上有没有历史能力」，不是「历史里有没有东西」：
    前者缺了要说清（没落库的装配下重启就丢），后者空着只是还没干过活。
    """
    turns = _turns(app)
    try:
        limit = int((q.get("limit") or ["20"])[0])
    except ValueError:
        limit = 20
    limit = max(1, min(50, limit))
    items = [
        {
            "id": v.turn_id,
            "brief": v.brief,
            "status": v.status,
            "started_at": v.started_at,
            "elapsed": v.elapsed,
            "tool_calls": v.tool_calls,
            "directory": v.directory,
            "model": v.model,
            "ok": v.ok,
            "reason": v.reason,
        }
        for v in turns.history(limit)
    ]
    return {"kind": "agent_turns", "available": turns.has_history(),
            "turns": items}


def agent_decide(app: App, body: dict[str, Any]) -> dict[str, Any]:
    """``POST /api/agent/decide`` —— 答授权卡 / 回答提问。"""
    turn_id = str(body.get("id") or "").strip()
    if not turn_id:
        raise FreeAgentError("没说答的是哪一轮")
    allow = bool(body.get("allow"))
    answer = str(body.get("answer") or "").strip()
    # 只记账、不在这里发请求：把一次网络往返挂在服务端锁上，症状正是
    # 「点了没反应」（12.7.2）。真正的答复由执行线程发。
    if not _turns(app).decide(turn_id, allow=allow, answer=answer):
        raise FreeAgentError(
            "这一轮现在没有待回答的卡 —— 可能它已经结束了，或卡已过期")
    return {"kind": "agent_decide", "id": turn_id, "allow": allow}


def agent_stop(app: App, body: dict[str, Any]) -> dict[str, Any]:
    """``POST /api/agent/stop`` —— 停一轮（**并真的叫停引擎**）。"""
    turn_id = str(body.get("id") or "").strip()
    if not turn_id:
        raise FreeAgentError("没说停的是哪一轮")
    if not _turns(app).stop(turn_id):
        raise FreeAgentError("这一轮已经结束了，没有可停的东西")
    return {"kind": "agent_stop", "id": turn_id}


#: ``POST /api/agent/<动作>`` 的派发表。做成表而不是三段 if：动作会继续加
#: （将来还要「接着上一句说」），而 ``routes_write.py`` 已经贴着行数上限。
_ACTIONS: dict[str, Callable[[App, dict[str, Any]], dict[str, Any]]] = {
    "start": lambda app, body: workspace.agent_start(app, body, _turns(app)),
    "decide": agent_decide,
    "stop": agent_stop,
    "project_save": lambda app, body: workspace.agent_project_save(app, body),
    "project_select": lambda app, body: workspace.agent_project_select(app, body),
    "project_update": lambda app, body: workspace.agent_project_update(app, body),
    "project_delete": lambda app, body: workspace.agent_project_delete(app, body),
}


def agent_post(app: App, action: str, read_body: Callable[..., dict[str, Any]]
               ) -> dict[str, Any]:
    """``POST /api/agent/<action>`` 的唯一入口。"""
    handler = _ACTIONS.get(action)
    if handler is None:
        raise FreeAgentError(
            f"不认识的动作：{action!r}"
            "（可选：start/decide/stop/project_save/project_select/"
            "project_update/project_delete）")
    return handler(app, read_body())
