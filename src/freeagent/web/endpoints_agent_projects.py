"""工作台项目档案端点（设计文档 21.6 / V1.124）。

## 为什么从 :mod:`endpoints_agent_workspace` 拆出来

那个文件原本只碰「目录浏览 + 开一轮」；项目档案（建档 / 选中 / 改名 / 移出）
是**第二种事实**：目录浏览回答「往下点哪个」，项目回答「我在哪个项目里干活」。
250 行体积守卫是拆块的直接原因，口径仍是职责分开。

## 档案与偏好各管一件事

- **档案**（``workbench_projects`` 表）：命名目录 + 可选备注 + 归属轮次，跨会话记得住。
- **偏好**（``workbench.json``）：当前选中哪个项目、上次目录、模型与授权档，便利不是事实。

删项目只删档案记录，磁盘目录与历史轮次都不动 —— 那是人的东西，不是档案的东西。
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

from ..app import App
from ..domain import FreeAgentError
from ..services import engine_host
from .endpoints_feishu import state_home_for

__all__ = [
    "agent_projects", "agent_project_save", "agent_project_select",
    "agent_project_update", "agent_project_delete",
    "load_prefs", "save_prefs",
]

_PREFS_NAME = "workbench.json"


def _prefs_path(app: App) -> pathlib.Path:
    return pathlib.Path(state_home_for(app)) / _PREFS_NAME


def load_prefs(app: App) -> dict[str, Any]:
    try:
        data = json.loads(_prefs_path(app).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_prefs(app: App, data: dict[str, Any]) -> None:
    path = _prefs_path(app)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    except OSError:
        # 存不下不影响这一轮 —— 只是下次要重选。失败理由是「目录写不了」，
        # 与「这句话能不能交给引擎」无关，不该让请求失败。
        return


def _project_repo(app: App) -> Any:
    """老装配 / 测试替身可能没挂仓储；拿不到时退回「只记偏好」，不装作有档案。"""
    return getattr(app, "project_repo", None)


def _project_out(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "directory": row["directory"],
        "note": row.get("note") or "",
        "created_at": row["created_at"],
        "last_used_at": row["last_used_at"],
    }


def _leaf_name(directory: str) -> str:
    """项目名缺省时取目录末段；根目录退回到整段路径，不给空名。"""
    return pathlib.Path(directory).name or directory


def agent_project_recent(app: App, limit: int = 5) -> list[dict[str, str]]:
    """浏览面板的「最近目录」：档案按最近使用倒序，目录去重，只回名称与路径。

    这是便利入口，不是白名单 —— 点下去仍走同一个目录校验端点。
    """
    repo = _project_repo(app)
    if repo is None:
        return []
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for row in repo.list_all():
        directory = str(row.get("directory") or "")
        if not directory or directory in seen:
            continue
        seen.add(directory)
        out.append({"label": str(row.get("name") or _leaf_name(directory)),
                    "path": directory})
        if len(out) >= limit:
            break
    return out


def agent_projects(app: App) -> dict[str, Any]:
    """``GET /api/agent/projects`` —— 项目清单 + 当前选中的项目。只读。

    每项多带一个 ``turns``：这个目录上一共跑过几轮（V1.139 左栏用）。
    **读不到回 ``None`` 而不是 0** —— 「还没跑过」和「读不出来」在界面上
    是两句话，把后者写成 0 就等于替人下了结论。
    """
    repo = _project_repo(app)
    turns_repo = getattr(app, "turn_repo", None)
    prefs = load_prefs(app)
    projects: list[dict[str, Any]] = []
    for row in (repo.list_all() if repo else []):
        out = _project_out(row)
        out["turns"] = (
            turns_repo.count_by_directory(str(out["directory"]))
            if turns_repo is not None else None
        )
        projects.append(out)
    return {
        "kind": "agent_projects",
        "projects": projects,
        "project_id": prefs.get("project_id", ""),
    }


def agent_project_save(app: App, body: dict[str, Any]) -> dict[str, Any]:
    """``POST`` 建档或导入本地文件夹。目录先过 ``resolve_workdir`` 现校验。

    按目录幂等：同一个文件夹再存一次只是刷新最近使用，不会长出第二行。
    """
    directory = str(body.get("directory") or "").strip()
    if not directory:
        raise FreeAgentError("先选一个本地文件夹")
    try:
        resolved = engine_host.resolve_workdir(directory)
    except ValueError as exc:
        raise FreeAgentError(str(exc)) from exc
    repo = _project_repo(app)
    name = str(body.get("name") or "").strip() or _leaf_name(resolved)
    note = str(body.get("note") or "").strip()
    if repo is None:
        # 没有仓储的装配下，至少把目录记进偏好；档案能力如实缺席。
        prefs = load_prefs(app)
        save_prefs(app, {**prefs, "directory": resolved})
        return {"kind": "agent_project", "project": None, "directory": resolved}
    row = repo.register(directory=resolved, name=name, note=note,
                        now=app.clock.now())
    prefs = load_prefs(app)
    save_prefs(app, {**prefs, "directory": resolved,
                     "project_id": str(row["id"])})
    return {"kind": "agent_project", "project": _project_out(row)}


def agent_project_select(app: App, body: dict[str, Any]) -> dict[str, Any]:
    """``POST`` 选中某个项目；偏好跟着切到它的目录。``id`` 为空 = 取消选中。"""
    repo = _project_repo(app)
    project_id = str(body.get("id") or "").strip()
    prefs = load_prefs(app)
    if not project_id:
        # 不使用项目：只清偏好里的选中关系，档案与历史轮次都不动。
        prefs.pop("project_id", None)
        prefs.pop("directory", None)
        save_prefs(app, prefs)
        return {"kind": "agent_project", "project": None}
    if repo is None:
        raise FreeAgentError("没有可选的项目")
    row = repo.get(project_id)
    if row is None:
        raise FreeAgentError("这个项目已经不在档案里了 —— 刷新一下列表")
    repo.touch(project_id, app.clock.now())
    save_prefs(app, {**prefs, "directory": str(row["directory"]),
                     "project_id": project_id})
    return {"kind": "agent_project", "project": _project_out(row)}


def agent_project_update(app: App, body: dict[str, Any]) -> dict[str, Any]:
    """``POST`` 改项目名 / 备注；目录不动（换目录等于另建一个项目）。"""
    repo = _project_repo(app)
    project_id = str(body.get("id") or "").strip()
    if repo is None or not project_id:
        raise FreeAgentError("没说改哪个项目")
    existing = repo.get(project_id)
    if existing is None:
        raise FreeAgentError("这个项目已经不在档案里了")
    name = str(body.get("name") or "").strip() or str(existing["name"])
    note = str(body.get("note") or "").strip()
    row = repo.rename(project_id, name, note)
    if row is None:
        raise FreeAgentError("保存项目修改失败")
    return {"kind": "agent_project", "project": _project_out(row)}


def agent_project_delete(app: App, body: dict[str, Any]) -> dict[str, Any]:
    """``POST`` 移出档案。**只删记录**，磁盘目录与历史轮次都不动。"""
    repo = _project_repo(app)
    project_id = str(body.get("id") or "").strip()
    if repo is None or not project_id:
        raise FreeAgentError("没说删哪个项目")
    repo.delete(project_id)
    prefs = load_prefs(app)
    if prefs.get("project_id") == project_id:
        prefs.pop("project_id", None)
        save_prefs(app, prefs)
    return {"kind": "agent_project_deleted", "id": project_id}
