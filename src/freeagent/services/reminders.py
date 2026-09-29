"""提醒引擎。

V1 的诚实边界（设计文档 9.2）：**系统不运行时不会通知**。
这是轮询式实现的固有局限，原型阶段接受。

两条不扰民规则：
* 一个周期内所有到期提醒**合并成一条摘要**；
* 超过 6 小时的提醒标记为**已错过**，如实说明，不当作刚发生的新提醒。

## 两阶段：查与确认分开

取提醒是**纯查询** :meth:`ReminderEngine.due`，落去重令牌是**送达之后**的
:meth:`ReminderEngine.acknowledge`。两者曾经是一个方法，于是令牌在推送之前
就落库 —— 进程死在中间、或推送失败，那条提醒就被永久标记成「已发过」，
而用户从没收到。分开的代价是**允许重复**：不承诺恰好一次。
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
    def entries(self) -> tuple[ReminderEntry, ...]:
        """全部条目（先到点、后已错过）。确认送达时按这个顺序落库。"""
        return self.fired + self.missed

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

    def due(self) -> ReminderDigest:
        """**纯查询**：到期且尚未确认送达的提醒。**不写库。**

        为什么必须与 :meth:`acknowledge` 分开：去重令牌是对「已送达」的
        记账，不是对「已检查」的记账。两者合一的时候，令牌在推送之前就落了库，
        于是「进程死在检查与推送之间」或「推送失败」这两种情况里，提醒都会被
        永久标记成已送达 —— 而用户从没收到，且 9.3 的「已错过」分支也不会再
        触发它。这条约束见设计文档 9.2。
        """
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
            entry = ReminderEntry(
                task_id=task.id,
                title=task.title,
                due_at=reminder_time,
                missed=now - reminder_time > MISSED_AFTER,
            )
            (missed if entry.missed else fired).append(entry)

        return ReminderDigest(fired=tuple(fired), missed=tuple(missed))

    def acknowledge(self, digest: ReminderDigest) -> None:
        """确认送达，落去重令牌。**必须在推送成功之后调用。**

        送达失败时就**不要**调用 —— 令牌不落库，那条提醒下一轮还会出现。

        刻意**不承诺恰好一次**：这个设计允许同一条提醒被送达两次。
        个人事务助手的代价函数里，重复提醒的代价远小于静默吞掉一条提醒，
        所以宁可重复也不静默丢弃（设计文档 9.2）。
        """
        if digest.is_empty:
            return
        now = self._clock.now()
        for entry in digest.entries:
            self._records.append(
                entry.task_id,
                RecordType.REMINDER_FIRED,
                self._token(entry.due_at),
                now,
            )

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
