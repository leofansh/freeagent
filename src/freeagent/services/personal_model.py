"""连续性个人模型（设计文档 11.17 / 20.1.1）。

## 它解决什么

``TaskRecord`` 是只追加日志（5.1），已经存下了你全部的承诺 / 完成 / 顺延
事件。``PersonalModelService`` 直接从它**派生一个轻量个人模型**——典型完成
时段、常顺延的事务、卡在别人手里的等候、角色活跃度——而不引入任何向量库或
神经网络。这正好贴合「不自带 RAG」的定位（第十五章）：规则统计即可。

V1.59 起再并入**关系账**（``contacts`` 表的「我欠 / 我等」）与
**承诺与目标**（对外承诺 + 记忆里的「约定 / 目标」），把「关于你自己的画像」
从纯行为统计扩成「节奏 / 关系 / 承诺」三面。仍是只读聚合：**不写库、不排序
打分、不给建议**—— 关系账与承诺只摆事实（逾期标「已逾期」，不写「违约」；
判断仍是用户的事），记忆只原样列出「约定 / 目标」原文。

它是 **Personal 与通用助手的真正分水岭**（20.1.1 原文）：无状态即每次从零，
等于不是 personal；而这里跨会话积累的，是对「你自己」的理解。

## 刻意不做的事

| 不做 | 为什么 |
|---|---|
| **不写库** | 纯只读聚合。模型是「从记录现推」的视图，不是一份要维护的状态；写一份就等于又多一个真源要和它保持同步 |
| **不排序打分** | 打分用的是「此刻的精力与紧迫度」（7.2）。拿它给已经过去的一段时间贴「高效 / 低效」标签，是替用户做判断 |
| **不给结论、不提建议** | 「你总在深夜完成」「这件又顺延了，下周别拖」是评价。视图只摆规律（第七章「摆得更清楚 ≠ 替你决定」），与 /week、/feedback 同一条纪律 |
| **不解析记录正文** | 顺延次数数的是 ``ROLLOVER`` 记录的**条数**；完成时段读的是 ``Task.completed_at``。一旦去解析正文「原定 X 顺延到 Y」，改文案就等于改数据 |
| **不引入向量 / RAG** | 规则统计即可，零新依赖；也避免「自动 consolidation」带来的静默改写风险 |
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Mapping, Sequence

from ..domain import Contact, Promise, RecordType, Task
from ..domain.models import TaskRecord
from ..storage.repos import RecordRepo, RoleRepo, TaskRepo
from .clock import Clock
from .memory import MemoryEntry

__all__ = ["PersonalModelService", "PersonalModelView", "build_personal_model"]

#: 等候超过这么多天，算「卡住」（主动提示去催，而不是默默挂着）。
_STUCK_WAITING_DAYS = 7
#: 顺延次数达到这么多，才进「反复顺延」清单（偶发一次不算规律）。
_MIN_ROLLOVERS = 2
#: 默认回看窗口（天）。个人模型是长周期规律，太短没有意义。
_DEFAULT_SINCE_DAYS = 90


@dataclass(frozen=True, slots=True)
class PersonalModelView:
    """从你的记录里现推的轻量规律。

    全部字段都是**事实统计**，没有一项是评价：

    * ``completion_by_hour`` / ``completion_by_dow``：完成时段分布；
    * ``rollover_total`` / ``tasks_rolled_over``：顺延次数与反复顺延的事务；
    * ``stuck_waiting``：悬在别人手里超过阈值的等候；
    * ``role_activity``：各角色挂接的事务数（活跃度代理）。
    """

    since: date
    until: date
    window_tasks: int
    done_in_window: int
    completion_by_hour: tuple[tuple[int, int], ...]   # (hour 0-23, count) 升序
    completion_by_dow: tuple[tuple[int, int], ...]    # (weekday 0=Mon, count) 升序
    rollover_total: int
    tasks_rolled_over: tuple[tuple[str, str, int], ...]  # (id8, title, n) 按 n 降序
    stuck_waiting: tuple[tuple[str, str, int], ...]      # (id8, title, age_days) 按 age 降序
    role_activity: tuple[tuple[str, int], ...]            # (role_name, count) 按 count 降序
    #: 关系账：只列「有未结清账」的关系对象（id, name, 我欠他的承诺数, 我在等他的笔数）。
    relationships: tuple[tuple[str, str, int, int], ...]  # 按（承诺+等候）账重降序
    #: 承诺与目标：对外承诺（open）+ 记忆里的「约定 / 目标」。只摆事实，不评判。
    commitments: tuple[tuple[str, str, bool], ...]        # (kind, text, overdue)

    @property
    def is_empty(self) -> bool:
        """没有任何可归纳的事实。

        角色活跃度不计入：只要有事务就一定有角色，它不能代表「有没有规律」。
        关系账与承诺计入：有「跟谁还有账」或「还记着的目标」本身就是规律。
        """
        return (
            self.done_in_window == 0
            and self.rollover_total == 0
            and not self.stuck_waiting
            and not self.relationships
            and not self.commitments
        )


def build_personal_model(
    tasks: Sequence[Task],
    records: Sequence[TaskRecord],
    role_names: Mapping[str, str],
    now: datetime,
    *,
    since_days: int = _DEFAULT_SINCE_DAYS,
    stuck_waiting_days: int = _STUCK_WAITING_DAYS,
    min_rollovers: int = _MIN_ROLLOVERS,
    contacts: Sequence[Contact] = (),
    open_promises: Sequence[Promise] = (),
    memory_entries: Sequence[MemoryEntry] = (),
) -> PersonalModelView:
    """纯函数聚合。抽出它以便单测直接喂领域对象，不必扛一整只 App。

    窗口是**左闭右闭**的日期区间 ``[since, until]``；对 ``datetime`` 比较时
    右端取当天 ``time.max``，否则「今天刚完成」会落在窗口外——表现为
    「刚做完的事在模型里看不见」，而那是本仓最忌讳的静默（同 week.py 口径）。
    """
    since = (now - timedelta(days=since_days)).date()
    until = now.date()
    since_dt = datetime.combine(since, time.min)
    until_dt = datetime.combine(until, time.max)

    by_hour: Counter[int] = Counter()
    by_dow: Counter[int] = Counter()
    done_in_window = 0
    for task in tasks:
        ca = task.completed_at
        if ca is None or not (since_dt <= ca <= until_dt):
            continue
        done_in_window += 1
        by_hour[ca.hour] += 1
        by_dow[ca.weekday()] += 1

    rollover_by_task: Counter[str] = Counter()
    for record in records:
        if record.type is RecordType.ROLLOVER and since_dt <= record.ts <= until_dt:
            rollover_by_task[record.task_id] += 1
    rollover_total = sum(rollover_by_task.values())

    title_by_id = {t.id: t.title for t in tasks}
    tasks_rolled_over = tuple(
        sorted(
            (
                (tid[:8], (title_by_id.get(tid) or "?")[:40], n)
                for tid, n in rollover_by_task.items()
                if n >= min_rollovers
            ),
            key=lambda item: (-item[2], item[0]),
        )
    )

    stuck: list[tuple[str, str, int]] = []
    for task in tasks:
        waiting = task.waiting_on
        if waiting is None or not task.is_open:
            continue
        age = (now - waiting.since).days
        if age >= stuck_waiting_days:
            stuck.append((task.id[:8], (task.title or "?")[:40], age))
    stuck.sort(key=lambda item: (-item[2], item[0]))

    role_counter: Counter[str] = Counter()
    for task in tasks:
        for role_id in task.role_ids:
            role_counter[role_id] += 1
    role_activity = tuple(
        sorted(
            ((role_names.get(rid, rid[:8]), count) for rid, count in role_counter.items()),
            key=lambda item: (-item[1], item[0]),
        )
    )

    # -- 关系账（V1.59）：只列「有未结清账」的关系对象 ------------------------
    # 复用既有的「我欠 / 我等」两套计数口径（与 endpoints_pending.contacts
    # 同逻辑，但这里是只读聚合，不新写判定）：从 open 承诺按 contact_id 数，
    # 从 open 事务的 waiting_on.contact_id 数。没有未结清账的关系不出现 ——
    # 列一堆「0/0」是噪音，而且会让人误以为「跟谁都有账」。
    promise_count: Counter[str] = Counter()
    for p in open_promises:
        if p.contact_id:
            promise_count[p.contact_id] += 1
    waiting_count: Counter[str] = Counter()
    for task in tasks:
        waiting = task.waiting_on
        if task.is_open and waiting is not None and waiting.contact_id:
            waiting_count[waiting.contact_id] += 1
    name_by_id = {c.id: c.name for c in contacts}
    relationship_ids = (promise_count.keys() | waiting_count.keys()) & name_by_id.keys()
    relationships = tuple(
        sorted(
            (
                (cid, name_by_id[cid], promise_count.get(cid, 0), waiting_count.get(cid, 0))
                for cid in relationship_ids
            ),
            key=lambda item: (-(item[2] + item[3]), item[1]),
        )
    )

    # -- 承诺与目标（V1.59）：对外承诺（open）+ 记忆里的「约定 / 目标」 ------
    # 只摆事实，不排序打分、不评判；承诺的「已逾期」是日期比较，不是违约判定
    # （11.19.5：判违约是用户的事）。记忆只原样列出「约定 / 目标」原文。
    commitments: list[tuple[str, str, bool]] = []
    for p in open_promises:
        commitments.append(
            (
                "承诺",
                p.statement,
                p.promised_for is not None and p.promised_for < now,
            )
        )
    for entry in memory_entries:
        if entry.kind in ("约定", "目标"):
            commitments.append((entry.kind, entry.content, False))
    # 逾期承诺排最前，其余承诺在约定/目标前，再按原文稳定排序。
    commitments.sort(key=lambda item: (not item[2], item[0] != "承诺", item[1]))
    commitments = tuple(commitments)

    return PersonalModelView(
        since=since,
        until=until,
        window_tasks=len(tasks),
        done_in_window=done_in_window,
        completion_by_hour=tuple(sorted(by_hour.items())),
        completion_by_dow=tuple(sorted(by_dow.items())),
        rollover_total=rollover_total,
        tasks_rolled_over=tasks_rolled_over,
        stuck_waiting=tuple(stuck),
        role_activity=role_activity,
        relationships=relationships,
        commitments=commitments,
    )


class PersonalModelService:
    def __init__(
        self,
        tasks: TaskRepo,
        records: RecordRepo,
        clock: Clock,
        *,
        role_repo: RoleRepo | None = None,
        promise: "PromiseService | None" = None,
        memory: "MemoryService | None" = None,
    ) -> None:
        self._tasks = tasks
        self._records = records
        self._clock = clock
        self._role_repo = role_repo
        self._promise = promise
        self._memory = memory

    def view(self, since_days: int = _DEFAULT_SINCE_DAYS) -> PersonalModelView:
        role_names: dict[str, str] = {}
        if self._role_repo is not None:
            role_names = {role.id: role.name for role in self._role_repo.list_all()}
        contacts = self._promise.contacts() if self._promise is not None else ()
        open_promises = self._promise.open_promises() if self._promise is not None else ()
        memory_entries = self._memory.list_all() if self._memory is not None else ()
        return build_personal_model(
            self._tasks.list_all(),
            self._records.list_all(),
            role_names,
            self._clock.now(),
            since_days=since_days,
            contacts=contacts,
            open_promises=open_promises,
            memory_entries=memory_entries,
        )
