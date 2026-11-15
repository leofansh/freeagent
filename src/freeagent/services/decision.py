"""决策日志：拍板当时的快照，到期与现况对峙（设计文档 7.6 / 19.5.4 候选 D）。

**它补的是判断层的另一半**：7.5（代价对照）逼主人选，但**选完就忘了** ——
下一周再遇到类似的取舍，那次的判断依据无处可查，于是每次都重新想。
本章把「当时那一板」记下来，让「现在」能跟「当时」对质。

## 为什么存快照，而不是存 task_id

19.5.4 的原话是「带**时间戳的认知快照**：为什么买、当时什么价、当时的判断依据，
到期自动拉出来与现况对峙」。**「当时的」是硬要求**。

若只存 ``task_id``，对峙时会用**今天**的库重算「当时的代价对比」——
而那份对比会随事实漂移：承诺兑现了、期限过了、事务被删了，
「当时 A 是对外承诺未兑现」这句话在今天算出来可能是「A 已兑现」。
于是对峙变成「拿今天的事实跟今天的事实比」，什么也照不出来。

所以本表存**落那一刻的冻结快照**：每个选项的标题、它的每项代价
（文本与出处）、以及当时的对比理由 —— **逐字冻结**，之后不再变。

**但 ``task_id`` 也在快照里** —— 它只用于「现在」那一侧**去查现况**，
**不用它重建当时的文本**（那会漂移）。这两件事分开，是本模块的关键。

## 三条纪律

1. **只摆事实，不评价**。对峙只回答「当时选了谁、当时的理由是什么、
   现在这几件事各自什么状态」。**它不说「你选对了」也不说「你选错了」** ——
   那是替人下结论（7.1 / 〇.8.4）。判断留给主人自己。
2. **不读人的决策来推断倾向**。本模块**只写不读**人的审批决策
   （11.9.11 第 ② 条）：它记的是「主人自己的一次选择」，
   不参与任何权重、排序或偏好推断 —— 那是筛选层，本仓不做（7.5.5）。
3. **事实不足就如实说**。快照里没有的事（如「当时没排期」），现在也
   **不补一句推测**。查不到的事务如实报「已经不在库里了」，不静默跳过。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Sequence

from ..domain import DecisionLog, Task
from .weigh import WeighOption, WeighResult

__all__ = [
    "CONFRONT_AFTER_DAYS",
    "Confrontation",
    "DecisionService",
    "FrozenOption",
    "RecordResult",
    "freeze_options",
    "parse_frozen_options",
]


#: 到期对峙的间隔（天）。
#:
#: **为什么是 7 天而不是「当天」**：7.5 摆出的取舍争的是**某一天**，
#: 但主人是当天拍的板 —— 当天就拉出来对峙，看到的是「事情还没发生」，
#: 对峙不出任何东西。要等到**那一周过去**，现况才有内容可说。
#:
#: **为什么不是「无限期」**：不设上限的话，快照会永远堆着，
#: 而 19.5.4 的原话是「**到期**自动拉出来」—— 到期是一次性的事。
CONFRONT_AFTER_DAYS = 7


@dataclass(frozen=True, slots=True)
class FrozenOption:
    """一个选项的**冻结快照**。

    ``facets`` 里是落那一刻的原文 —— 它们**不随事实变化**。
    """

    task_id: str
    title: str
    #: 当时的代价项（文本 + 出处），逐字冻结。
    facets: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class Confrontation:
    """一次对峙：当时的快照 + 现在的状态，并排。

    **只摆事实**（纪律 1）。它**不**判断「当时选的那个」和「现在完成的那个」
    是不是同一个 —— 那是评价，主人自己看。
    """

    #: 当时的快照。
    decision: DecisionLog
    #: 当时选中的那一项（从冻结快照里取）。
    chosen: FrozenOption | None
    #: 现在这几件事各自的状态（一行一条，人话）。
    current_lines: tuple[str, ...]
    #: 现在已经不在库里了的事（被删）—— 那几件的标题。
    vanished_titles: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RecordResult:
    """一次拍板的落库结果。

    **为什么要带 ``is_new``**：回执要能区分两种情况 ——
    「这一板刚记下」与「这一板今天已经记过，还是那一条」。
    两者都说成「记下了」就是把事实说错了：后者库里并没有多出一行。
    """

    #: 落库（或命中已有）的那条快照。
    decision: DecisionLog
    #: 是不是**新**落了一条。False = 今天已经拍过同一块板、选了同一个。
    is_new: bool


def freeze_options(options: Sequence[WeighOption]) -> str:
    """把当时的选项清单冻结成一段 JSON 文本。

    **JSON 而不是拼接文本**：``task_id`` 必须能查现况，而文本行拆不出
    id（标题可能重名、可能含换行）。JSON 让「冻结」与「可查」各取所需，
    两者不互相污染。

    **facets 存 ``[文本, 出处]`` 二元组**而不是对象 —— 形状固定，
    且 JSON 读出来不用管键名。
    """
    payload = [
        {
            "task_id": option.task.id,
            "title": option.task.title,
            "facets": [[facet.text, facet.source] for facet in option.facets],
        }
        for option in options
    ]
    return json.dumps(payload, ensure_ascii=False)


def parse_frozen_options(options_json: str) -> tuple[FrozenOption, ...]:
    """把冻结快照读回结构。

    **读不出来就抛** —— 快照读不懂等于这条决策不存在，
    而「静默跳过」会让人以为没拍过板（那正是本仓最坏的失效形态：
    看起来在工作，其实少了一块）。
    """
    try:
        raw = json.loads(options_json)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"决策快照读不出来：{options_json[:60]!r}") from exc
    if not isinstance(raw, list):
        raise ValueError("决策快照的形状不对：期望数组")

    frozen: list[FrozenOption] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("决策快照里有一项形状不对")
        facets_raw = item.get("facets") or []
        if not isinstance(facets_raw, list):
            raise ValueError("决策快照里的 facets 形状不对")
        facets: list[tuple[str, str]] = []
        for facet in facets_raw:
            if not isinstance(facet, (list, tuple)) or len(facet) != 2:
                raise ValueError("决策快照里有一项代价形状不对")
            facets.append((str(facet[0]), str(facet[1])))
        frozen.append(
            FrozenOption(
                task_id=str(item.get("task_id", "")),
                title=str(item.get("title", "")),
                facets=tuple(facets),
            )
        )
    return tuple(frozen)


class DecisionService:
    """决策日志：记一次拍板 / 到期时拉出来与现况对峙。

    **它不读人的审批决策**（纪律 2）。本服务只接受
    :class:`~freeagent.services.weigh.WeighResult` —— 那是系统摆出来的
    事实，不是人的倾向信号。
    """

    def __init__(self, decisions, tasks, clock) -> None:
        self._decisions = decisions
        self._tasks = tasks
        self._clock = clock

    # ---- 记 ---- #

    def record(self, result: WeighResult, chosen_index: int) -> RecordResult:
        """记一次拍板。

        :param result: 7.5 摆出来的那组取舍。
        :param chosen_index: 主人选了第几个（**0 基**）。

        **越界即抛** —— 主人不可能选一个不存在的选项。静默夹到范围内
        会把「选错了」变成「记了别的那个」，那比报错坏得多。

        **同一板、同一选择、同一天 → 只记一条**（幂等）：
        连按两次 ``/weigh 1`` 不该在日志里留下两条一模一样的东西 ——
        那不是两次拍板，是一次拍板被记了两遍。

        **判据是「逐字相同」而不是「同一组任务」**：
        冻结清单连**顺序**一起比。顺序是板的一部分（主人选的是「第 1 个」），
        顺序变了就是板的样子变了 —— 那种情况宁可多记一条，
        也不把两次不同的思考合并成一条。

        **「改了主意」要留两条**：换了选择就是**另一板**，
        逐字相同（同一组选项 + 同一份代价文本 + 同一个顺序 + 同一个选择）
        才是同一板。

        **跨天另记一条**：那是另一次想（哪怕结论一样），
        而且对峙日按各自拍板日 + 7 天算，合成一条会让后来的那次提前对质。
        """
        if not 0 <= chosen_index < len(result.options):
            raise ValueError(
                f"选了第 {chosen_index + 1} 个，但当时只摆了 {len(result.options)} 个"
            )

        now = self._clock.now()
        options_json = freeze_options(result.options)

        # 幂等：与**今天最近的那条**逐字相同 → 那就是同一次拍板，
        # 返回它，不另记一条。
        #
        # **只跟「今天最近的一条」比，不跟历史里任何一条比** ——
        # 「选了甲 → 改成乙 → 又选回甲」是真实的三次摇摆（两次甲一次乙），
        # 跟最久远那条甲合并会把「最后落在甲」这件事弄丢。
        previous = self._decisions.latest_on(now.date())
        if (
            previous is not None
            and previous.options_json == options_json
            and previous.chosen_index == chosen_index
        ):
            return RecordResult(decision=previous, is_new=False)

        # 对峙日 = 拍板那天 + 7（见 CONFRONT_AFTER_DAYS 的理由）
        confront_on = now.date() + timedelta(days=CONFRONT_AFTER_DAYS)
        decision = self._decisions.add(
            chosen_index=chosen_index,
            options_json=options_json,
            reason_text=result.because,
            conflict_text=result.conflict_text,
            decided_at=now,
            confront_on=confront_on,
        )
        return RecordResult(decision=decision, is_new=True)

    # ---- 读 ---- #

    def due(self, today: date) -> list[DecisionLog]:
        """今天该拉出来对峙的快照。"""
        return self._decisions.due_on(today)

    def history(self) -> list[DecisionLog]:
        """全部拍板快照（最近的先列）。"""
        return self._decisions.list_all()

    # ---- 对峙 ---- #

    def confront(self, decision: DecisionLog) -> Confrontation:
        """把一条快照与**现在**并排摆出来。

        **「现在」从库里现查**（这是它与「当时」的区别）：
        快照冻结在落库那一刻，但事务的状态会往前走。

        **被删的事务如实报「已经不在库里了」**，不静默跳过 ——
        少一条就看不出「当时摆了三件、现在只查得到两件」。
        """
        frozen = parse_frozen_options(decision.options_json)
        chosen: FrozenOption | None = None
        if 0 <= decision.chosen_index < len(frozen):
            chosen = frozen[decision.chosen_index]

        current_lines: list[str] = []
        vanished: list[str] = []
        for option in frozen:
            task = self._tasks.find(option.task_id) if option.task_id else None
            if task is None:
                vanished.append(option.title)
                continue
            current_lines.append(_current_line(task))

        return Confrontation(
            decision=decision,
            chosen=chosen,
            current_lines=tuple(current_lines),
            vanished_titles=tuple(vanished),
        )


def _current_line(task: Task) -> str:
    """一件事现在的状态（一行，人话）。

    **只陈述事实**：状态 + 期限，不评价「该不该做」「拖太久了」。
    """
    state = task.state.label
    if task.due_time is not None:
        return f"{task.title} —— {state}，期限 {task.due_time:%Y-%m-%d %H:%M}"
    if task.scheduled_for is not None:
        return f"{task.title} —— {state}，排 {task.scheduled_for}"
    return f"{task.title} —— {state}"
