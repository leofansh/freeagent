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

V1.101 起，节奏面的时段分布多带一句**凝练结论**：「37 次完成里 12 次
（32%）集中在 21-23 点」—— 原始分布照旧全列，凝练句只是把「集中在哪」
从「自己数」变成「直接说」。它是**对已发生事实的统计描述**，不是
「你总在深夜干活，建议调整」那种评价；判定线（一段至少 3 次、且占全部
的三分之一）是常数，样本不够时**不出结论并说清为什么**（V1.96：宁可
漏判，不错判）。

它是 **Personal 与通用助手的真正分水岭**（20.1.1 原文）：无状态即每次从零，
等于不是 personal；而这里跨会话积累的，是对「你自己」的理解。

## 刻意不做的事

| 不做 | 为什么 |
|---|---|
| **不写库** | 纯只读聚合。模型是「从记录现推」的视图，不是一份要维护的状态；写一份就等于又多一个真源要和它保持同步 |
| **不排序打分** | 打分用的是「此刻的精力与紧迫度」（7.2）。拿它给已经过去的一段时间贴「高效 / 低效」标签，是替用户做判断 |
| **不做评价、不提建议** | 「你总在深夜完成」「这件又顺延了，下周别拖」是评价。视图只摆规律（第七章「摆得更清楚 ≠ 替你决定」），与 /week、/feedback 同一条纪律。**界线在「可数」与否**（V1.101）：「37 次里 13 次在 21-23 点」是统计描述（每一项都能在原始分布里对账），「你总在深夜干活」是给这件事贴意义 —— 前者说，后者不说 |
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
#: 一段时段里的完成数达到这么多，才够资格谈「集中」。
#: 与 ``_MIN_ROLLOVERS`` 同一条精神：偶发一两次是巧合，不是规律。
#: 时段是连续量、随机性比「同一条事务顺延几次」高，所以线不能更低。
_MIN_PEAK_COUNT = 3
#: 「集中」还要求这一段占全部完成的至少三分之一（``count * 3 >= done``，
#: 用整数比而不是浮点）。24 个小时里最高的一段只占 8%，那是「分布平坦」，
#: 不是「集中」—— 把最高的一段说成「集中」会误导（V1.96：宁可漏判）。
#: 同数并列时取起始早的一段（稳定、可预期，不引入随机兜底）。
_PEAK_SHARE_NUM = 3
#: 成段的键至少要有这么多次完成。**孤例不定义段的边界** —— 21-23 点
#: 各完成 4/5/3 次是峰，旁边 20 点只完成过 1 次，把它并进来会把
#: 「21-23 点」撑成「20-23 点」，而那句话就不再是原话要说的事。
_MIN_RUN_COUNT = 2
#: 默认回看窗口（天）。个人模型是长周期规律，太短没有意义。
_DEFAULT_SINCE_DAYS = 90


@dataclass(frozen=True, slots=True)
class PersonalModelView:
    """从你的记录里现推的轻量规律。

    全部字段都是**事实统计**，没有一项是评价：

    * ``completion_by_hour`` / ``completion_by_dow``：完成时段分布；
    * ``hour_peak`` / ``dow_peak``：分布的**凝练结论**（V1.101）—— 集中在
      哪一段 / 哪一天，未达判定线时为 ``None``（判定不出，不是「没有峰」）；
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
    #: 滋养兴趣（V1.69，记录优先）：记忆里的「偏好」—— 爱好、口味、喜欢做什么。
    #: 刻意与 ``commitments`` 分开：偏好是「我是谁」，约定/目标是「我在追什么」，
    #: 混在「承诺与目标」里会把它读成该兑现的义务（〇.8.4 只记录不催促）。
    preferences: tuple[tuple[str, str], ...]              # (kind, text)　kind 恒为「偏好」
    #: 完成最集中的一段**连续小时**（起、止、计数）；判定不出为 None。
    #: 凝练结论带计数（V1.96「没有计数的规律不可反驳」在框架层的落地）。
    hour_peak: tuple[int, int, int] | None = None
    #: 完成最集中的**星期几**（weekday、计数）；判定不出为 None。
    dow_peak: tuple[int, int] | None = None

    @property
    def is_empty(self) -> bool:
        """没有任何可归纳的事实。

        角色活跃度不计入：只要有事务就一定有角色，它不能代表「有没有规律」。
        关系账、承诺与滋养偏好计入：有「跟谁还有账」或「还记着的目标 / 爱好」
        本身就是规律。
        """
        return (
            self.done_in_window == 0
            and self.rollover_total == 0
            and not self.stuck_waiting
            and not self.relationships
            and not self.commitments
            and not self.preferences
        )


def _merge_runs(counts: Mapping[int, int]) -> list[tuple[int, int, int]]:
    """把键相邻（差 1）、且**各自至少** ``_MIN_RUN_COUNT`` 次的计数并成段。

    返回 ``[(start, end, count), ...]``，按计数降序、同数起始早在前。
    21/22/23 点各几次完成会并成一段「21-23 点」—— 人说的「集中在
    21-23 点」本来就是这种形态。**只完成过 1 次的键先滤掉**：孤例不定义
    段的边界（否则旁边的散点会把峰撑大，见 ``_MIN_RUN_COUNT`` 的注释）。
    **不做环形合并**（23 点与 0 点不相邻）：环上有歧义（「周日+周一」
    算不算一段没有天然答案），第一版不猜，记在文档里。
    """
    dense = {k: v for k, v in counts.items() if v >= _MIN_RUN_COUNT}
    segments: list[tuple[int, int, int]] = []
    run_start: int | None = None
    run_end: int | None = None
    run_count = 0
    for key in sorted(dense):
        if run_end is not None and key == run_end + 1:
            run_end = key
            run_count += dense[key]
        else:
            if run_start is not None:
                segments.append((run_start, run_end, run_count))
            run_start = run_end = key
            run_count = dense[key]
    if run_start is not None and run_end is not None:
        segments.append((run_start, run_end, run_count))
    segments.sort(key=lambda seg: (-seg[2], seg[0]))
    return segments


def _peak(counts: Mapping[int, int], total: int) -> tuple[int, int, int] | None:
    """最集中的一段连续区间；达不到判定线返回 ``None``（判定不出）。

    判定线两条同时满足：段内计数 ≥ ``_MIN_PEAK_COUNT``、且段内计数占
    全部至少三分之一。段已按计数降序，所以第一个过线的就是「计数最高
    的过线段」；最高段都过不了线时，次高段更过不了（计数更小而总数不变）。
    """
    for start, end, count in _merge_runs(counts):
        if count >= _MIN_PEAK_COUNT and count * _PEAK_SHARE_NUM >= total:
            return (start, end, count)
    return None


def _dow_peak(counts: Mapping[int, int], total: int) -> tuple[int, int] | None:
    """最集中的星期几（离散维度，不合并）；判定不出返回 ``None``。"""
    for dow, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        if count >= _MIN_PEAK_COUNT and count * _PEAK_SHARE_NUM >= total:
            return (dow, count)
    return None


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

    # -- 时段凝练（V1.101）：分布在上面摆着，「集中在哪」要能一句话说出 ----
    # 每条凝练结论都带计数（V1.96：没有计数的规律不可反驳），且判定不出
    # 时是 None 而不是「分布平坦」四个字 —— 后者是把判定结果说成结论。
    hour_peak = _peak(by_hour, done_in_window)
    dow_peak = _dow_peak(by_dow, done_in_window)

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

    # -- 滋养兴趣（V1.69，记录优先）：记忆里的「偏好」----------------------------
    # 只原样列出，不排序打分、不评判 —— 它就是「你是怎样的人」，没有对错。
    preferences = tuple((entry.kind, entry.content) for entry in memory_entries if entry.kind == "偏好")

    return PersonalModelView(
        since=since,
        until=until,
        window_tasks=len(tasks),
        done_in_window=done_in_window,
        completion_by_hour=tuple(sorted(by_hour.items())),
        completion_by_dow=tuple(sorted(by_dow.items())),
        hour_peak=hour_peak,
        dow_peak=dow_peak,
        rollover_total=rollover_total,
        tasks_rolled_over=tasks_rolled_over,
        stuck_waiting=tuple(stuck),
        role_activity=role_activity,
        relationships=relationships,
        commitments=commitments,
        preferences=preferences,
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
