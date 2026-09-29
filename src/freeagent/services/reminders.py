"""提醒引擎。

V1 的诚实边界（设计文档 9.2）：**系统不运行时不会通知**。
这是轮询式实现的固有局限，原型阶段接受。

两条不扰民规则：
* 一个周期内所有到期提醒**合并成一条摘要**；
* 超过 6 小时的提醒标记为**已错过**，如实说明，不当作刚发生的新提醒。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from ..domain import RecordType, Task
from ..storage.repos import RecordRepo, TaskRepo
from .clock import Clock

__all__ = ["ReminderEngine", "ReminderDigest", "ReminderEntry", "MISSED_AFTER"]

#: 超过这个时长算「已错过」。
MISSED_AFTER = timedelta(hours=6)


@dataclass(frozen=True, slots=True)
class ReminderEntry:
    task_id: str
    title: str
    due_at: datetime
    missed: bool


@dataclass(frozen=True, slots=True)
class ReminderDigest:
    """一次检查的合并结果。"""

    fired: tuple[ReminderEntry, ...] = ()
    missed: tuple[ReminderEntry, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.fired and not self.missed

    @property
    def count(self) -> int:
        return len(self.fired) + len(self.missed)

    def text(self) -> str | None:
        """渲染成**一条**摘要。``None`` 表示无事发生。"""
        if self.is_empty:
            return None
        parts: list[str] = []
        if self.fired:
            parts.append("到点：" + "、".join(e.title for e in self.fired))
        if self.missed:
            parts.append(
                "已错过：" + "、".join(f"{e.title}（{e.due_at:%m-%d %H:%M}）" for e in self.missed)
            )
        body = "；".join(parts)
        return f"提醒 {self.count} 条 — {body}"


class ReminderEngine:
    def __init__(self, tasks: TaskRepo, records: RecordRepo, clock: Clock) -> None:
        self._tasks = tasks
        self._records = records
        self._clock = clock

    @staticmethod
    def _token(reminder_time: datetime) -> str:
        """去重令牌：同一次提醒只发一遍。"""
        return f"at:{reminder_time.isoformat()}"

    def _already_sent(self, task_id: str, token: str) -> bool:
        return any(
            r.type is RecordType.REMINDER_FIRED and r.content == token
            for r in self._records.list_for_task(task_id)
        )

    def check(self) -> ReminderDigest:
        """检查到期提醒。幂等：已发过的不会重复发。"""
        now = self._clock.now()
        fired: list[ReminderEntry] = []
        missed: list[ReminderEntry] = []

        for task in self._tasks.list_due_reminders(now):
            reminder_time = task.reminder_time
            if reminder_time is None:
                continue
            token = self._token(reminder_time)
            if self._already_sent(task.id, token):
                continue
            self._records.append(task.id, RecordType.REMINDER_FIRED, token, now)
            entry = ReminderEntry(
                task_id=task.id,
                title=task.title,
                due_at=reminder_time,
                missed=now - reminder_time > MISSED_AFTER,
            )
            (missed if entry.missed else fired).append(entry)

        return ReminderDigest(fired=tuple(fired), missed=tuple(missed))

    def due_count(self) -> int:
        return len(self._tasks.list_due_reminders(self._clock.now()))

    def pending(self) -> list[Task]:
        """已到点但仍未销账、且本次尚未推送的提醒事务。"""
        now = self._clock.now()
        out: list[Task] = []
        for task in self._tasks.list_due_reminders(now):
            if task.reminder_time is None:
                continue
            if self._already_sent(task.id, self._token(task.reminder_time)):
                continue
            out.append(task)
        return out
