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

## 安静窗口（9.8）：只改推送时机，不新增状态

:class:`QuietHours` 在 :meth:`ReminderEngine.due` 外面套一层「现在是不是
低打扰时段」。窗口内**照常查出提醒但不推送** —— 更准确地说，`due()` 在窗口
内返回**空摘要 + `quiet_until`**，于是调用方（终端 / 飞书 / Web）走各自
「这轮没提醒」的既有分支，窗口一过下一轮自然推。

刻意**不引入「推迟到某时刻」的持久状态**：那要加一张表或一列、还要写
唤醒调度器，而轮询本来就每分钟一次，窗口一过自动就推了。多一个状态就多
一处能与现实不一致的地方。窗口内也**不 acknowledge**（令牌不落库），
所以它不会丢 —— 这一条完全复用 9.2 已有的「宁可重发不可静默丢弃」性质。

**为什么不在这个模块里做「为什么不复用 energy_windows」的论证**：
那是排序模块的东西，语义是「现在适合做什么」，与「能不能打扰」是两回事。
完整的否决理由写在设计文档 9.8，改这块前请先读那一段。
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from ..domain import NotFoundError, RecordType, Task
from ..storage.repos import RecordRepo, TaskRepo
from .clock import Clock
from .recurrence import RecurrenceError, parse_rule

__all__ = [
    "ReminderEngine",
    "ReminderDigest",
    "ReminderEntry",
    "QuietHours",
    "MISSED_AFTER",
]

#: 超过这个时长算「已错过」。
MISSED_AFTER = timedelta(hours=6)


@dataclass(frozen=True, slots=True)
class QuietHours:
    """安静窗口：本地墙钟起止（左闭右开），**可以跨天**。

    ``start`` / ``end`` 是 :class:`datetime.time`。构造时只允许
    ``start != end``（相等 = 零长度窗口 = 无意义，拒掉）。跨天由
    :meth:`contains` 自己判：``end <= start`` 即跨天（例如 22:00–08:00）。

    **这是「能不能打扰」的判定，与排序用的 `EnergyWindows` 无关** ——
    后者回答「现在适合做什么」。合并两者会把 18 点变成全网静音，
    理由见设计文档 9.8。
    """

    start: time
    end: time

    def __post_init__(self) -> None:
        if self.start == self.end:
            raise ValueError("安静窗口的起止时间不能相同（那是零长度窗口）")

    @property
    def crosses_midnight(self) -> bool:
        return self.end <= self.start

    def contains(self, moment: datetime) -> bool:
        """``moment`` 的本地墙钟是否落在窗口内（左闭右开）。"""
        now = moment.time()
        if self.crosses_midnight:
            # 22:00–08:00：``now >= start`` 或 ``now < end``。
            return now >= self.start or now < self.end
        return self.start <= now < self.end

    def describe(self) -> str:
        """人话，用于日志与界面（``22:00–08:00``，跨天加注）。"""
        text = f"{self.start:%H:%M}–{self.end:%H:%M}"
        return f"{text}（跨天）" if self.crosses_midnight else text

    def quiet_note(self, moment: datetime) -> str:
        """窗口内（``moment`` 落在窗口里）的压后说明（V1.60）。

        供每日总览这类**独立于到点提醒**的推送用：窗口内不落去重令牌，
        把「压后到何时」说出来，窗口一过自然再推 —— 与 ``ReminderDigest``
        的 ``quiet_note`` 同一语义，只是不依赖 digest 自己的 ``quiet_until``。
        """
        until = _end_of_quiet(self, moment)
        return (
            f"安静窗口内（到 {until:%H:%M}），每日总览已压后，"
            "窗口结束会自动推送。"
        )


def _end_of_quiet(hours: QuietHours, moment: datetime) -> datetime:
    """窗口内时，返回本窗口结束的**瞬时**（供 `due()` 报告「压后到何时」）。"""
    end = datetime.combine(moment.date(), hours.end, tzinfo=moment.tzinfo)
    if hours.crosses_midnight and moment.time() >= hours.start:
        # 22:00–08:00 且现在是 23:00：结束点在**次日** 08:00。
        end += timedelta(days=1)
    return end



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

    #: 处于安静窗口时，本窗口结束的瞬时；不在窗口内时为 ``None``。
    #:
    #: 窗口内 ``fired`` / ``missed`` **必为空**（9.8：不推送），所以它只用来
    #: 让调用方能说清「为什么这轮没有提醒」—— 有到点提醒却全空、且带着
    #: ``quiet_until``，就是「被安静窗口压后」而不是「真没有」。
    quiet_until: datetime | None = None

    @property
    def is_empty(self) -> bool:
        return not self.fired and not self.missed

    @property
    def is_quiet(self) -> bool:
        """是不是「因为安静窗口所以没推」。与「真没有提醒」必须能区分。"""
        return self.quiet_until is not None

    @property
    def entries(self) -> tuple[ReminderEntry, ...]:
        """全部条目（先到点、后已错过）。确认送达时按这个顺序落库。"""
        return self.fired + self.missed

    @property
    def count(self) -> int:
        return len(self.fired) + len(self.missed)

    def text(self) -> str | None:
        """渲染成**一条**摘要。``None`` 表示无事发生（含安静窗口内）。"""
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

    def quiet_note(self) -> str | None:
        """安静窗口内的说明；不在窗口内返回 ``None``。

        9.8 的诚实纪律：**不许静默**。窗口把提醒压后这件事必须能说出来 ——
        特别是「窗口内到点、窗口结束时已经超过 6 小时」的情况，用户看到的是
        一条「已错过」，他有权知道那是因为怕打扰他而没有当场推。
        """
        if self.quiet_until is None:
            return None
        return (
            f"安静窗口内（到 {self.quiet_until:%H:%M}），"
            "到期提醒已压后，窗口结束会自动推送。"
        )


class ReminderEngine:
    def __init__(
        self,
        tasks: TaskRepo,
        records: RecordRepo,
        clock: Clock,
        *,
        quiet_hours: QuietHours | None = None,
    ) -> None:
        self._tasks = tasks
        self._records = records
        self._clock = clock
        #: 安静窗口（9.8）。``None`` = 不启用（默认朝关）。
        #:
        #: 刻意放在引擎里而不是调用方：三条通道（终端 / 飞书 / Web）共用
        #: ``due()``，窗口判定若各写一份，迟早分叉成「这三个通道什么时候
        #: 会静音」的三种答案。能力一份实现（12.1.1）。
        self._quiet = quiet_hours
        #: 最近一次「重复规则坏了 / 算不出下一次」的原因，空串 = 没问题。
        #: 由调用方（doctor / 启动日志）决定要不要告诉用户 ——
        #: 引擎自己不打印，理由同 :class:`~freeagent.secrets.SecretStr`：
        #: 错误信息会进日志，而这条里带着事务 id 与规则原文。
        self.last_rule_error: str = ""

    def set_quiet_hours(self, quiet_hours: QuietHours | None) -> None:
        """运行期改安静窗口（设置页用）。与 ``TodayService.set_energy_windows`` 同形。"""
        self._quiet = quiet_hours

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

        ## 安静窗口（9.8）

        若现在落在安静窗口内，返回**空摘要 + ``quiet_until``** ——
        「有提醒、但现在不推」与「真没有提醒」必须能分开，所以不能简单
        返回空摘要。窗口内**不 acknowledge**（令牌不落库），窗口一过
        下一轮自然推出：压后，不丢弃。
        """
        now = self._clock.now()
        if self._quiet is not None and self._quiet.contains(now):
            return ReminderDigest(quiet_until=_end_of_quiet(self._quiet, now))

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
        """确认送达，落去重令牌，并**推进重复提醒到下一次**。

        送达失败时就**不要**调用 —— 令牌不落库，那条提醒下一轮还会出现。

        刻意**不承诺恰好一次**：这个设计允许同一条提醒被送达两次。
        个人事务助手的代价函数里，重复提醒的代价远小于静默吞掉一条提醒，
        所以宁可重复也不静默丢弃（设计文档 9.2）。

        ## 重复提醒怎么推进

        一次性提醒落完令牌就完事。**带规则**的还要问规则「下一个在哪」，
        把结果写回 ``reminder_time`` —— 那个字段始终是「下一次触发的瞬时」，
        所以到期查询、合并摘要、错过窗口都不需要知道规则存在。

        规则坏了（``parse_rule`` 抛）时**只跳过这一条的推进**，并把原因记进
        ``last_rule_error``，不去动令牌 —— 令牌照落，因为这条**确实送达了**。
        反过来做的话，一条坏规则会连累已经发出去的提醒被反复重发。
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
            self._advance_recurring(entry.task_id, now)

    def _advance_recurring(self, task_id: str, now: datetime) -> None:
        """把带规则的事务推到下一次触发。无规则 / 规则坏 / 事务已消失都安静跳过。"""
        try:
            task = self._tasks.get(task_id)
        except NotFoundError:
            return
        try:
            rule = parse_rule(task.reminder_rule)
        except RecurrenceError as exc:
            self.last_rule_error = f"事务 {task_id} 的重复规则坏了：{exc}"
            return
        if rule is None:
            return
        try:
            nxt = rule.next_after(now)
        except RecurrenceError as exc:
            # 找不到下一个可触发时刻：规则配错了。已经把这条记成送达是
            # **对的**（它确实到了），所以不撤销，只是不再排下一次 ——
            # 否则它会每轮都冒出来。
            self.last_rule_error = f"事务 {task_id} 的重复规则算不出下一次：{exc}"
            return
        self._tasks.update(dataclasses.replace(task, reminder_time=nxt), now)

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
