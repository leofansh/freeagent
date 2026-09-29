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


def today_payload(view: TodayView, names: dict[str, str]) -> dict:
    rolled = view.rolled_over_ids
    return {
        "kind": "today",
        "day": view.day.isoformat(),
        "disclaimer": DISCLAIMER_MARKER,
        "count": len(view.items),
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


def all_payload(scored_list: list[ScoredTask], names: dict[str, str]) -> dict:
    """全部 / 未结束 / 已结束视图。形状与今天视图一致，**只有信号不同** ——
    这里不再算一遍分，排序仍然由 ``services`` 决定。"""
    return {
        "kind": "all",
        "disclaimer": DISCLAIMER_MARKER,
        "count": len(scored_list),
        "items": [scored_payload(s, names) for s in scored_list],
    }
