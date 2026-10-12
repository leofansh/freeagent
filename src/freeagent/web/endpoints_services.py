"""后台服务的统一控制端点：状态查询与启停重启。

GET 只读、POST 才动手 —— 与全仓一致的理由：能起杀进程的操作不能挂在
「浏览器预取/爬虫顺手 GET 一下就触发」的路径上。会话令牌由 server 层
统一校验，这里不另造鉴权。
"""

from __future__ import annotations

from ..app import App
from ..domain import FreeAgentError
from ..services import svcs

__all__ = ["services", "service_action"]


def _serialize(state: svcs.ServiceState) -> dict:
    return {
        "key": state.key,
        "title": state.title,
        "note": state.note,
        "running": state.running,
        "pid": state.pid,
        "cmdline": state.cmdline,
        "started_by_page": state.started_by_page,
    }


def services(app: App) -> dict:
    """``GET /api/services`` —— 全部服务的当前状态。"""
    try:
        items = [_serialize(s) for s in svcs.list_states()]
    except svcs.ServiceRefused as exc:
        raise FreeAgentError(str(exc)) from exc
    return {"kind": "services", "items": items}


def service_action(app: App, key: str, action: str) -> dict:
    """``POST /api/services/<key>/<action>``。"""
    if action not in ("start", "stop", "restart"):
        raise FreeAgentError(f"不认识的动作：{action!r}（start/stop/restart）")
    try:
        if action == "start":
            state = svcs.start(key)
        elif action == "stop":
            state = svcs.stop(key)
        else:
            state = svcs.restart(key)
    except svcs.ServiceRefused as exc:
        raise FreeAgentError(str(exc)) from exc
    verb = {"start": "已启动", "stop": "已停止", "restart": "已重启"}[action]
    return {
        "kind": "service_action",
        "action": action,
        "message": f"{state.title} {verb}"
                   + (f"（pid {state.pid}）" if state.running and state.pid else ""),
        "service": _serialize(state),
    }
