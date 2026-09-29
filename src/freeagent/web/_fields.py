"""领域对象 → 基础字段的投影。

``serialize`` 与 ``serialize_api`` 都要用这几个，所以放在共享模块 ——
之前它们只在 ``serialize`` 里定义，另一边直接引用会 ``NameError``。
"""

from __future__ import annotations

from ..domain import ScoredTask, Task

__all__ = ["iso", "signals", "base_task"]


def iso(value) -> str | None:
    """``datetime`` / ``date`` → ISO 字符串；``None`` 原样返回。"""
    return value.isoformat() if value is not None else None


def signals(scored: ScoredTask) -> list[dict]:
    """排序信号。

    一条硬约束（设计文档第七章）：**每条信号的理由必须出现在响应里**。
    前端想「画得好看」也不能把 ``reason`` 藏起来，所以这里是必填项。
    """
    return [
        {"code": s.code.value, "weight": s.weight, "reason": s.reason}
        for s in scored.signals
    ]


def base_task(task: Task, names: dict[str, str]) -> dict:
    """事务的公共字段。列表视图和详情页共用，保证两处形状一致。"""
    return {
        "id": task.id,
        "short_id": task.id[:8],
        "title": task.title,
        "roles": [names.get(r, r) for r in task.role_ids],
        "kind": task.kind.value,
        "kind_label": task.kind.label,
        "state": task.state.value,
        "state_label": task.state.label,
        "is_open": task.is_open,
        "intent": task.intent,
        "definition_of_done": task.definition_of_done,
        "scheduled_for": iso(task.scheduled_for),
        "due_time": iso(task.due_time),
        "reminder_time": iso(task.reminder_time),
    }
