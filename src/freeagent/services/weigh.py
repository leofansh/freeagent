"""代价对照：把该拍板的东西整理到值得拍板（设计文档 7.5 / 20.1.14）。

## 它解决什么

排序回答「先看哪个」，**取舍**回答「只能选一个时选哪个」—— 这是两件事。
本仓有前者（``services/sorting.py`` 的信号权重表），没有后者。于是主人面对的
现状是：一条队列，队列里每件事单独看都挺急，但**没有一处告诉你它们互相
冲突在哪**。真正的取舍不在单条事务上，而在**两件事都要占同一天**。

典型形状（都是库里已有的事实，不是推论）：

* A：对外承诺，已过承诺日 3 天，对方在等回话（11.19 ``promises``）
* B：自己的截止日还剩 2 天，无对外承诺
* 今天只剩半天

→ 摆出来的不是「A 权重 45 > B 权重 40」，而是**「一件对外、一件只对自己，
都要占今天 —— 先动哪件由你定」**。

## 三条纪律（每条都有前置出处，不是本模块自创）

1. **代价项必须可对账**（``engine_errors.py:39``「宁可给不出建议，也不给错建议」
   的同一条纪律的推广）。每一项代价带一个**出处**——库里的列名。
   推论（"对方会失望"）**没有出处**，所以**不写进代价**。
   这条是本模块能不能存在的**前提**：一旦代价里混进推论，它就从「帮你看清」
   变成「让你信我」，而 19.2 第 2 条会因此反向生效 ——
   **武断 → 用户自我审查 → 数据变薄 → 价值归零**。
2. **事实不足就不给方案**（同上）。推不出真取舍就返回 ``None``，
   不硬凑、不降格成「建议做 A」。
3. **方案用陈述句，不用祈使句**（``delegate.py:551``「指令类语句是自动决策的
   前一步」）。措辞里出现「你应该」「建议」「最好」即视为违规。

## 它刻意不做的事

| 不做 | 为什么 |
|---|---|
| **不排序、不筛选** | 本模块只**摆**，不决定谁先谁后。摆出来的每一件都还在队列里，主人自己挑 |
| **不读人的决策** | 不从审批流学倾向（11.9.11 第 ② 条）。本模块**零输入依赖人点过什么** |
| **不给「建议做 X」** | 那是筛选层（决定看不看得到），有损 —— 可能漏掉。摆代价是呈现层，无损 |
| **不猜「要几小时」** | 本仓没有工时事实。工时是推论，**推论不进代价**（所以也没有「4 小时切 1+3」那种第三方案） |
| **不做槽位级争用** | 精力档只有三档、每档 5～7 小时宽 —— 两件事都在「早晨」并不构成争用。**档太粗，说不硬**，所以只用日期 |
| **不建真源** | 纯派生视图：不写库、不加列、不改 schema（与 ``personal_model.py`` 同一条纪律） |

## 「真取舍」的判据

三个都成立才算一组取舍，否则不给：

* **都还没结束**（``state`` 非终态）
* **争同一天**：两件事各自「要占的那天」有交集。这是**日期级争用** ——
  一天只有一份力气，这是可对账的硬约束
* **都不是纯等候**：``kind == WAIT`` 的事务是**在等别人** —— 它占不了
  你的时间，跟任何事都不冲突

**「代价性质不同」不是门槛，是呈现层的增强** —— 这一条最初写错过，
被测试逮住（两件都同一天到期、性质相同的事，此前被判成「不是取舍」）。

差别很要紧：**取舍成立看互斥（争同一天），性质差异只决定「怎么摆」。**
今天有两件事到期、都是自己的截止日 —— 它们**仍然是取舍**（今天只够做一件），
只是「为什么是取舍」这句话里没有「一件对外一件不对外」可讲，
得改讲「两件的代价性质相同」。

把性质差异当门槛会造成一个具体的坏后果：**最常见的取舍（同一天两件事）
恰好被过滤掉**，剩下只有「性质不同」的那种才被摆出来 —— 而那种本来
就更该被看见。这个偏差会把系统推向「只摆容易的取舍」。

**另外两条最容易漏**：

* **没有交集就不是取舍**：A 今天到期、B 下周三到期 —— 摆成「二选一」是
  **假冲突**，比不摆更坏（它会让主人以为必须放弃一件）。
* **超载不是取舍**：同一天挤了 8 件 —— 那不是「挑一个」，是**超载**，
  是另一件事（本仓 ``/week`` 的顺延统计在管超载）。所以列出的件数有上限
  （:data:`MAX_OPTIONS`）。

## 为什么「要占哪一天」这样算

一天一份力气 —— 所以争用的单位是**日期**。每件事要的日期来自三处，
全部是库里已有列：

* ``tasks.scheduled_for`` —— 排在那天
* ``tasks.due_time`` —— 自己的硬期限
* ``promises.promised_for`` —— 对外承诺的期限

**已过期的那天不算「要占今天」** —— 过期的事是**现在**就该做，
它占的是「此刻」，不是某个未来日期。它与今天到期的事冲突（都抢现在），
但不与下周三到期的事冲突。

**没有日期的事不参与争用**：没排期、没期限、没承诺 —— 它「随时能做」，
不占任何具体的一天。摆进取舍会稀释信号。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Sequence

from ..domain import Promise, PromiseStatus, Task, TaskKind, TaskState
from ..domain.models import ScoredTask

__all__ = [
    "CostFacet",
    "WeighOption",
    "WeighResult",
    "FACET_EXTERNAL",
    "FACET_SELF_ONLY",
    "FACET_HARD_DEADLINE",
    "FACET_NO_DEADLINE",
    "FACET_WAITING_ON_ME",
    "FACET_WAITING_ON_OTHER",
    "weigh",
]


# ---------------------------------------------------------------------------
# 代价维度
# ---------------------------------------------------------------------------
#
# 每一项代价的**取值**只能来自库里已有列，所以它是可对账的：
#
#   FACET_EXTERNAL         ← promises 有行且 status=open（我欠了别人）
#   FACET_SELF_ONLY        ← 无未兑现承诺（只影响我自己）
#   FACET_HARD_DEADLINE    ← tasks.due_time 或 promises.promised_for 有值
#   FACET_NO_DEADLINE      ← 两者皆空
#   FACET_WAITING_ON_ME    ← tasks.blocked_by 非空（有别的事在等我）
#   FACET_WAITING_ON_OTHER ← tasks.waiting_on 非空（我在等别人）
#
# **「代价的性质不同」用两两取值不同来判定**，不引入「代价大小」——
# 大小需要一个标量，而「谁更急」是排序，那是筛选层的事。

FACET_EXTERNAL = "external"
FACET_SELF_ONLY = "self_only"
FACET_HARD_DEADLINE = "hard_deadline"
FACET_NO_DEADLINE = "no_deadline"
FACET_WAITING_ON_ME = "waiting_on_me"
FACET_WAITING_ON_OTHER = "waiting_on_other"

#: 一组取舍最多列几件事。
#:
#: 为什么是 3：**取舍是「你在挑哪一个」，不是「你在清库存」**。
#: 同一天挤了 8 件事 —— 那不是取舍，是**超载**，那是另一件事
#: （本仓有 ``/week`` 的顺延统计在管超载，不该在这里混着说）。
#: 3 件是「挑一个」的直觉上限；超过就说明这天是超载而不是取舍。
MAX_OPTIONS = 3


@dataclass(frozen=True, slots=True)
class CostFacet:
    """一项代价。

    **``source`` 是这个模块的关键设计**：它写清这一项事实来自库里哪一列。
    有了它，「代价可对账」就不是一句承诺，而是**可机械核对**的 ——
    每项都能指回一个真实存在的列名。

    **``source`` 为空的代价项不允许存在**（:func:`weigh` 只构造带 source 的项；
    测试守这条）。
    """

    code: str
    #: 面向用户的中文说明。**只陈述事实**，不含评价与祈使
    #: （"对方会失望"是推论，不许出现；"承诺已过 3 天"是事实，可以）。
    text: str
    #: 出处：库里的列 / 那个表。**非空**。
    source: str


@dataclass(frozen=True, slots=True)
class WeighOption:
    """一个候选方案 —— 一件待取舍的事 + 它的代价清单。

    **它不是「建议」**：它是一件事及其代价。选不选是主人的事。
    """

    task: Task
    facets: tuple[CostFacet, ...]
    #: 这件事要占哪一天（``date``）。它是争用的单位。
    day: date

    def describe(self) -> str:
        """一行摘要（不含祈使句）。"""
        return f"{self.task.title} —— " + "；".join(f.text for f in self.facets)


@dataclass(frozen=True, slots=True)
class WeighResult:
    """一组取舍。

    **至少两件**（真取舍是二元的），**最多三件**（见 :data:`MAX_OPTIONS`）。
    不摆成一长列 —— 那样会让人以为「挑一个出来」，而实际是
    「在几件之间选」，而且长列是超载不是取舍。
    """

    options: tuple[WeighOption, ...]
    #: 争的是哪一天（人话）。
    conflict_text: str
    #: 为什么这几件互相冲突 —— 只陈述事实。
    because: str


def _open_promise(promise: Promise | None) -> Promise | None:
    """只认**未兑现**的承诺。

    已兑现 / 已收回的不算代价 —— 那件事已经过去了。
    """
    if promise is None:
        return None
    if promise.status is not PromiseStatus.OPEN:
        return None
    return promise


def _facets_for(
    task: Task,
    promise: Promise | None,
    now: datetime,
) -> tuple[CostFacet, ...]:
    """算出一条事务的代价清单。

    **每一项都带 source**，且 source 写的是真实存在的列名。
    推论一律不进 —— 见模块 docstring 的纪律 1。
    """
    facets: list[CostFacet] = []
    open_promise = _open_promise(promise)

    # 维度一：对外 / 只对自己
    if open_promise is not None:
        facets.append(
            CostFacet(
                code=FACET_EXTERNAL,
                text=f"对外承诺未兑现，承诺日 {open_promise.promised_for:%Y-%m-%d}",
                source="promises.promised_for",
            )
        )
    else:
        facets.append(
            CostFacet(
                code=FACET_SELF_ONLY,
                text="无未兑现的对外承诺",
                source="promises.status",
            )
        )

    # 维度二：有没有硬期限
    deadline = task.due_time
    deadline_source = "tasks.due_time"
    if deadline is None and open_promise is not None:
        deadline = open_promise.promised_for
        deadline_source = "promises.promised_for"
    if deadline is not None:
        delta = deadline - now
        if delta < timedelta(0):
            facets.append(
                CostFacet(
                    code=FACET_HARD_DEADLINE,
                    text=f"硬期限已过 {-delta.days} 天",
                    source=deadline_source,
                )
            )
        else:
            facets.append(
                CostFacet(
                    code=FACET_HARD_DEADLINE,
                    text=f"硬期限还剩 {delta.days} 天",
                    source=deadline_source,
                )
            )
    else:
        facets.append(
            CostFacet(
                code=FACET_NO_DEADLINE,
                text="无硬期限",
                source="tasks.due_time",
            )
        )

    # 维度三：关系方向 —— 有人在等我 / 我在等别人
    if task.blocked_by:
        facets.append(
            CostFacet(
                code=FACET_WAITING_ON_ME,
                text=f"有 {len(task.blocked_by)} 件事在等它",
                source="tasks.blocked_by",
            )
        )
    elif task.waiting_on is not None:
        facets.append(
            CostFacet(
                code=FACET_WAITING_ON_OTHER,
                text=f"在等 {task.waiting_on.who_or_what}",
                source="tasks.waiting_on",
            )
        )

    return tuple(facets)


def _days_claimed(
    task: Task,
    promise: Promise | None,
    today: date,
) -> frozenset[date]:
    """这件事要占哪几天。

    来自三处已有列：``tasks.scheduled_for`` / ``tasks.due_time`` /
    ``promises.promised_for``。

    **已过期的那天换成今天** —— 过期的事是**现在**就该做，它占的是此刻，
    不占某个未来日期。它与今天到期的事冲突（都抢现在），但不与下周三
    到期的事冲突。

    **没有日期就不参与争用**：没排期、没期限、没承诺 —— 它「随时能做」，
    不占任何具体的一天。摆进取舍会稀释信号。
    """
    days: set[date] = set()

    if task.scheduled_for is not None:
        days.add(task.scheduled_for)

    deadlines: list[datetime] = []
    if task.due_time is not None:
        deadlines.append(task.due_time)
    open_promise = _open_promise(promise)
    if open_promise is not None and open_promise.promised_for is not None:
        deadlines.append(open_promise.promised_for)

    for deadline in deadlines:
        day = deadline.date()
        days.add(day if day >= today else today)

    return frozenset(days)


def _is_open(task: Task) -> bool:
    return task.state in (TaskState.INBOX, TaskState.ACTIVE, TaskState.BLOCKED)


def _first_difference(
    facets_a: Sequence[CostFacet], facets_b: Sequence[CostFacet]
) -> str | None:
    """找出两件东西代价上**第一处不同**，并用事实说出来。

    返回一句话，形如「一件是对外承诺未兑现，一件无未兑现的对外承诺」。
    **只陈述差异，不评价谁更重要** —— 那是排序要做的事。

    比较的是**维度取值**：facet 的 code 是「维度 + 取值」合一的，
    所以先按维度分组，再看两边取值是否不同。

    **返回 ``None`` 不是「不是取舍」** —— 它只表示「两件代价性质相同，
    没有可陈述的差异」。取舍成立看的是互斥（同一天），不是差异。
    """
    by_code_a = {f.code: f for f in facets_a}
    by_code_b = {f.code: f for f in facets_b}

    # 维度 → (取值 A, 取值 B)。同一维度下取值不同 = 代价不在一处。
    dimensions = (
        (FACET_EXTERNAL, FACET_SELF_ONLY),
        (FACET_HARD_DEADLINE, FACET_NO_DEADLINE),
        (FACET_WAITING_ON_ME, FACET_WAITING_ON_OTHER),
    )
    for code_a, code_b in dimensions:
        fa = by_code_a.get(code_a)
        fb = by_code_b.get(code_b)
        if fa is not None and fb is not None:
            return f"一件是「{fa.text}」，另一件是「{fb.text}」"
        # 反向：一边是 B 取值，另一边是 A 取值
        fa2 = by_code_a.get(code_b)
        fb2 = by_code_b.get(code_a)
        if fa2 is not None and fb2 is not None:
            return f"一件是「{fa2.text}」，另一件是「{fb2.text}」"
    return None


def _shared_shape(facets: Sequence[CostFacet]) -> str | None:
    """两件代价性质相同时，说清它们「同在哪」。

    为什么需要这一句：**差异不是取舍的门槛**（见模块 docstring）。
    两件同一天到期、都是自己的截止日 —— 仍然是取舍，只是没有差异可讲。
    什么都不说会让主人以为「这两件没区别」，而事实是「它们性质一样，
    所以差别在别处（内容、你要不要做）」—— 那句话本身是有用的信息。
    """
    if not facets:
        return None
    return "两件的代价性质相同 —— " + "、".join(f.text for f in facets)


def weigh(
    scored: Sequence[ScoredTask],
    now: datetime,
    *,
    today: date,
    promises_by_task: dict[str, Promise] | None = None,
) -> WeighResult | None:
    """算出一组取舍；**没有真取舍就返回 ``None``**。

    ``None`` 是本模块最重要的返回值 —— 它意味着「现在没有值得拍板的东西」。
    宁可不出，也不硬凑（``engine_errors.py:39``）。

    :param scored: 已排序的事务（``sorting.score_and_sort`` 的输出）。
    :param now: 当前时刻。
    :param today: 当前日期。
    :param promises_by_task: task_id → 承诺（**只取未兑现的**）。
    """
    promises_by_task = promises_by_task or {}

    # 只考虑还没结束、且不是纯等候的
    #
    # **判据用 ``kind == WAIT``，不用「有 waiting_on」** —— 这两条不一样，
    # 而 4.3 的不变式把它们锁在一起：未结束的事务里，
    # ``state == BLOCKED`` **必然**有 ``waiting_on``。所以按 waiting_on 判
    # 等于把所有 BLOCKED 都排除掉 —— 那会把「卡在流程里」（等审批、
    # 等材料）的事误判成「不占时间」，而它明明占着你今天。
    #
    # ``kind`` 是**形状**（这件事是什么性质），``waiting_on`` 是**对象**
    # （在等谁）。一个 ACTION 事务也可以是 BLOCKED —— 它在等，但不是
    # 「在等别人」，是卡住了。卡住的事照样要花时间。
    candidates = [
        s.task
        for s in scored
        if _is_open(s.task) and s.task.kind is not TaskKind.WAIT
    ]
    if len(candidates) < 2:
        return None

    facets_by_task = {
        t.id: _facets_for(t, promises_by_task.get(t.id), now) for t in candidates
    }
    days_by_task = {
        t.id: _days_claimed(t, promises_by_task.get(t.id), today)
        for t in candidates
    }

    # 按「要占的那天」分组 —— 争用只发生在同一天之间
    by_day: dict[date, list[Task]] = {}
    for task in candidates:
        for day in days_by_task[task.id]:
            by_day.setdefault(day, []).append(task)

    # 争用最激烈的那天 = 要占这天的件数最多的那天
    best_day: date | None = None
    best_group: list[Task] = []
    for day in sorted(by_day):
        group = by_day[day]
        if len(group) > len(best_group):
            best_day, best_group = day, group
    if best_day is None or len(best_group) < 2:
        return None

    # 为什么是取舍 —— 优先说「性质差异在哪」；没有差异就说「性质相同」
    #（**差异不是门槛**：取舍成立看的是互斥，不是差异。见模块 docstring）
    reason = _reason_for(best_group, facets_by_task)
    if reason is None:
        return None

    options: list[WeighOption] = []
    for task in best_group[:MAX_OPTIONS]:
        options.append(
            WeighOption(
                task=task,
                facets=facets_by_task[task.id],
                day=best_day,
            )
        )

    return WeighResult(
        options=tuple(options),
        conflict_text=f"都要占 {best_day:%Y-%m-%d} 这一天",
        because=reason,
    )


def _reason_for(
    group: Sequence[Task], facets_by_task: dict[str, tuple[CostFacet, ...]]
) -> str | None:
    """这一组为什么是取舍 —— 找出代价性质不同的那一对。

    **性质相同也是取舍**（同一天就是互斥），所以找不到差异时改说
    「性质相同」而不是放弃 —— 见 :func:`_shared_shape`。
    """
    for i, left in enumerate(group):
        for right in group[i + 1:]:
            reason = _first_difference(
                facets_by_task[left.id], facets_by_task[right.id]
            )
            if reason is not None:
                return reason
    # 没有差异 —— 性质相同，但仍然互斥（争同一天）
    return _shared_shape(facets_by_task[group[0].id])
