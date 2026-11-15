"""工作台的「工作目录」与一次开跑（设计文档 21.4 / V1.114）。

## 为什么从 :mod:`endpoints_agent` 拆出来

那个文件已经顶到 web 包 250 行上限（``TestWebPackageStaysSmall`` 守着）。
拆的口径是**职责**而不是行数：这里只碰两件与「目录」有关的事 ——
列出目录、记住上次的选择；再加一个「按这份选择开一轮」。状态、答卡、停止
在另一个文件里。

## 「上次的选择」不是契约

它落在 ``<state_home>/workbench.json``，读不出来就当没有（下次重选）。
刻意**不**放数据库：它不是事实，是便利 —— 建表就要迁移、要清理、要解释
「为什么删了目录还留着它」，而它的全部价值只是省一次点击。
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

from ..app import App
from ..domain import FreeAgentError
from ..services import agent_router, agent_turn, engine_host, workbench_context
from .endpoints_feishu import state_home_for

__all__ = ["agent_dirs", "agent_start", "load_prefs", "save_prefs"]

_PREFS_NAME = "workbench.json"
#: 目录列表最多返回多少项。不设上限时，一个 ``node_modules`` 就能把一次
#: GET 拖成几秒 —— 而人只需要知道「往下该点哪个」。
_MAX_DIRS = 300


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


def agent_dirs(app: App, q: dict[str, list[str]]) -> dict[str, Any]:
    """``GET /api/agent/dirs?path=`` —— 列出某个目录下的子目录。

    这是**唯一**一个能读到工作目录里有什么的端点，所以两条约束写死：
    只列**目录**（不读文件内容、不递归），且最多 :data:`_MAX_DIRS` 项。
    它回答「往下该点哪个」，不是「这个目录里有什么」。
    """
    raw = (q.get("path") or [""])[0].strip()
    if not raw:
        raw = str(load_prefs(app).get("directory") or pathlib.Path.home())
    path = pathlib.Path(raw).expanduser()
    if not path.is_absolute():
        raise FreeAgentError(f"目录必须是绝对路径（收到 {raw!r}）")
    try:
        path = path.resolve()
    except OSError as exc:
        raise FreeAgentError(f"目录解析失败：{raw}（{exc}）") from exc
    if not path.is_dir():
        raise FreeAgentError(f"不是目录：{path}")
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
    }


def _context_for(app: App, text: str, *, inject: bool) -> tuple[str, str]:
    """这一轮要带的背景（21.5 / V1.115）。**读不出来就不带，但说出来。**

    ``inject=False`` 时返回一句「你关掉了」的说明 —— 不是空串：空串会被
    显示成「这一版还没有这个能力」，而那是两件事。
    """
    if not inject:
        return "", "你关掉了上下文注入 —— 只带了你那句话"
    try:
        brief = workbench_context.build_context(app, text)
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

    # 可用性在这里**不判**：路由只回答「要不要」。引擎真起不来时会在
    # 那一轮里留下原因 —— 在这里预判一次等于把同一件事说两遍，且会说错
    # （装了但没登录、装了但起不来，路由无从知道）。
    decision = agent_router.decide(text, directory=directory, engine_available=True)
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

    context, context_note = _context_for(
        app, decision.brief, inject=body.get("inject", True) is not False,
    )
    try:
        view = turns.start(
            text=decision.brief, directory=directory, mode=mode,
            model=str(body.get("model") or "").strip(),
            agent=str(body.get("agent") or "").strip(),
            variant=str(body.get("variant") or "").strip(),
            context=context, context_note=context_note,
        )
    except ValueError as exc:
        raise FreeAgentError(str(exc)) from exc

    save_prefs(app, {
        "directory": directory, "model": str(body.get("model") or ""),
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
