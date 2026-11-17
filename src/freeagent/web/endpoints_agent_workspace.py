"""工作台的「工作目录」与一次开跑（设计文档 21.4 / V1.114）。

## 为什么从 :mod:`endpoints_agent` 拆出来

那个文件已经顶到 web 包 250 行上限（``TestWebPackageStaysSmall`` 守着）。
拆的口径是**职责**而不是行数：这里只碰两件与「目录」有关的事 ——
列出目录、按这份选择开一轮。状态、答卡、停止在另一个文件里。

## 项目档案在哪

命名目录 + 备注 + 选中关系归 :mod:`endpoints_agent_projects`；这里只在
``agent_start`` 起轮前复用它的仓储与偏好助手，不重复建档逻辑。
"""

from __future__ import annotations

import pathlib
import string
import sys
from typing import Any

from ..app import App
from ..domain import FreeAgentError
from ..services import agent_router, agent_turn, engine_host, workbench_context
from .endpoints_agent_projects import (
    _leaf_name,
    _project_repo,
    agent_project_delete,
    agent_project_save,
    agent_project_select,
    agent_project_update,
    agent_projects,
    load_prefs,
    save_prefs,
)

__all__ = ["agent_dirs", "agent_start", "load_prefs", "save_prefs",
           "agent_projects", "agent_project_save", "agent_project_select",
           "agent_project_update", "agent_project_delete"]

#: 目录列表最多返回多少项。不设上限时，一个 ``node_modules`` 就能把一次
#: GET 拖成几秒 —— 而人只需要知道「往下该点哪个」。
_MAX_DIRS = 300


def _available_roots() -> list[str]:
    """能作为浏览起点的根：Windows 枚存在的盘符，其它平台就一个 ``/``。

    这里只列**存在**的根；列出来的路径点下去仍走 :func:`resolve_workdir`
    校验，枚举本身不是放行依据。
    """
    if sys.platform == "win32":
        roots = [f"{letter}:\\" for letter in string.ascii_uppercase
                 if pathlib.Path(f"{letter}:\\").is_dir()]
        return roots
    return ["/"]


def _quick_places(home: pathlib.Path) -> list[dict[str, str]]:
    """家目录与常见个人目录：只收**真实存在**的，缺哪个不列哪个。

    这是浏览快捷入口，不是白名单 —— 点进去之后照旧过目录校验。
    """
    candidates = [
        ("家目录", home),
        ("桌面", home / "Desktop"),
        ("下载", home / "Downloads"),
        ("文档", home / "Documents"),
    ]
    return [{"label": label, "path": str(path)}
            for label, path in candidates if path.is_dir()]


def agent_dirs(app: App, q: dict[str, list[str]]) -> dict[str, Any]:
    """``GET /api/agent/dirs?path=`` —— 列出某个目录下的子目录。

    这是**唯一**一个能读到工作目录里有什么的端点，所以两条约束写死：
    只列**目录**（不读文件内容、不递归），且最多 :data:`_MAX_DIRS` 项。
    它回答「往下该点哪个」，不是「这个目录里有什么」。
    """
    raw = (q.get("path") or [""])[0].strip()
    if not raw:
        raw = str(load_prefs(app).get("directory") or pathlib.Path.home())
    try:
        resolved = engine_host.resolve_workdir(raw)
    except ValueError as exc:
        raise FreeAgentError(str(exc)) from exc
    path = pathlib.Path(resolved)
    try:
        entries = [d.name for d in path.iterdir() if d.is_dir()]
    except OSError as exc:
        raise FreeAgentError(f"读不出这个目录：{path}（{exc}）") from exc
    entries.sort(key=str.lower)
    truncated = len(entries) > _MAX_DIRS
    return {
        "kind": "agent_dirs",
        "path": str(path),
        "parent": "" if path.parent == path else str(path.parent),
        "items": entries[:_MAX_DIRS],
        "truncated": truncated,
        "total": len(entries),
        "drives": _available_roots(),
        "places": _quick_places(pathlib.Path.home()),
    }


def _selected_project_note(app: App, resolved: str) -> str:
    """选中项目的备注（只读）。

    备注是附加偏好，读不到不该让一轮发不出去：仓储缺席 / 档案已删就返回
    空串。优先按偏好里的 ``project_id`` 取；缺选中关系时按目录兜底找一行
    —— 老偏好或手动填目录的状态也能带上既有备注。返回值已压成单行。
    """
    repo = getattr(app, "project_repo", None)
    if repo is None:
        return ""
    try:
        prefs = load_prefs(app)
        project_id = str(prefs.get("project_id") or "").strip()
        row = repo.get(project_id) if project_id else None
        if row is None:
            row = repo.find_by_directory(resolved)
        note = str((row or {}).get("note") or "").strip()
    except Exception:  # noqa: BLE001 - 档案读失败不挡这一轮，备注本来就是附加项
        return ""
    return " ".join(note.split())


def _context_for(app: App, text: str, *, inject: bool,
                 project_note: str = "") -> tuple[str, str]:
    """这一轮要带的背景（21.5 / V1.115）。**读不出来就不带，但说出来。**

    ``inject=False`` 时返回一句「你关掉了」的说明 —— 不是空串：空串会被
    显示成「这一版还没有这个能力」，而那是两件事。
    """
    if not inject:
        return "", "你关掉了上下文注入 —— 只带了你那句话"
    try:
        brief = workbench_context.build_context(
            app, text, project_note=project_note)
    except Exception as exc:  # noqa: BLE001 - 注入失败不该让一轮发不出去
        return "", f"上下文没读成（{exc}）—— 这一轮只带了你那句话"
    return brief.text, brief.note


def agent_start(app: App, body: dict[str, Any], turns: Any) -> dict[str, Any]:
    """``POST /api/agent/start`` —— 先判一次，再（可能）开一轮。

    ``turns`` 由 :mod:`endpoints_agent` 传进来（同一条懒建的服务实例）。
    """
    text = str(body.get("text") or "").strip()
    directory = str(body.get("directory") or "").strip()
    mode = str(body.get("mode") or engine_host.ENGINE_ASK).strip()
    force = bool(body.get("force"))
    if not text:
        raise FreeAgentError("一句话都没说")
    if not directory:
        raise FreeAgentError("先选工作目录 —— 没有目录就不知道该在哪儿动手")
    if mode not in (engine_host.ENGINE_ASK, engine_host.ENGINE_AUTO_DIR):
        raise FreeAgentError(f"不认识的授权档：{mode!r}")
    # 目录在起线程之前就挡住：坏目录先落库、再起一轮后台失败，人只会看到
    # 一张失败的卡，却不知道是自己路径写错了。校验口径与宿主 ``ensure`` 同一份。
    try:
        resolved = engine_host.resolve_workdir(directory)
    except ValueError as exc:
        raise FreeAgentError(str(exc)) from exc

    # 可用性在这里**不判**：路由只回答「要不要」。引擎真起不来时会在
    # 那一轮里留下原因 —— 在这里预判一次等于把同一件事说两遍，且会说错
    # （装了但没登录、装了但起不来，路由无从知道）。
    decision = agent_router.decide(text, directory=resolved, engine_available=True)
    if not decision.needs_engine and not force:
        return {
            "kind": "agent_decision",
            "routed": False,
            "decision": {
                "needs_engine": False,
                "reason": decision.reason,
                "hits": list(decision.hits),
                "brief": decision.brief,
            },
            "turn": None,
        }

    project_note = _selected_project_note(app, resolved)
    context, context_note = _context_for(
        app, decision.brief, inject=body.get("inject", True) is not False,
        project_note=project_note,
    )
    try:
        view = turns.start(
            text=decision.brief, directory=resolved, mode=mode,
            model=str(body.get("model") or "").strip(),
            agent=str(body.get("agent") or "").strip(),
            variant=str(body.get("variant") or "").strip(),
            context=context, context_note=context_note,
        )
    except ValueError as exc:
        raise FreeAgentError(str(exc)) from exc

    # 选用即建档（V1.124）：这个目录第一次被选用就登记成项目，名取末段；
    # 已有档案则只刷新最近使用。备注不在这一步填 —— 建档时人通常还没想好。
    repo = _project_repo(app)
    project_id = ""
    if repo is not None:
        row = repo.register(
            directory=resolved, name=_leaf_name(resolved), note="",
            now=app.clock.now(),
        )
        project_id = str(row["id"])
    save_prefs(app, {
        "directory": resolved, "project_id": project_id,
        "model": str(body.get("model") or ""),
        "agent": str(body.get("agent") or ""),
        "variant": str(body.get("variant") or ""), "mode": mode,
    })
    # 延迟 import：``endpoints_agent`` 在模块级 import 本模块（派发表要用
    # ``agent_start``），顶层再反着 import 会成环。
    from .endpoints_agent import turn_dict

    reason = decision.reason if not force else (
        "你强制交给了引擎（规则本来判：" + decision.reason + "）")
    return {
        "kind": "agent_decision",
        "routed": True,
        "decision": {
            "needs_engine": True,
            "reason": reason,
            "hits": list(decision.hits),
            "brief": decision.brief,
        },
        "turn": turn_dict(view),
    }
