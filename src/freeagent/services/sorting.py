"""透明弱信号与排序。

立场（设计文档第七章）：**摆得更清楚 ≠ 替你决定。**
每条信号都带一句面向用户的中文理由，输出必须标注「启发式提示，不是评分」。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Sequence

from ..domain import (
    SORT_SIGNAL_WEIGHTS,
    ScoredTask,
    SortSignal,
    SortSignalCode,
    Task,
    TaskState,
)

__all__ = [
    "EnergyWindows",
    "DEADLINE_RISK_HOURS",
    "RESUME_STALE_DAYS",
    "WAITING_TOO_LONG_DAYS",
    "DISCLAIMER_MARKER",
    "compute_signals",
    "score_task",
    "sort_tasks",
    "score_and_sort",
]

#: 输出必须原样带上这句话，否则就变成了「助手在替用户打分」。
DISCLAIMER_MARKER = "启发式提示，不是评分"

#: 距截止不足这个小时数即命中 DEADLINE_RISK。
DEADLINE_RISK_HOURS = 24

#: 进行中但超过这个天数没恢复，命中 RESUME_STALE。
RESUME_STALE_DAYS = 3

#: 等候超过这个天数，命中 WAITING_TOO_LONG。
WAITING_TOO_LONG_DAYS = 7


@dataclass(frozen=True, slots=True)
class EnergyWindows:
    """三档固定精力时段（左闭右开，单位为小时）。"""

    morning: tuple[int, int] = (5, 12)
    afternoon: tuple[int, int] = (12, 18)
    evening: tuple[int, int] = (18, 24)

    def label_for(self, moment: datetime) -> str | None:
        hour = moment.hour
        for name, (low, high) in (
            ("早晨", self.morning),
            ("下午", self.afternoon),
            ("晚上", self.evening),
        ):
            if low <= hour < high:
                return name
        return None


def _weight(code: SortSignalCode) -> int:
    return SORT_SIGNAL_WEIGHTS[code]


def _overdue_wait_signals(task: Task, now: datetime) -> list[SortSignal]:
    waiting = task.waiting_on
    if waiting is None or waiting.follow_up_at is None:
        return []
    if waiting.follow_up_at > now:
        return []
    overdue = now - waiting.follow_up_at
    days = overdue.days
    span = f"已过 {days} 天" if days >= 1 else f"已过 {max(1, overdue.seconds // 3600)} 小时"
    return [
        SortSignal(
            code=SortSignalCode.OVERDUE_WAIT,
            weight=_weight(SortSignalCode.OVERDUE_WAIT),
            reason=f"该催 {waiting.who_or_what} 了（跟进日{span}）",
        )
    ]


def _deadline_risk_signals(task: Task, now: datetime) -> list[SortSignal]:
    if task.due_time is None:
        return []
    remaining = task.due_time - now
    if remaining > timedelta(hours=DEADLINE_RISK_HOURS):
        return []
    if remaining.total_seconds() < 0:
        span = f"已逾期 {-remaining.days} 天" if remaining.days < -1 else "已逾期"
    else:
        span = f"还剩 {max(1, int(remaining.total_seconds() // 3600))} 小时"
    return [
        SortSignal(
            code=SortSignalCode.DEADLINE_RISK,
            weight=_weight(SortSignalCode.DEADLINE_RISK),
            reason=f"临近截止（{task.due_time:%m-%d %H:%M} 到期，{span}）",
        )
    ]


def _depended_on_signals(dependents: int) -> list[SortSignal]:
    if dependents <= 0:
        return []
    return [
        SortSignal(
            code=SortSignalCode.DEPENDED_ON,
            weight=_weight(SortSignalCode.DEPENDED_ON),
            reason=f"有 {dependents} 件事在等它",
        )
    ]


def _resume_stale_signals(task: Task, now: datetime) -> list[SortSignal]:
    """进行中却久无动静。

    ``last_resumed_at`` 为空时回退到 ``updated_at`` ——
    「刚 start 完就再没碰过」同样是一种中断，只看恢复点会漏掉它。
    """
    if task.state is not TaskState.ACTIVE:
        return []
    reference = task.last_resumed_at or task.updated_at
    idle = now - reference
    if idle <= timedelta(days=RESUME_STALE_DAYS):
        return []
    return [
        SortSignal(
            code=SortSignalCode.RESUME_STALE,
            weight=_weight(SortSignalCode.RESUME_STALE),
            reason=f"中断已 {idle.days} 天没恢复",
        )
    ]


def _waiting_too_long_signals(task: Task, now: datetime) -> list[SortSignal]:
    waiting = task.waiting_on
    if waiting is None:
        return []
    waited = now - waiting.since
    if waited <= timedelta(days=WAITING_TOO_LONG_DAYS):
        return []
    return [
        SortSignal(
            code=SortSignalCode.WAITING_TOO_LONG,
            weight=_weight(SortSignalCode.WAITING_TOO_LONG),
            reason=f"已经等了 {waited.days} 天（{waiting.who_or_what}）",
        )
    ]


def _scheduled_today_signals(task: Task, today: date) -> list[SortSignal]:
    if task.scheduled_for != today:
        return []
    return [
        SortSignal(
            code=SortSignalCode.SCHEDULED_TODAY,
            weight=_weight(SortSignalCode.SCHEDULED_TODAY),
            reason="排在今天",
        )
    ]


def _inbox_unsorted_signals(task: Task) -> list[SortSignal]:
    if task.state is not TaskState.INBOX:
        return []
    return [
        SortSignal(
            code=SortSignalCode.INBOX_UNSORTED,
            weight=_weight(SortSignalCode.INBOX_UNSORTED),
            reason="还没开始",
        )
    ]


def _energy_fit_signals(
    task: Task, windows: EnergyWindows | None
) -> list[SortSignal]:
    if windows is None:
        return []
    moment = task.reminder_time
    if moment is None and task.scheduled_for is not None:
        return [
            SortSignal(
                code=SortSignalCode.ENERGY_FIT,
                weight=_weight(SortSignalCode.ENERGY_FIT),
                reason="排在你设定的今天时段",
            )
        ]
    if moment is None:
        return []
    label = windows.label_for(moment)
    if label is None:
        return []
    return [
        SortSignal(
            code=SortSignalCode.ENERGY_FIT,
            weight=_weight(SortSignalCode.ENERGY_FIT),
            reason=f"{moment:%H:%M} 落在「{label}」档",
        )
    ]


def compute_signals(
    task: Task,
    now: datetime,
    today: date,
    *,
    dependents: int = 0,
    energy_windows: EnergyWindows | None = None,
) -> tuple[SortSignal, ...]:
    """计算一条事务命中的全部弱信号。

    已结束的事务不产生任何信号。
    """
    if not task.is_open:
        return ()
    signals: list[SortSignal] = []
    signals += _overdue_wait_signals(task, now)
    signals += _deadline_risk_signals(task, now)
    signals += _depended_on_signals(dependents)
    signals += _resume_stale_signals(task, now)
    signals += _waiting_too_long_signals(task, now)
    signals += _scheduled_today_signals(task, today)
    signals += _inbox_unsorted_signals(task)
    signals += _energy_fit_signals(task, energy_windows)
    signals.sort(key=lambda s: -s.weight)
    return tuple(signals)


def score_task(
    task: Task,
    now: datetime,
    today: date,
    *,
    dependents: int = 0,
    energy_windows: EnergyWindows | None = None,
) -> ScoredTask:
    signals = compute_signals(
        task, now, today, dependents=dependents, energy_windows=energy_windows
    )
    return ScoredTask(
        task=task, signals=signals, total_weight=sum(s.weight for s in signals)
    )


def sort_tasks(scored: Sequence[ScoredTask]) -> list[ScoredTask]:
    """总权重降序；同权重**保持输入顺序**。

    用稳定排序而不是 ``id`` 做 tie-break，是因为 id 必须保持随机 ——
    界面和 CLI 都靠 ``id[:8]`` 前缀匹配，前缀若是时间戳就会误匹配。
    仓储层保证返回顺序是创建顺序（``ORDER BY entered_at, rowid``），
    所以同分事务自然按「先进先出」排。
    """
    return sorted(scored, key=lambda s: -s.total_weight)


def score_and_sort(
    tasks: Sequence[Task],
    now: datetime,
    today: date,
    *,
    dependents_of=None,
    energy_windows: EnergyWindows | None = None,
) -> list[ScoredTask]:
    """一步到位：打分 + 排序。``dependents_of`` 用于避免 N+1 查询。"""
    if dependents_of is None:

        def dependents_of(task_id: str) -> int:
            return 0

    scored = [
        score_task(
            task,
            now,
            today,
            dependents=dependents_of(task.id),
            energy_windows=energy_windows,
        )
        for task in tasks
    ]
    return sort_tasks(scored)
