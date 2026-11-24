"""多会话端点（V1.176；设计文档 21.x）。

## 为什么独立模块

``endpoints_agent`` 恰好贴着 250 行体积上限（``TestWebPackageStaysSmall`` 守着），
会话这组动作加进去必然溢出。按职责拆：那个文件管「一轮怎么跑」，这里管
「哪一轮属于哪个会话」。POST 动作经它的 ``_ACTIONS`` 合并进来，仍是同一入口。

## 会话 id 与引擎会话

真实会话的 id 直接来自引擎 ``create_session``（V1.97 实测旧 id 跨进程仍被接受），
所以切会话只换字符串、不重建服务 —— 一个进程里其它会话都还活着。
唯一例外是 localStorage 迁移来的历史会话（``hist:`` 前缀）：它没有引擎对应物，
切过去时清空当前 id，让 ``ensure`` 起服务时新建干净的引擎会话；绝不拿假 id 去
``prompt``，那等于指向一个不存在的会话。
"""

from __future__ import annotations

import os
from typing import Any

from ..app import App
from ..domain import FreeAgentError, new_id
from ..services import engine_host
from .endpoints_agent_projects import load_prefs, save_prefs

__all__ = ["agent_sessions", "sessions_for_prefs", "ACTIONS"]

_HISTORY_PREFIX = "hist:"
_NO_TABLE = "这个库还没有会话表 —— 升级到 v36 再试"


def _repo(app: App) -> Any:
    repo = getattr(app, "chat_session_repo", None)
    if repo is None:
        raise FreeAgentError(_NO_TABLE)
    return repo


def _directory(app: App) -> str:
    directory = str(load_prefs(app).get("directory") or "").strip()
    if not directory:
        raise FreeAgentError("还没选工作目录 —— 先选一个项目或目录")
    return directory


def _same_dir(a: str, b: str) -> bool:
    """Windows 大小写不敏感，末尾斜杠也不该把同一目录判成两个。"""
    return (os.path.normcase(os.path.normpath(a))
            == os.path.normcase(os.path.normpath(b)))


def _session_out(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "directory": row["directory"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _owned(app: App, sid: str) -> dict[str, Any]:
    """取一条属于当前目录的会话 —— 目录不符就拒，防串味。"""
    if not sid:
        raise FreeAgentError("会话 id 不能为空 —— 不猜")
    row = _repo(app).get(sid)
    if row is None:
        raise FreeAgentError("找不到这个会话")
    if not _same_dir(str(row["directory"]), _directory(app)):
        raise FreeAgentError("这个会话属于另一个目录 —— 不能拿过来用")
    return row


def _host(app: App) -> Any:
    # 延迟 import：endpoints_agent 在模块级 import 本模块，反着 import 成环。
    # 复用它的懒造实例，保证与派活共用同一个 EngineHost（一处状态一个真源）。
    from .endpoints_agent import _host as agent_host

    return agent_host(app)


def _merge_prefs(app: App, **fields: Any) -> None:
    """save_prefs 是全量覆盖写的：任何单字段更新都必须先 load 合并（V1.174）。"""
    prefs = load_prefs(app)
    prefs.update(fields)
    save_prefs(app, prefs)


def _sessions_in(app: App, directory: str) -> dict[str, Any]:
    repo = getattr(app, "chat_session_repo", None)
    if repo is None:
        return {"available": False, "reason": _NO_TABLE,
                "directory": directory, "sessions": []}
    try:
        rows = repo.list_by_directory(directory)
    except Exception as exc:  # noqa: BLE001 - 读不到要自报，不拖垮整个状态页
        return {"available": False, "reason": f"会话读不到：{exc}",
                "directory": directory, "sessions": []}
    return {"available": True, "reason": "", "directory": directory,
            "sessions": [_session_out(r) for r in rows]}


def sessions_for_prefs(app: App) -> dict[str, Any]:
    """``agent_state`` 里的会话块：读不到给原因，绝不拖垮状态页。"""
    try:
        directory = _directory(app)
    except FreeAgentError as exc:
        return {"available": False, "reason": str(exc),
                "directory": "", "sessions": []}
    return _sessions_in(app, directory)


def agent_sessions(app: App, q: dict[str, list[str]]) -> dict[str, Any]:
    """``GET /api/agent/sessions``；带 directory 时列那个目录的会话。"""
    directory = (q.get("directory") or [""])[0].strip()
    if not directory:
        return {"kind": "agent_sessions", **sessions_for_prefs(app)}
    return {"kind": "agent_sessions", **_sessions_in(app, directory)}


def _session_new(app: App, body: dict[str, Any]) -> dict[str, Any]:
    directory = str(body.get("directory") or _directory(app)).strip()
    if not directory:
        raise FreeAgentError("还没选工作目录 —— 先选一个项目或目录")
    try:
        resolved = engine_host.resolve_workdir(directory)
    except ValueError as exc:
        raise FreeAgentError(str(exc)) from exc
    prefs = load_prefs(app)
    host = _host(app)
    host.ensure(
        directory=resolved,
        mode=str(prefs.get("mode") or engine_host.ENGINE_ASK),
        model=str(prefs.get("model") or ""),
        agent=str(prefs.get("agent") or ""),
        variant=str(prefs.get("variant") or ""),
    )
    sid = host.new_session()
    title = str(body.get("title") or "").strip() or "新会话"
    row = _repo(app).create(
        session_id=sid, directory=resolved, title=title, now=app.clock.now(),
    )
    _merge_prefs(app, session_id=sid)
    return {"kind": "agent_session_new", "session": _session_out(row)}


def _session_switch(app: App, body: dict[str, Any]) -> dict[str, Any]:
    sid = str(body.get("id") or "").strip()
    row = _owned(app, sid)
    _host(app).switch_session(sid)
    _merge_prefs(app, session_id=sid)
    return {"kind": "agent_session_switch", "session": _session_out(row)}


def _session_rename(app: App, body: dict[str, Any]) -> dict[str, Any]:
    sid = str(body.get("id") or "").strip()
    _owned(app, sid)
    title = str(body.get("title") or "").strip()
    if not title:
        raise FreeAgentError("会话名不能为空")
    row = _repo(app).rename(sid, title, app.clock.now())
    if row is None:
        raise FreeAgentError("会话已经不在了 —— 刷新再看")
    return {"kind": "agent_session_rename", "session": _session_out(row)}


def _session_delete(app: App, body: dict[str, Any]) -> dict[str, Any]:
    sid = str(body.get("id") or "").strip()
    _owned(app, sid)
    # 删会话不删轮次：轮次里的 session_id 留着，读侧按历史会话兜底展示。
    _repo(app).delete(sid)
    prefs = load_prefs(app)
    if str(prefs.get("session_id") or "") == sid:
        prefs.pop("session_id", None)
        save_prefs(app, prefs)
    return {"kind": "agent_session_delete", "id": sid}


def _session_import(app: App, body: dict[str, Any]) -> dict[str, Any]:
    """把 localStorage 里的老对话承接成一条会话（V1.176 历史迁移）。

    ``hist:`` 前缀的 id 没有引擎对应物；``EngineHost.switch_session`` 见到它会
    清空当前引擎会话，让 ensure 起服务时新建干净的会话 —— 绝不拿假 id 去
    prompt，那等于指向一个不存在的会话。
    """
    directory = str(body.get("directory") or _directory(app)).strip()
    title = str(body.get("title") or "").strip() or "历史会话"
    sid = _HISTORY_PREFIX + new_id()
    row = _repo(app).create(
        session_id=sid, directory=directory, title=title, now=app.clock.now(),
    )
    return {"kind": "agent_session_import", "session": _session_out(row)}


ACTIONS = {
    "session_new": _session_new,
    "session_switch": _session_switch,
    "session_rename": _session_rename,
    "session_delete": _session_delete,
    "session_import": _session_import,
}
