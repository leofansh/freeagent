"""催办草稿：替人准备「该催的那句话」，但**绝不代发**（20.1.4）。

## 它解决什么

9.5 的 ``OVERDUE_WAIT`` 信号已经能说「该催张总了」，8.2 的 ``next_actions``
也能给「催一下 X」。缺的是最后一步：**说清楚了，用户仍然要自己从零敲
那条消息**。而「催一下」这件事最难的不是措辞，是**张冠李戴** ——
催错人、把上周的事催成这周的、把「我等你」写成「你欠我」。

所以这里做的**只有一件事**：把库里已有的事实（等谁、等了多久、答应过什么）
铺成一条可编辑的草稿，剩下的**留白**。

## 四条纪律（本模块的存在理由）

1. **起草不发送，且系统没有发送能力**。这不是承诺，是**结构**：
   本模块只产出文本，**不持有任何通道对象**（没有 sender、没有 client）。
   想「顺手发出去」的人会发现没有那条路 —— 与其靠自律，不如让路径不存在
   （同 11.10.5「绝不自动触发委派」：不可逆的动作不该只靠一条纪律守着）。
2. **只显式触发，绝不静默生成**。``/nudge`` 是用户敲的命令；系统**不会**
   在 ``/today`` 里偷偷起草好。理由与 11.19.3 同型：从数据里猜「用户想
   催谁、催什么」是弱信号，而生成一条带倾向的文本比生成一句提醒重得多。
   **宁可漏，不可擅自动手**（第十五章）。
3. **只复述库里有的事实，其余一律留白**。这是第十章 ``[TODO]`` 纪律的
   延伸：草稿里出现任何库中没有的信息（对方的邮箱、上次谈的结论、
   这次该给什么承诺），都是**编造**。所以模板只填事实位，
   语气、诉求、承诺这些**必须由用户自己敲**。
4. **不代替用户改关系状态**。催办草稿生成后，承诺**仍是 open**、
   等候**仍卡着**。催一次不等于账结了 —— 结算只能由用户显式动作
   （``/promise keep``、``/followup``），与 11.19.5 同一条纪律。

## 为什么落在「等候」而不是「承诺」上

20.1.4 的原话是「基于 ``waiting_on``（如「等张总回合同」）生成跟进草稿」。
本模块同时吃两路事实：

* ``WaitingOn``（9.1）—— **我等别人**。催办话术天然属于这一侧：
  「提醒对方动一动」。
* ``Promise``（11.19）—— **我答应别人**。这一侧的催办话术方向相反：
  是「告诉对方我要延期」，而不是「问对方进度」。

两路的**语气相反**，所以模板按方向分岔而不是混成一句。但它们共用一个
落点：同一套事实（等谁 / 等多久 / 承诺什么），以及同一条纪律（不代发）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..domain import Promise, Task, ValidationError
from .clock import Clock

__all__ = ["NudgeService", "NudgeTarget", "NUDGE_LEADIN", "NUDGE_DIRECTIONS"]

#: 草稿里那句一定要自己敲的地方。
#:
#: **刻意留白而不是给默认值**：填一个「以示礼貌」的默认句很容易，
#: 而它会造成一种「系统替我把话说完了」的错觉 —— 用户就真的会直接发。
#: 催办的分寸（急不急、软不软、给不给台阶）只有他知道。
NUDGE_LEADIN = "[TODO 开头：怎么称呼、先说什么]"

#: 两个方向的抬头写法 —— 这是**给你的参照**，不是要写进正文的话。
#:
#: 拆成 :data:`NUDGE_DIRECTIONS`（说明）与 :data:`_NUDGE_OPENING`（抬头模板）
#: 两张表，是因为方向是**事实**（这账是我欠的、还是我在等），
#: 而抬头是**措辞**（那半句话该怎么起）。混在一张表里的后果是：
#: 迭代措辞时会顺手改掉事实那一列。
NUDGE_DIRECTIONS = {
    "wait": "等我方动静 —— 问对方进度 / 提醒对方交付",
    "promise": "等我方交付 —— 告知延期 / 给出新时间",
}

#: 每个方向的抬头。**只给结构，把内容留空** —— 同 :data:`NUDGE_LEADIN`
#: 的理由：系统替用户把第一句写好，用户就会直接发出去。
_NUDGE_OPENING = {
    "wait": "[TODO 一句话说明来意（例如：想问下合同的进度）]",
    "promise": "[TODO 一句话说明现状（例如：报价要推到周五）]",
}


@dataclass(frozen=True, slots=True)
class NudgeTarget:
    """一条可催办的事务，及其已经确定的事实。

    **只装事实**，不含任何「该怎么催」的判断 —— 那属于模板，不属于数据。
    """

    task: Task
    #: ``wait`` = 我在等对方；``promise`` = 我欠对方。
    direction: str
    #: 关系对象的名字（11.19）；没挂关系时是 ``None``。
    contact_name: str | None
    #: 等了多久 / 逾了多久的自然语言短语，如「已过 3 天」。
    elapsed: str
    promise: Promise | None = None


class NudgeService:
    """催办草稿。**只读 + 拼文本**，不写任何表。"""

    def __init__(
        self,
        tasks,
        promises,
        contacts,
        clock: Clock,
    ) -> None:
        self._tasks = tasks
        self._promises = promises
        self._contacts = contacts
        self._clock = clock

    # -- 挑出该催的 ---------------------------------------------------------- #

    def targets(self, now: datetime | None = None) -> list[NudgeTarget]:
        """当前**该催**的事务，按「已经拖了多久」**降序**（最该催的在前）。

        两条入选口径，与两个信号的触发条件**逐字对齐**：

        * ``OVERDUE_WAIT``：``waiting_on.follow_up_at`` 已过（9.5）
        * ``PROMISE_OVERDUE``：承诺过了 ``promised_for`` 且仍 open（11.19.4）

        **刻意不在这里新增判定** —— 一旦催办有自己的「什么算该催」，
        它就会与排序信号漂移，而漂移的症状是「今天视图说该催，
        ``/nudge`` 却列不出来」。宁可让两边都指向同一个函数。

        排序键是**该催的那个时刻本身**（越早越靠前），不是 ``elapsed``
        那句中文 —— 用渲染文案排序会让改一次措辞就换一次顺序。
        """
        moment = now or self._clock.now()
        found: list[NudgeTarget] = []

        for task in self._tasks.list_open():
            waiting = task.waiting_on
            if waiting is not None and waiting.follow_up_at is not None:
                if waiting.follow_up_at <= moment:
                    found.append(
                        NudgeTarget(
                            task=task,
                            direction="wait",
                            contact_name=self._contact_name(waiting.contact_id),
                            elapsed=_elapsed(moment, waiting.follow_up_at, "已过"),
                        )
                    )
            promise = self._promises.list_for_task(task.id)
            if promise:
                open_one = next(
                    (p for p in promise if p.is_open), None
                )
                if (
                    open_one is not None
                    and open_one.promised_for is not None
                    and open_one.promised_for <= moment
                ):
                    found.append(
                        NudgeTarget(
                            task=task,
                            direction="promise",
                            contact_name=self._contact_name(open_one.contact_id),
                            elapsed=_elapsed(moment, open_one.promised_for, "已逾期"),
                            promise=open_one,
                        )
                    )
        # 越早该催的排越前（reverse=False 是刻意的：**反了**就成了
        # 「快到点的排最前」，而那恰恰是最不该先催的 —— 拖了三个月的
        # 会被今天刚过期的挤到最后。测试 test_long_ago_first 盯住这条。
        found.sort(key=lambda t: _due_at(t) or datetime.max)
        return found

    def count(self, now: datetime | None = None) -> int:
        return len(self.targets(now))

    # -- 出草稿 -------------------------------------------------------------- #

    def draft(self, target: NudgeTarget) -> str:
        """把一条催办铺成可编辑草稿。

        **返回的是文本，不是待发队列** —— 见模块纪律 1。
        每一处需要用户自己拿主意的位置都留 ``[TODO]``，见纪律 3。
        """
        task = target.task
        who = target.contact_name
        lines = [
            f"方向：{NUDGE_DIRECTIONS[target.direction]}",
            f"对象：{who or '[未挂关系对象 —— 谁该看这条？]'}"
            f"　{target.elapsed}",
            "",
            "## 事实（下面这些是从库里读出来的，可核对）",
        ]
        waiting = task.waiting_on
        if waiting is not None:
            since = f"{waiting.since:%Y-%m-%d}"
            lines.append(f"- 在等：{waiting.who_or_what}（从 {since} 起）")
            if waiting.follow_up_at is not None:
                lines.append(f"- 原定跟进：{waiting.follow_up_at:%Y-%m-%d %H:%M}")
        if target.promise is not None:
            lines.append(f"- 我答应过：{target.promise.statement}")
            if target.promise.promised_for is not None:
                lines.append(f"- 承诺期限：{target.promise.promised_for:%Y-%m-%d %H:%M}")
        if task.intent:
            lines.append(f"- 这件事的目标：{task.intent}")
        lines.append("- 上次跟进的结论：[TODO 库里没有 —— 你自己补，或删掉这行]")
        lines.append("")
        lines += [
            "## 正文（下面这段要你自己敲，系统一个字都不替你决定）",
            "",
            NUDGE_LEADIN,
            "",
            _NUDGE_OPENING[target.direction],
            "",
            "[TODO 什么时候给回复 / 要不要留台阶]",
            "",
            "[TODO 落款]",
            "",
            "---",
            f"（草稿由系统铺事实、正文留白。**系统不会发这条** —— 复制出去，"
            f"发不发你定。催过了记得 /followup {task.id[:8]} <时间> 或"
            f" /promise keep <序号> 把账结掉）",
        ]
        return "\n".join(lines)

    def draft_for_task(self, task_id: str, now: datetime | None = None) -> str:
        """按事务 id 出草稿。**不在候选里就拒绝**，并说清为什么。

        刻意不做「随便哪条都能起草」：用户敲了 ``/nudge`` 却指了一条
        没到期的，那多半是记错了 id。指出来比默默起草一条用不上的
        消息更省事。
        """
        task = self._tasks.get(task_id)
        for target in self.targets(now):
            if target.task.id == task_id:
                return self.draft(target)
        waiting = task.waiting_on
        promise = self._promises.list_for_task(task_id)
        if waiting is None and not promise:
            raise ValidationError(
                f"「{task.title}」既没有在等什么、也没有对外承诺 —— "
                "没有什么可催的（先 /wait 或 /promise 记一条）"
            )
        raise ValidationError(
            f"「{task.title}」现在还没到催的时候"
            "（要等 follow_up_at 过了、或承诺过了期限才会进候选）"
        )

    # -- 内部 --------------------------------------------------------------- #

    def _contact_name(self, contact_id: str | None) -> str | None:
        if not contact_id:
            return None
        contact = self._contacts.get(contact_id)
        return contact.name if contact is not None else None


def _due_at(target: NudgeTarget) -> datetime | None:
    """这条催办所依据的那个时刻（用来排序）。"""
    if target.direction == "wait":
        waiting = target.task.waiting_on
        return waiting.follow_up_at if waiting is not None else None
    return target.promise.promised_for if target.promise is not None else None


def _elapsed(now: datetime, since: datetime, prefix: str) -> str:
    """「已过 3 天」/「已过 5 小时」/「今天刚过」。

    只描述**多久没动**，不写「你违约了」—— 判违约是用户的事（11.19.5）。
    """
    delta = now - since
    if delta.days >= 1:
        return f"{prefix} {delta.days} 天"
    if delta.seconds >= 3600:
        return f"{prefix} {delta.seconds // 3600} 小时"
    return f"{prefix}不到一天"