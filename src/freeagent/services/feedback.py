"""执行层反馈回流：**从记录里读出「派得怎么样」**，只读，不做任何决定。

## 存在的理由（2026-10-17 架构自审）

一次自审发现两件事，它们其实是**同一件事的两面**：

1. 委派失败只在任务上留一条 ``delegation_failed`` 文本。第三次踩同一个坑
   不会被提示 —— 那些记录**躺在库里，没人读**。
2. 产出验收（``ArtifactService.accept``）只改 artifact 状态 + 追加一条记录，
   **不影响任何排序信号、不进恢复契约**。于是「上次这么做被要求改了两次」
   这个事实，对下一次派发毫无影响。

合起来是一句话：**执行层的信息没有系统性地回流到治理层。**
而那恰恰是「Agent 之上的 Agent」最核心的那个动作 —— 上层存在的意义
就是从下层的结果里学到东西。

## 刻意不做的事（这是本模块存在的意义所在）

| 不做 | 为什么 |
|---|---|
| **不自动重派** | 与 11.10.5「绝不自动触发委派」同构：判断「该不该再派」是弱信号 + 不可复核，用户看不到也反驳不了，而派错的代价不对称 |
| **不进排序打分** | 与第七章「不替用户做取舍」同构。反馈**可查**，但不**替用户决定** |
| **不给失败加标签** | 沿用 ``attempt_state`` 的纪律：判据从**记录形状**推出来，不给历史贴分类标签 —— 标签会把「这次的形状」固化成「这个东西的属性」 |
| **不新增表** | 全部结论都从 ``task_records``（唯一真源）现推 |

最后一条尤其重要：本模块**只读**。它不写库、不改状态、不发提醒。
真要据此行动，那是别处的决定 —— 这个模块只负责「把事实摆出来」。

## 为什么这不算「通用云端助手也能做」

19.2 那条判据要问的是「一个通用助手能不能替我做」。这里不是替我做决定，
而是**从本机执行记录里读出我自己派过的东西的履历** —— 那份记录只有本仓
有（任务 / 产物 / 只追加日志三张表的关系），云端助手拿不到，而把它算出来的
逻辑（``attempt_state`` 那套形状推导）也只在本仓。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import Sequence

from ..domain import RecordType
from ..domain.models import TaskRecord
from .delegate import attempt_state

__all__ = ["DelegationOutcome", "FeedbackReport", "read_outcome", "feedback_report"]


@dataclass(frozen=True, slots=True)
class DelegationOutcome:
    """一次委派尝试的结局。**从记录形状推出来的，不是标签。**"""

    #: 成功 / 执行期失败 / 派发前被拒 / 从没试过。
    outcome: str
    #: 失败时的错因首行（若有）。**原样截取，不归类** ——
    #: 归类就成了给失败贴标签，而那正是 ``attempt_state`` 刻意不做的事。
    reason: str = ""
    #: 这次尝试之后，用户对产出做了什么（12.9 之后的验收闭环）。
    verdict: str = ""


@dataclass(frozen=True, slots=True)
class FeedbackReport:
    """一批事务的执行层履历。**只读结论，不含建议动作。**

    刻意**没有** ``should_retry`` / ``next_action`` 这类字段：
    那是替用户做决定，而第七章要求的是「摆得更清楚 ≠ 替你决定」。
    """

    total: int
    succeeded: int
    failed: int
    refused: int
    never: int
    #: 同类错因计数（错因文本 → 次数），降序。
    top_reasons: tuple[tuple[str, int], ...]
    #: 验收结论计数：``accepted`` / ``revised`` / ``dropped`` / 空。
    verdicts: tuple[tuple[str, int], ...]
    #: 被反复要求返工的产出（>=2 次返工），按返工次数降序。
    revised_tasks: tuple[tuple[str, str, int], ...]

    @property
    def success_rate(self) -> float | None:
        """成功占比。**分母只算真跑过的那些** —— 派发前被拒不该算失败。

        返回 ``None`` 表示「一次都没真跑过」，那与「成功率 0%」是**两回事**：
        后者意味着「跑了但全失败」，前者意味着「根本没开始」，
        而这两者该做的下一步完全不同。
        """
        ran = self.succeeded + self.failed
        if ran == 0:
            return None
        return self.succeeded / ran


def read_outcome(records: Sequence[TaskRecord]) -> DelegationOutcome:
    """从一条事务的记录里读出它的委派结局与验收结论。

    两部分**分别**判定，因为它们来自两条独立的链：

    * 结局复用 :func:`~freeagent.services.delegate.attempt_state`
      —— **不重写一遍推导**。两份推导一旦漂移，「这里说成功」与
      「派发守卫说已派过」就会对不上，而那种不一致只在真跑起来时暴露。
    * 验收结论从产物记录里读：``artifact_accepted`` 是采纳，
      ``artifact_created`` **多于**一次是返工（用户圈改重做过）。

    ## 同刻记录的顺序前提（V1.37 起由 seq 兜底）

    ``RecordRepo.list_for_task`` 曾是 ``ORDER BY ts ASC, id ASC``，而
    ``id`` 是 ``new_id()`` 随机生成的 —— 同一条时间戳上的多条记录顺序随机
    （实测 2026-10-17：``delegation_dispatched`` 与 ``delegation_failed``
    同微秒落库后，:func:`attempt_state` 可能判成 ``refused`` 而不是
    ``failed``，**结论整个反过来**）。

    **V1.37 已根治**：``task_records.seq``（每任务单调）落列，读侧
    ``list_for_task`` 改 ``ts ASC, seq ASC``，同刻顺序 = 写入顺序，
    确定性。本模块的输入合同不变：传进来的记录**必须已按读取顺序排好**
    （调用方都会用 ``list_for_task``）—— 判据从记录形状推导这一纪律
    不变，变的只是形状本身不再随机。

    写测试仍须给每条记录**递增**时间戳或用 ``RecordRepo.append`` 取真序号；
    手工拼记录列表时同刻多条的顺序由你自己的列表序决定。
    """
    _in_flight, last = attempt_state(records)

    reason = ""
    for record in reversed(records):
        if record.type is RecordType.DELEGATION_FAILED:
            reason = record.content.strip()
            break
        if record.type is RecordType.DELEGATION_SUCCEEDED:
            break

    accepted = any(
        r.type is RecordType.ARTIFACT_ACCEPTED for r in records
    )
    drafts = sum(1 for r in records if r.type is RecordType.ARTIFACT_CREATED)
    verdict = ""
    if accepted:
        verdict = "accepted"
    elif drafts >= 2:
        # 两版以上草稿且没采纳 = 用户圈改重做过至少一次。
        verdict = "revised"
    elif drafts == 1:
        verdict = ""

    return DelegationOutcome(outcome=last or "never", reason=reason, verdict=verdict)


def feedback_report(
    task_ids: Sequence[str],
    records_for: Callable[[str], Sequence[TaskRecord]],
    title_of: Callable[[str], str] | None = None,
) -> FeedbackReport:
    """读一批事务的执行层履历。

    :param records_for: ``task_id -> Sequence[TaskRecord]`` 的取值函数。
        传函数而不传仓储，是为了**不与存储层耦合** ——
        本模块的每一条结论都能从「一组记录」算出来，
        而测试可以用一个 dict 替代整个数据库。
    :param title_of: ``task_id -> 标题``，只用于 ``revised_tasks`` 那几行。
        缺省就不列 —— **没给就不猜**，而不是回填一个 ``task_id``
        让用户自己认（那正是 8.2「文本必须能从本函数持有的事实推出」的反例）。
    """
    counts: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    verdicts: Counter[str] = Counter()
    revisions: Counter[str] = Counter()

    for task_id in task_ids:
        outcome = read_outcome(records_for(task_id))
        counts[outcome.outcome] += 1
        if outcome.outcome == "failed" and outcome.reason:
            reasons[outcome.reason] += 1
        if outcome.verdict:
            verdicts[outcome.verdict] += 1
        if outcome.verdict == "revised":
            revisions[task_id] += 1

    revised: list[tuple[str, str, int]] = []
    if title_of is not None:
        for task_id, n in revisions.most_common():
            revised.append((task_id, title_of(task_id), n))

    return FeedbackReport(
        total=len(task_ids),
        succeeded=counts["succeeded"],
        failed=counts["failed"],
        refused=counts["refused"],
        never=counts["never"],
        top_reasons=tuple(reasons.most_common(5)),
        verdicts=tuple(verdicts.most_common()),
        revised_tasks=tuple(revised),
    )