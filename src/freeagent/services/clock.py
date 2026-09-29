"""可注入时钟。

顺延、提醒、排序全都依赖「今天是哪天」，因此业务逻辑里**不允许**
直接调用 ``datetime.now()``。测试注入 :class:`FrozenClock` 即可完全确定。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Protocol, runtime_checkable

__all__ = ["Clock", "SystemClock", "FrozenClock"]


@runtime_checkable
class Clock(Protocol):
    """时间来源。"""

    def now(self) -> datetime:
        """当前时刻。"""
        ...

    def today(self) -> date:
        """当前本地日历日。"""
        ...


class SystemClock:
    """真实时钟。"""

    def now(self) -> datetime:
        return datetime.now()

    def today(self) -> date:
        return datetime.now().date()


class FrozenClock:
    """测试用固定时钟，可显式推进。"""

    def __init__(self, moment: datetime) -> None:
        self._moment = moment

    def now(self) -> datetime:
        return self._moment

    def today(self) -> date:
        return self._moment.date()

    # -- 测试辅助 ---------------------------------------------------------- #
    def set(self, moment: datetime) -> None:
        self._moment = moment

    def advance(
        self,
        *,
        days: int = 0,
        hours: int = 0,
        minutes: int = 0,
        seconds: int = 0,
    ) -> None:
        self._moment = self._moment + timedelta(
            days=days, hours=hours, minutes=minutes, seconds=seconds
        )
