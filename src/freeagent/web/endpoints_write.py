"""端点实现：**写**的那一半（新建事务、状态迁移、排期）。

与只读端点分开，见 :mod:`endpoints_read` 的说明。

三条硬约束都在这里：

1. **白名单** —— 只认 :data:`freeagent.web.actions.STATE_ACTIONS` 里的动作
2. **迁移合法性来自服务层** —— 这里不查表，只转发
3. **建事务前先判意图** —— 问句和「改已有事务」的请求都不建东西
"""

from __future__ import annotations

from datetime import date

from ..app import App
from ..domain import FreeAgentError, TaskKind, WaitingKind, WaitingOn
from ..services.llm import InputIntent, read_intent
from . import serialize_api
from .endpoints_read import resolve_task, role_names, task_state

__all__ = ["create_task", "mutate"]


def create_task(app: App, payload: dict) -> dict:
    title = str(payload.get("title") or "").strip()
    if not title:
        raise FreeAgentError("标题不能为空")

    # 问句 / 改已有事务的请求都不是「要记的一件事」。表单是对话之外的另一个
    # 入口，同样会污染数据 —— 尤其是角色：一旦被随口一句话建出来，
    # 整个脉络结构就脏了。
    intent = read_intent(title)
    if intent is InputIntent.QUESTION:
        raise FreeAgentError(
            "标题看着是问句，这里只建事务、不做问答。要记事请写成一句话陈述。"
        )
    if intent is InputIntent.MUTATE:
        raise FreeAgentError(
            "这里只建新事务，不能改已有的一条。"
            "改排期用 /today-pin <id> <日期>，放弃用 /drop <id>。"
        )

    role_ids = payload.get("role_ids")
    if not isinstance(role_ids, list) or not role_ids:
        raise FreeAgentError("至少要选一个角色")
    valid = {r.id for r in app.roles.list_roles(include_merged=True)}
    resolved: list[str] = []
    for raw in role_ids:
        rid = str(raw)
        if rid not in valid:
            raise FreeAgentError(f"角色不存在：{rid}")
        if rid not in resolved:
            resolved.append(rid)

    try:
        kind = TaskKind(str(payload.get("kind") or "action"))
    except ValueError as exc:
        raise FreeAgentError("kind 只能是 action / wait / reminder") from exc

    scheduled = None
    if payload.get("scheduled_for"):
        try:
            scheduled = date.fromisoformat(str(payload["scheduled_for"]))
        except ValueError as exc:
            raise FreeAgentError("日期格式应为 YYYY-MM-DD") from exc

    created = app.tasks.create(
        title,
        resolved,
        kind=kind,
        intent=str(payload.get("intent") or "").strip() or None,
        scheduled_for=scheduled,
    )
    return {
        "kind": "created",
        "task": serialize_api.task_payload(
            app.restore.build_view(created), role_names(app)
        )["task"],
    }


def mutate(app: App, task_id: str, action: str, body: dict) -> dict:
    """状态迁移 / 排期。**全部委托服务层**，web 层不重写规则。"""
    resolved = resolve_task(app, task_id)
    if action == "done":
        task = app.tasks.complete(resolved)
    elif action == "drop":
        task = app.tasks.drop(resolved)
    elif action == "pause":
        task = app.tasks.pause(resolved)
    elif action == "start":
        task = app.tasks.start(resolved)
    elif action == "blocked":
        # 「放一放」必须说清在等什么 —— 否则 BLOCKED 是个黑洞状态，
        # 原则 3 的「为什么卡住」就答不上来。
        target = body.get("waiting_on")
        if not isinstance(target, str) or not target.strip():
            raise FreeAgentError("放一放要说清在等什么")
        task = app.tasks.block(
            resolved,
            WaitingOn(
                kind=WaitingKind.PERSON,
                who_or_what=target.strip(),
                since=app.clock.now(),
            ),
            reason="放一放",
        )
    elif action == "unpin":
        task = app.tasks.unschedule(resolved)
    elif action == "pin":
        task = app.tasks.schedule(resolved, app.clock.today())
    else:  # pragma: no cover - 白名单已拦
        raise FreeAgentError(f"不支持的动作：{action}")
    return task_state(app, task)
