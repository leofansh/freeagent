"""周回顾：这一周推进了什么、卡在哪、下周承诺了什么（设计文档 6.5）。

## 为什么值得单独一个视图

``/today`` 回答「现在做什么」，回答不了「这一周我到底推进了什么」。
GTD 的核心仪式是**定期回顾** —— 没有它，事务只会一直向前滚动，
没人回头看；而「助人变得更有能力」恰恰需要人看见自己的节奏。

回顾**不需要任何新数据**：``TaskRecord`` 是只追加日志（5.1），
``TaskRepo.list_closed_between`` 已能按结束时间查（V1.20 加的），
顺延次数直接数 ``ROLLOVER`` 记录的条数。所以这是纯查询视图，
不加表、不迁移。

## 刻意不做的事

| 不做 | 为什么 |
|---|---|
| **不排序打分** | 打分用的是「此刻的精力与紧迫度」（7.2）。拿它衡量已经过去的一周没有意义；下周的承诺更不该被**今天**的精力权重左右 |
| **不给结论、不提建议** | 「这周效率不高」「下周别再拖了」是替用户做判断。视图只摆事实（第七章） |
| **不解析记录正文** | 顺延次数数的是 ``ROLLOVER`` 记录的**条数**。正文「原定 X，顺延到 Y」是给人看的 —— 一旦程序去解析它，改文案就等于改数据 |
| **不写库、不发提醒** | 纯查询。回顾是用户主动要的，不是系统催的 |

## 为什么先顺延再统计

与 ``/today`` 同一条理由（6.3 收口机制）：逾期未结束的事务如果不先
顺延，它们既不在「本周结束」里、也不在「下周承诺」里、也不在「没排期」
里 —— 会从四个分区里同时消失。那正是本仓最忌讳的**静默**。
``rollover()`` 幂等、且每次移动都落一条记录（所以顺延次数仍然可数）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from ..domain import RecordType, Task
from ..storage.repos import RecordRepo, TaskRepo
from .clock import Clock
from .today import TodayService

__all__ = ["SlipItem", "WeekService", "WeekView"]

#: ``list_closed_between`` 的区间是**左闭右开**：右端要 +1 秒，否则
#: 「刚刚完成（``completed_at`` 恰好等于 now）」会被判成不在窗口内 ——
#: 表现为「刚做完的事在周回顾里看不见」，而那是本仓最忌讳的静默。
#: ``chat.py`` 的 ``_time_window`` 用同一条口径（``now + timedelta(seconds=1)``）。
_ONE_SECOND = timedelta(seconds=1)


@dataclass(frozen=True, slots=True)
class SlipItem:
    """本周顺延过的事务。``times`` 是**这一周内**的顺延次数。"""

    task: Task
    times: int


@dataclass(frozen=True, slots=True)
class WeekView:
    """一周的事实。四个分区互不重叠，合起来覆盖「该看见的全部」：

    * ``closed``：本周结束（完成或放弃）；
    * ``slips``：本周被顺延过的未结束事务（卡在哪）；
    * ``next_week``：下周已排期的未结束事务（承诺了什么）；
    * ``unscheduled``：没排期的未结束事务（记了却没进任何一天）；
    * ``waiting``：在等的未结束事务（卡在别人手里）。

    ``unscheduled`` 与 ``waiting`` **可能重叠**（在等的事也可能没排期）——
    那不是 bug：一条事务可以同时是「悬空」和「在等」。视图分层是为了
    回答不同问题，不是为了把事务归档到唯一一格。
    """

    start: date
    end: date
    closed: tuple[Task, ...]
    slips: tuple[SlipItem, ...]
    next_week: tuple[Task, ...]
    unscheduled: tuple[Task, ...]
    waiting: tuple[Task, ...]

    @property
    def completed(self) -> tuple[Task, ...]:
        return tuple(t for t in self.closed if t.completed_at is not None)

    @property
    def dropped(self) -> tuple[Task, ...]:
        return tuple(t for t in self.closed if t.completed_at is None)

    @property
    def slipped_times(self) -> int:
        return sum(item.times for item in self.slips)

    @property
    def is_empty(self) -> bool:
        return not (
            self.closed or self.slips or self.next_week
            or self.unscheduled or self.waiting
        )


class WeekService:
    def __init__(
        self,
        tasks: TaskRepo,
        records: RecordRepo,
        clock: Clock,
        *,
        today: TodayService | None = None,
    ) -> None:
        self._tasks = tasks
        self._records = records
        self._clock = clock
        # 顺延的**唯一实现**在 TodayService 里。不复制一份 —— 复制就会漂移，
        # 而「顺延到哪天」漂移的后果是两条通道给出不同答案。
        self._today = today

    def view(self) -> WeekView:
        if self._today is not None:
            self._today.rollover()          # 幂等；理由见模块文档
        now = self._clock.now()
        today = self._clock.today()
        monday = today - timedelta(days=today.weekday())
        start = datetime.combine(monday, time.min)
        week_end = datetime.combine(monday + timedelta(days=7), time.min)
        next_end = datetime.combine(monday + timedelta(days=14), time.min)

        closed = tuple(self._tasks.list_closed_between(start, now + _ONE_SECOND))
        open_tasks = [t for t in self._tasks.list_open()]

        slips: list[SlipItem] = []
        for task in open_tasks:
            times = sum(
                1
                for record in self._records.list_for_task(task.id)
                if record.type is RecordType.ROLLOVER
                and start <= record.ts < week_end
            )
            if times:
                slips.append(SlipItem(task=task, times=times))
        # 顺延最多的排前面；同次数按创建先后（稳定、可复现）。
        slips.sort(key=lambda item: (-item.times, item.task.created_at))

        next_week = tuple(
            sorted(
                (
                    t for t in open_tasks
                    if t.scheduled_for is not None
                    and monday + timedelta(days=7) <= t.scheduled_for < next_end.date()
                ),
                key=lambda t: (t.scheduled_for, t.created_at),
            )
        )
        unscheduled = tuple(
            sorted(
                (t for t in open_tasks if t.scheduled_for is None),
                key=lambda t: t.created_at,
            )
        )
        waiting = tuple(
            sorted(
                (t for t in open_tasks if t.waiting_on is not None),
                key=lambda t: (t.waiting_on.since, t.created_at),
            )
        )
        return WeekView(
            start=monday,
            end=monday + timedelta(days=7),
            closed=closed,
            slips=tuple(slips),
            next_week=next_week,
            unscheduled=unscheduled,
            waiting=waiting,
        )
