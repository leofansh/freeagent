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

__all__ = ["create_task", "mutate", "drop_tasks", "delete_tasks"]

#: 单次批量「算了」的上限。不是性能考量（服务层逐条很快），而是**界面
#: 语义**：一次勾几十条然后一把全算了，手滑的成本太高；真有大扫除需求
#: 的人可以分几次点。上限数字本身没有技术含义。
MAX_BATCH_DROP = 100

#: 单次批量**删除**的上限。与 ``MAX_BATCH_DROP`` 同一条理由：批量删掉
#: 手滑的成本更高（还不可恢复），所以上限更该有。
MAX_BATCH_DELETE = 100


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


def drop_tasks(app: App, payload: dict) -> dict:
    """批量「算了」。**规则全部在服务层**，这里只做参数与汇总。

    为什么单独一个端点而不是复用 ``mutate`` 循环调：批量要有整体上限、
    逐条结果汇总和一句话总结，这些是批量语义的一部分。而状态迁移的
    合法性仍然只有服务层一处（``app.tasks.drop_many`` → ``drop`` →
    ``set_state``）—— 这里不查表、不判定迁移，只转发与汇总。
    """
    raw_ids = payload.get("ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        raise FreeAgentError("ids 必须是非空列表")
    cleaned: list[str] = []
    for raw in raw_ids:
        rid = str(raw).strip()
        if rid:
            cleaned.append(rid)
    if not cleaned:
        raise FreeAgentError("ids 里没有有效的事务 id")
    if len(cleaned) > MAX_BATCH_DROP:
        raise FreeAgentError(
            f"一次最多 {MAX_BATCH_DROP} 条，收到 {len(cleaned)} 条"
        )
    reason = payload.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise FreeAgentError("reason 只能是字符串")

    # 整批一把锁：批量是「一次操作」，不该和别的写请求交错成
    # 「A 线程落了 3 条、B 线程落了 2 条」的中间态。
    with app.lock:
        items = app.tasks.drop_many(
            cleaned, reason=(reason or None) if reason else None
        )
    done = [it for it in items if it.ok]
    failed = [it for it in items if not it.ok]
    return {
        "kind": "batch_drop",
        "dropped": len(done),
        "failed": [
            {"id": it.id, "title": it.title, "reason": it.reason}
            for it in failed
        ],
        "summary": (
            f"已算了 {len(done)} 条"
            + (f"，{len(failed)} 条没成" if failed else "")
            + (
                "：" + "；".join(
                    f"{(it.title or it.id[:8])}（{it.reason}）"
                    for it in failed[:3]
                )
                if failed
                else ""
            )
        ),
    }


def delete_tasks(app: App, payload: dict) -> dict:
    """批量硬删。**规则全部在服务层**，这里只做参数与汇总。

    与 ``drop_tasks`` 的分工同构：整体上限在这里（界面语义），逐条结果
    在服务层（``TaskService.delete_many``），汇总在这里说成人话。

    ## 为什么端点还认一个 ``confirmed`` 布尔

    前端已经两道 confirm，这里是第二道防线：端点只认 ``confirmed`` 为
    真布尔才动手（与 ``services/reset.py`` 同一先例）。**前端确认不能替代
    服务端闸门** —— 浏览器可以被绕过、按钮可以被手滑，而「删数据」的
    入口如果信任调用方自述的「我确认过」，那两道 confirm 就只是仪式。
    """
    raw_ids = payload.get("ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        raise FreeAgentError("ids 必须是非空列表")
    cleaned: list[str] = []
    for raw in raw_ids:
        rid = str(raw).strip()
        if rid:
            cleaned.append(rid)
    if not cleaned:
        raise FreeAgentError("ids 里没有有效的事务 id")
    if len(cleaned) > MAX_BATCH_DELETE:
        raise FreeAgentError(
            f"一次最多 {MAX_BATCH_DELETE} 条，收到 {len(cleaned)} 条"
        )
    if payload.get("confirmed") is not True:
        # 不替「忘了传 confirmed」找理由放行：调用方必须显式说「已确认」。
        # 前端两道 confirm 都过才发这个 true（见 js_core.batchDelete）。
        raise FreeAgentError("缺少 confirmed 标记，已拒绝删除")

    with app.lock:
        items = app.tasks.delete_many(cleaned)
    done = [it for it in items if it.ok]
    failed = [it for it in items if not it.ok]
    return {
        "kind": "batch_delete",
        "deleted": len(done),
        "failed": [
            {"id": it.id, "title": it.title, "reason": it.reason}
            for it in failed
        ],
        "summary": (
            f"已删除 {len(done)} 条"
            + (f"，{len(failed)} 条没成" if failed else "")
            + (
                "：" + "；".join(
                    f"{(it.title or it.id[:8])}（{it.reason}）"
                    for it in failed[:3]
                )
                if failed
                else ""
            )
        ),
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
