"""领域对象 → JSON（视图类：今天 / 全部 / 角色列表）。

**这一层只做转换，不含任何业务判断。** 所有规则（顺延、排序、状态迁移）
仍然由 ``services`` 决定，Web 层绝不自己算一遍 —— 否则两个入口会漂移。

一条硬约束（设计文档第七章）：**每条排序信号的理由必须出现在响应里**，
且响应必须带上免责标记。前端想「画得好看」也不能把它们藏起来。

单实体与信封类响应在 :mod:`serialize_api`，公共字段投影在 :mod:`_fields`。
"""

from __future__ import annotations

from ..domain import Role, ScoredTask
from ..services.sorting import DISCLAIMER_MARKER
from ..services.today import TodayView
from ._fields import base_task, iso, signals

__all__ = [
    "DISCLAIMER_MARKER",
    "role_names",
    "role_payload",
    "roles_payload",
    "scored_payload",
    "today_payload",
    "all_payload",
    "roles_options",
    "pending_payload",
    "promises_payload",
    "contacts_payload",
    "model_payload",
]


def roles_options(roles: list[Role]) -> list[dict]:
    """建事务表单用的下拉项。**已合并的、停用的不给** —— 往那种角色加事务是错的。"""
    return [
        {"id": r.id, "name": r.name, "active": r.active}
        for r in roles
        if r.active and r.merged_into is None
    ]


def role_names(roles) -> dict[str, str]:
    """id → 名字。序列化时用来把 role_ids 换成人类看得懂的名字。"""
    return {r.id: r.name for r in roles}


def role_payload(role: Role, names: dict[str, str]) -> dict:
    payload = {
        "id": role.id,
        "name": role.name,
        "note": role.note,
        "active": role.active,
        "default_definition_of_done": role.default_definition_of_done,
        "merged_into_name": names.get(role.merged_into) if role.merged_into else None,
    }
    return payload


def roles_payload(roles: list[Role], names: dict[str, str]) -> dict:
    """角色列表。按**活跃/静置**分组，已合并的照旧显示但带「已并入 X」标记。

    刻意**不**给已合并的角色单独开一组：视图切换只是「换个角度读同一份
    数据」，把它们藏起来等于替用户做取舍。它们只是不再出现在
    ``options``（新建表单的下拉）里，因为往已合并的角色加事务是错的。
    """
    return {
        "kind": "roles",
        "count": len(roles),
        "active": [role_payload(r, names) for r in roles if r.active],
        "silenced": [role_payload(r, names) for r in roles if not r.active],
    }


def scored_payload(
    scored: ScoredTask, names: dict[str, str], *, rolled_over: bool = False
) -> dict:
    """列表里的一条：基础字段 + 分值 + **全部信号（含理由）**。"""
    return {
        **base_task(scored.task, names),
        "total_weight": scored.total_weight,
        "signals": signals(scored),
        "rolled_over": rolled_over,
    }


def today_payload(
    view: TodayView, names: dict[str, str], pending_count: int = 0
) -> dict:
    rolled = view.rolled_over_ids
    return {
        "kind": "today",
        "day": view.day.isoformat(),
        "disclaimer": DISCLAIMER_MARKER,
        "count": len(view.items),
        # 没排期的**不进**今天视图（设计如此），但条数照报 —— 不报的话
        # 界面显示「今天 0 条」，而用户刚记的那件事其实在库里。
        "unscheduled_count": view.unscheduled_count,
        # 待处理收集器的条数（11.18 / 11.19）。**只报数不报内容** ——
        # 收集器是工作台，今天这块屏幕只负责提醒「还有没收揽的活」。
        # 0 条照报（前端据此隐藏那一行），不像 CLI 那样「0 条不占位」。
        "pending_count": pending_count,
        "items": [
            scored_payload(s, names, rolled_over=s.task.id in rolled)
            for s in view.items
        ],
        "rollover": {
            "count": view.rollover.count,
            "summary": view.rollover.summary(),
            "items": [
                {"task_id": i.task_id, "title": i.title,
                 "from_day": i.from_day.isoformat()}
                for i in view.rollover.items
            ],
        },
    }


def model_payload(view, now=None) -> dict:
    """连续性个人模型（11.17 / V1.59）：节奏 / 关系 / 承诺 三面。

    只读聚合的投影，不含任何业务判断 —— 逾期标 ``overdue``、违约交给用户，
    与 /api/contacts、/api/promises 同一份「只摆事实」纪律（11.19.5）。
    """
    return {
        "kind": "personal_model",
        "since": view.since.isoformat(),
        "until": view.until.isoformat(),
        "window_tasks": view.window_tasks,
        "done_in_window": view.done_in_window,
        "completion_by_hour": [list(p) for p in view.completion_by_hour],
        "completion_by_dow": [list(p) for p in view.completion_by_dow],
        "rollover_total": view.rollover_total,
        "tasks_rolled_over": [
            {"id8": t[0], "title": t[1], "n": t[2]} for t in view.tasks_rolled_over
        ],
        "stuck_waiting": [
            {"id8": t[0], "title": t[1], "age_days": t[2]} for t in view.stuck_waiting
        ],
        "role_activity": [{"name": n, "count": c} for (n, c) in view.role_activity],
        # V1.59 新增两面：关系账 + 承诺与目标。
        "relationships": [
            {"id": r[0], "name": r[1], "open_promises": r[2], "open_waitings": r[3]}
            for r in view.relationships
        ],
        "commitments": [
            {"kind": c[0], "text": c[1], "overdue": c[2]} for c in view.commitments
        ],
        "disclaimer": DISCLAIMER_MARKER,
    }


def pending_payload(items) -> dict:
    """待处理收集器（11.18）。

    **只摆事实，不猜内容**：每条捕获显示类型、收下时间、大小、本地路径 ——
    「它是什么」留给用户。

    ``index`` 是**澄清时的编号**（从 1 起，与 ``/pending N`` 一致）。
    这里必须显式给出而不能指望前端数出来：三个入口（终端、飞书、Web）
    数的是同一批东西，谁自己数谁就跟别人对不上号。字段写在这里，
    是为了让「序号」只有一个出处（12.1.1「一份能力一份实现」）。
    """
    return {
        "kind": "pending",
        "count": len(items),
        "items": [
            {
                "index": index,
                "id": i.id,
                "kind": i.kind,
                "name": i.name,
                "size": i.size,
                "path": i.path,
                "created_at": i.created_at.isoformat(),
            }
            for index, i in enumerate(items, start=1)
        ],
    }


def promises_payload(items, contacts: dict[str, str], now=None) -> dict:
    """对外承诺（11.19）。

    **只摆事实，不判违约**：逾期的只标「已逾期」，不写「违约」——
    判违约是用户的事（11.19.5）。
    """
    rows = []
    for index, promise in enumerate(items, start=1):
        overdue = (
            now is not None
            and promise.promised_for is not None
            and promise.promised_for < now
        )
        rows.append(
            {
                "index": index,
                "id": promise.id,
                "statement": promise.statement,
                "contact_id": promise.contact_id,
                "contact_name": contacts.get(promise.contact_id or ""),
                "promised_for": (
                    promise.promised_for.isoformat()
                    if promise.promised_for is not None
                    else None
                ),
                "status": promise.status.value,
                "overdue": overdue,
            }
        )
    return {"kind": "promises", "count": len(items), "items": rows}


def contacts_payload(
    items, promise_counts: dict[str, int], waiting_counts: dict[str, int] | None = None
) -> dict:
    """关系对象（11.19 / 20.1.5）。附「几条承诺未兑现」与「几笔在等他」。

    两数并排，关系才真的「一等」：只看承诺会漏掉「我在等对方」那一半，
    只看等候会漏掉「我欠对方」那一半 —— 而用户问的其实是同一句
    「我跟这个人之间有几笔没结清的」（V1.52）。
    """
    waits = waiting_counts or {}
    return {
        "kind": "contacts",
        "count": len(items),
        "items": [
            {
                "id": c.id,
                "name": c.name,
                "kind": c.kind.value,
                "note": c.note,
                "open_promises": promise_counts.get(c.id, 0),
                "open_waitings": waits.get(c.id, 0),
            }
            for c in items
        ],
    }


def all_payload(scored_list: list[ScoredTask], names: dict[str, str]) -> dict:
    """全部 / 未结束 / 已结束视图。形状与今天视图一致，**只有信号不同** ——
    这里不再算一遍分，排序仍然由 ``services`` 决定。"""
    return {
        "kind": "all",
        "disclaimer": DISCLAIMER_MARKER,
        "count": len(scored_list),
        "items": [scored_payload(s, names) for s in scored_list],
    }
