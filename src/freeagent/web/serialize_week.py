"""周回顾 → JSON（V1.133 的 Web 视图）。

为什么单独一个文件：``serialize.py`` 已接近 250 行上限（``TestWebPackageStaysSmall``
守着那条线），而周回顾是「一整面」的投影 —— 六个事务分区加三段痕迹，
放一起会顶破。与 ``serialize_model.py`` 同一种拆法。

**这一层只做字段投影，不含任何业务判断**：窗口、顺延、拍板 / 承诺的过滤全在
:class:`~freeagent.services.week.WeekService` 里算完，这里只是把事实摆成 JSON。
"""

from __future__ import annotations

from ..services.sorting import DISCLAIMER_MARKER

__all__ = ["week_payload"]


def _task(t, names: dict[str, str]) -> dict:
    """事务只投影视图用得到的字段；角色名由调用方给，这里不查库。"""
    role = names.get(t.role_ids[0]) if t.role_ids else None
    when = t.completed_at or t.dropped_at
    return {
        "title": t.title,
        "state": t.state.label,
        "role": role,
        "when": when.isoformat() if when is not None else None,
        "scheduled_for": t.scheduled_for.isoformat() if t.scheduled_for else None,
        "waiting_on": t.waiting_on.describe() if t.waiting_on else None,
    }


def week_payload(view, names: dict[str, str]) -> dict:
    """``/api/week`` 的响应。保留免责标记字段，前端必须原样呈现。"""
    return {
        "kind": "week",
        "disclaimer": DISCLAIMER_MARKER,
        "start": view.start.isoformat(),
        "end": view.end.isoformat(),
        "is_empty": view.is_empty,
        "closed": [_task(t, names) for t in view.closed],
        "slips": [
            {"title": item.task.title, "times": item.times} for item in view.slips
        ],
        "next_week": [_task(t, names) for t in view.next_week],
        "unscheduled": [_task(t, names) for t in view.unscheduled],
        "waiting": [_task(t, names) for t in view.waiting],
        "growth_goals": [
            {"kind": kind, "text": text} for kind, text in view.growth_goals
        ],
        "decisions": [
            {
                "conflict": d.conflict_text,
                "reason": d.reason_text,
                "decided_at": d.decided_at.isoformat() if d.decided_at else None,
            }
            for d in view.decisions
        ],
        "promises": [
            {
                "statement": p.statement,
                "promised_for": p.promised_for.isoformat() if p.promised_for else None,
            }
            for p in view.promises
        ],
        "turn_count": view.turn_count,
    }
