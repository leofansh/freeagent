"""今天视图与顺延。

顺延是「收口机制」：没有它，事务会无限堆积（设计文档第六章）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..domain import RecordType, ScoredTask, Task
from ..storage.repos import RecordRepo, TaskRepo
from .clock import Clock
from .sorting import EnergyWindows, score_and_sort

__all__ = ["TodayService", "RolloverReport", "RolloverItem", "TodayView"]


@dataclass(frozen=True, slots=True)
class RolloverItem:
    task_id: str
    title: str
    from_day: date


@dataclass(frozen=True, slots=True)
class RolloverReport:
    """一次顺延的结果。同一天内重复访问必须是空报告。"""

    day: date
    items: tuple[RolloverItem, ...] = ()

    @property
    def count(self) -> int:
        return len(self.items)

    def summary(self) -> str | None:
        """合并成一条摘要。**绝不逐条输出。**"""
        if not self.items:
            return None
        titles = "、".join(item.title for item in self.items[:3])
        suffix = "…" if len(self.items) > 3 else ""
        return f"{len(self.items)} 条事务从之前的日子顺延到今天：{titles}{suffix}"


@dataclass(frozen=True, slots=True)
class TodayView:
    """今天视图。

    ``items`` 是**全局排序**的单一列表 —— 分区各自排序会让「权重降序」失去意义。
    被顺延的事务在列表里额外带 ``顺延`` 标记（由 ``rolled_over_ids`` 判定）。
    """

    day: date
    items: tuple[ScoredTask, ...]
    rollover: RolloverReport

    @property
    def rolled_over_ids(self) -> frozenset[str]:
        return frozenset(item.task_id for item in self.rollover.items)


class TodayService:
    def __init__(
        self,
        tasks: TaskRepo,
        records: RecordRepo,
        clock: Clock,
        *,
        energy_windows: EnergyWindows | None = None,
    ) -> None:
        self._tasks = tasks
        self._records = records
        self._clock = clock
        self._energy_windows = energy_windows

    def set_energy_windows(self, windows: EnergyWindows | None) -> None:
        """运行期换精力档位（设置页用），下次算分即生效。

        刻意**读时才取**：所有打分都走 ``self._energy_windows``，
        所以换完不需要重建任何服务。
        """
        self._energy_windows = windows

    # -- 顺延 --------------------------------------------------------------- #
    def rollover(self) -> RolloverReport:
        """把所有逾期未结束的事务顺延到今天。幂等。"""
        now = self._clock.now()
        today = self._clock.today()
        moved: list[RolloverItem] = []
        for task in self._tasks.list_open_before(today):
            previous = task.scheduled_for
            self._tasks.set_schedule(task.id, today, now)
            self._records.append(
                task.id,
                RecordType.ROLLOVER,
                f"原定 {previous}，顺延到 {today}",
                now,
            )
            moved.append(
                RolloverItem(task_id=task.id, title=task.title, from_day=previous or today)
            )
        return RolloverReport(day=today, items=tuple(moved))

    # -- 视图 --------------------------------------------------------------- #
    def view(self) -> TodayView:
        """取今天视图。**先顺延，再全局排序渲染。**"""
        report = self.rollover()
        today = self._clock.today()
        now = self._clock.now()

        candidates: list[Task] = [
            t for t in self._tasks.list_by_scheduled_for(today) if t.is_open
        ]
        scored = score_and_sort(
            candidates,
            now,
            today,
            dependents_of=self._tasks.count_dependents,
            energy_windows=self._energy_windows,
        )
        return TodayView(day=today, items=tuple(scored), rollover=report)

    def scheduled_for(self, day: date) -> list[ScoredTask]:
        """指定日期的视图（不触发顺延）。"""
        now = self._clock.now()
        candidates = [t for t in self._tasks.list_by_scheduled_for(day) if t.is_open]
        return score_and_sort(
            candidates,
            now,
            self._clock.today(),
            dependents_of=self._tasks.count_dependents,
            energy_windows=self._energy_windows,
        )
