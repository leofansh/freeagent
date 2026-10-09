"""对话入口：把一句白话变成「一次对你数据的回答」或「一次记录」。

为什么有这个模块
----------------
上一轮修 bug 时撞上两件冲突的事：

1. 问一句「周报进展怎么样」会被当成一件要记的事，造出垃圾角色；
2. 但如果一律拒答，「用了才知道好不好用」就落空了 —— 用户要能**问**，
   才能判断这工具值不值得用。

所以这里的职责很窄，也刻意窄：

* **只读地回答**关于你自己数据的问题（今天做什么 / 进展如何 / 为什么排第一）
* **只新建**事务
* **不改动**任何已有事务

最后一条是刻意的。上一轮刚花力气堵住「一句话改错数据」，
这轮如果顺手加一堆自然语言改操作，等于把刚修的坑重新挖开。
所以要改已有事务时，这里会如实说「做不到，请用 X」，而不是猜一个动作去改。

路由是**纯规则**的：不调模型也能用，且完全确定、可测。
配了 Key 也不走模型路由 —— 会改数据的入口，路由层必须可预测。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum

from ..domain import ArtifactStatus, ScoredTask, Task, TaskKind, TaskState
from typing import TYPE_CHECKING

from ..services.clock import Clock

if TYPE_CHECKING:  # 环：capability 要 ChatReply，这里只出现在标注里
    from .capability import CapabilityRouter, CapabilityView
from ..services.llm.provider import (
    InputIntent,
    RoleHint,
    is_confidently_recordable,
    query_shaped,
    read_intent,
)
from ..services.roles import RoleService
from ..services.restore import RestoreService
from ..services.sorting import score_and_sort
from ..services.tasks import TaskService
from ..storage.repos import TaskRepo

__all__ = [
    "ChatReplyKind",
    "ChatItem",
    "ChatReply",
    "ChatService",
    "CAN_DO",
]


class ChatReplyKind(str, Enum):
    """回复类型。界面靠它决定怎么渲染。"""

    ANSWER = "answer"      # 回答了一个只读问题
    RECORDED = "recorded"  # 记下了一件事
    CLARIFY = "clarify"    # 需要用户补一个选择
    CANNOT = "cannot"      # 做不到（如实说，不猜一个动作糊过去）
    HELP = "help"          # 在问「你能做什么」


@dataclass(frozen=True, slots=True)
class ChatItem:
    """回复里带的一条事务。结构化数据交给界面渲染，不塞进 text 里。"""

    task_id: str
    short_id: str
    title: str
    roles: tuple[str, ...]
    kind_label: str
    state_label: str
    total_weight: int = 0
    reasons: tuple[str, ...] = ()
    detail: str = ""
    scheduled_for: str | None = None


@dataclass(frozen=True, slots=True)
class ChatReply:
    kind: ChatReplyKind
    text: str
    items: tuple[ChatItem, ...] = ()
    #: 可点的后续问题。让用户不用每次都打字。
    suggestions: tuple[str, ...] = ()
    task_id: str | None = None
    #: 命中了哪条路由（``"today"`` / ``"waiting"`` / ``"role"`` …）。
    #:
    #: **纯观测字段，不参与任何判断。** 加它是因为原先没法回答一个
    #: 基础问题：「它到底选了哪个视图？」—— 只能从 text 里反推，而
    #: 兵底兵到「今天」时输出和真问「今天」**一模一样**，于是「路由错了」
    #: 这件事在测试里根本不可见。一个把所有问题都路由到今天的实现，
    #: 能通过当时全部测试。
    route: str = ""
    #: True = 没匹配上任何路由，这是**兵底猜的**。
    #:
    #: 它和 ``route`` 是一对：光看 ``route`` 分不清「用户真问今天」和
    #: 「问了个别的、我猜成今天」。这两种必须能被区分，否则用户会把
    #: 猜测当成答案 —— 那比明说「我不知道」危险得多。
    inferred: bool = False


#: 开头就告诉用户能问什么 —— 猜谜语式的助手没人会用第二次。
CAN_DO: tuple[str, ...] = (
    "今天该做什么",
    "我在等什么",
    "有什么提醒",
    "我上周完成了什么",
    "家庭那边有什么",
    "周报进展怎么样",
    "为什么这条排第一",
)

_TODAY_WORDS = ("今天", "现在", "先做", "先干", "优先", "安排", "做什么", "干什么")

#: 问「**有哪些**脉络 / 角色」的词。
#:
#: 这一组**不是**为了「某条脉络名下的事」——那个由 ``_match_role``
#: （真名匹配）负责，不需要词表。这里管的是**列举**：句子里一个具体
#: 脉络名都没有，只在问「一共有哪几条」。
#:
#: 踩过的坑：原先 ``role`` 这条路上是 ``if role is None: return None``，
#: 而「没点名」被当成了「没有这条信息」—— 于是问脉络拿到今天视图。
#: **漏了还不报错**，只是悄悄退回兜底，用户看到自信的错误答案。
#:
#: 刻意只用「脉络 / 角色」这两个词，不用「有哪些」这种壳子：「有哪些」
#: 太泛，会把「有哪些提醒」也拽进来（那是 reminders）。
_ROLE_LIST_WORDS = (
    "脉络", "角色",
    "有哪些角色", "哪些角色", "什么角色",
    "角色列表", "脉络列表",
)
_WAITING_WORDS = ("等谁", "在等", "等什么", "卡住", "等候")
_REMINDER_WORDS = ("提醒", "会不会忘", "别忘")
_DONE_WORDS = ("完成", "做完", "搞定", "结束", "交付了")
_WHY_WORDS = ("为什么", "凭什么", "排第", "排最前", "原因")
_PROGRESS_WORDS = ("进展", "进度", "到哪", "怎么样了", "什么情况", "咋样", "怎么样")
_ALL_WORDS = ("全部", "所有", "清单", "一览", "有什么事务", "未结束")

#: 只读优先路由的长度上限。
#:
#: 「你能做什么」的结构。**不是词表** —— 词表被下一种说法击穿
#: （实测：词表里有「能做什么」，真人说「可以做什么」，差一个字就 miss）。
#:
#: 两个形态，都要覆盖：
#: 1. ``能|可以|会|可`` + 可选人称 + 可选动词 + 疑问词
#:    —— 人称与动词**各可缺席**：「能帮我做啥」里两个都在，
#:    而「能做什么」里两个都不在。所以是两个独立的可选组，不是一个。
#: 2. ``有什么|有哪些`` + 能力名词 —— 「有什么功能」不含「能」字。
#:
#: 末尾的疑问词含 ``嘛``，覆盖「能做嘛？」这类口语。
#:
#: ⚠️ 能力名词**只列这四个**：加「提醒」会把「有什么提醒」误判成求助，
#: 而那是个真问题（答案该是数据）。宁可少认求助，也不能把问题当求助。
_HELP_STRUCTURE_RE = re.compile(
    r"(能|可以|会|能够|可)\s*(帮我|帮忙|替我|给我)?\s*(做|干|搞|办)?\s*"
    r"(什么|啥|哪些|哪个|嘛)"
    r"|有什么\s*(功能|用法|能力|命令)"
    r"|有哪些\s*(功能|用法|能力|命令)"
)

#: 「全部未结束的事」7 个字就该当查询；「下周二要交的销售周报初稿」14 个字
#: 是在记事。查询是**短语**，指令是**句子** —— 用长度把它们粗略分开，
#: 比再加一堆词表可靠（词表会被下一种说法击穿，而长度不会）。
#: 「未结束」的**闭集**。
#:
#: 刻意写成闭集而不是取反（``not in (DONE, DROPPED)``）：新增一个状态时
#: 闭集不会被自动算错，而取反会 —— 新状态会悄悄被当成「未结束」。
#:
#: 踩过的坑：这里原先写 ``not t.state.is_terminal``，而 ``TaskState``
#: **没有** ``is_terminal`` 这个属性（真跑才炸：属性名是照记忆写的，
#: 不是查的）。凡是自己编的属性名，先查。
_OPEN_STATES: frozenset[TaskState] = frozenset(
    {TaskState.INBOX, TaskState.ACTIVE, TaskState.BLOCKED}
)

_VIEW_QUERY_MAX_LEN = 12

#: **封闭**视图集：``(名字, 中文说明)``。
#:
#: 说明在这里而不在 LLM 那一侧，因为「这个视图到底显示什么」是领域知识 ——
#: 放在视图旁边才不会和渲染逻辑漂移。LLM 只能从这些名字里**选**，
#: 自创的名字一律丢弃（封闭集就是这条接口的安全边界）。
#:
#: 注意说明是给**模型**读的，必须把易混的区分写清楚 —— 下面 ``closed`` 与
#: ``today`` 的区别就是评测里那条「我这周有什么安排」的直接成因。
_VIEWS: tuple[tuple[str, str], ...] = (
    ("today", "今天要动的事（已把逾期的顺延进来，再按提示信号排序）"),
    ("waiting", "卡住的：正在等别人、等回复、被别的事挡住的事"),
    ("reminders", "设了提醒但还没到点的事"),
    ("why", "某一条为什么排在这个位置、为什么它该先做"),
    ("progress", "某一条现在到哪一步了"),
    ("all", "全部还没结束的事，不限日期"),
    ("closed", "某个**时间段**内已经做完的事（本周/上周/最近N天）"),
    ("role", "某个脉络（角色）名下的事；**也包括**「我有哪些脉络/角色」这类**列举全部**的问题 —— 后者不需要点名任何一条"),
)

_RANGE_TOKEN_RE = re.compile(r"最近\s*(\d+)\s*(天|周|个月)")
_LAST_WEEK_RE = re.compile(r"上上周|上周|上星期|上个星期")
_THIS_WEEK_RE = re.compile(r"这周|本周|这星期")
_TODAY_ONLY_RE = re.compile(r"今天|今日")
_YESTERDAY_RE = re.compile(r"昨天|昨日")

#: 提问时把疑问词剥掉，剩下的才可能是事务名。
_QUESTION_NOISE = (
    "为什么", "凭什么", "排第几", "排最前", "原因", "进展", "进度", "怎么样了",
    "怎么样", "什么情况", "到哪", "咋样", "什么", "哪", "怎么", "如何", "吗",
    "呢", "？", "?", "我", "现在", "一下", "那条", "这条", "它的", "了",
)

#: ``ArtifactStatus`` 是纯字符串枚举，没有 ``.label``（不像 ``TaskState``），
#: 所以中文标签得自己给。之前漏了这条，演示库里一有草稿就 500 ——
#: 而当时的测试数据恰好没有草稿，所以那条分支从没被执行过。
_ARTIFACT_STATUS_LABEL = {
    ArtifactStatus.DRAFT: "草稿",
    ArtifactStatus.ACCEPTED: "已采纳",
    ArtifactStatus.SUPERSEDED: "被取代",
}

#: 指代：「第二个」「第三条」→ 上一轮结果里的第 N 项。
_ORDINAL_RE = re.compile(r"第\s*([一二三四五六七八九十两0-9]+)\s*(?:个|条|项|件)?")
_ORDINAL_DIGITS = {
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}

#: 代词/指示词：单独出现时指「上一轮说的那条」。
#: 刻意**只认在有上下文时**才算指代 —— 没有上文时说「它」是胡说。
_PRONOUNS = ("它", "这条", "那条", "这个", "那个", "此", "该")

#: 只由指代/疑问组成、极短的输入（「第二个」「它呢」）—— 走专门路由。
_TAIL_ONLY_RE = re.compile(
    r"^(?:第\s*[一二三四五六七八九十两0-9]+\s*(?:个|条|项|件)?|"
    r"它|这条|那条|这个|那个|此|该)"
    r"\s*(?:是啥|是什么|呢|呢[?？]?|怎么说|如何|怎么样|的)?$"
)


class ChatService:
    """把白话变成对用户数据的回答。**不改动已有事务。**"""

    def __init__(
        self,
        tasks: TaskService,
        task_repo: TaskRepo,
        roles: RoleService,
        restore: RestoreService,
        today,
        clock: Clock,
        llm,
        *,
        view_routing: bool = False,
        capabilities: CapabilityView | None = None,
        capability_router: CapabilityRouter | None = None,
    ) -> None:
        self._view_routing = view_routing
        self._tasks = tasks
        self._repo = task_repo
        self._roles = roles
        self._restore = restore
        self._today = today
        self._clock = clock
        self._llm = llm

        #: 助手**自己**的能力（有哪些项目 / 接了什么执行器）。
        #:
        #: 由组装层注入，而不是自己 import ``config`` —— 见
        #: :class:`~freeagent.services.capability.CapabilityView` 的说明。
        #: 默认 ``None`` 时能力通道一律闭嘴，所以**每个既有测试都照旧
        #: 通过**：没注入就不该有这类回答。
        self._capabilities = capabilities

        #: 能力问题的路由器（LLM 裁决）。没注入时 :mod:`capability`
        #: 一律闭嘴 —— 既有的每个测试因此照旧通过。
        self._capability_router = capability_router

    # -- 入口 ---------------------------------------------------------------- #
    def respond(self, text: str, *, context_ids: Sequence[str] = ()) -> ChatReply:
        """回一句话。

        ``context_ids`` 是**上一轮**答复里的事务 id（按答复顺序）。
        由调用方回传，服务本身不存状态 ——
        这样多轮能接上，同时服务端仍然无状态、可测、重启不丢。
        """
        # 能力类问题（「有哪些项目」「你能做什么」「在吗」）**在这里**接，
        # 而不是在 :meth:`Repl._natural`。
        #
        # 踩过的坑（真机复现）：原先接在 ``Repl._natural`` 里，而
        # ``/api/chat`` 走的是 ``app.chat.respond(...)`` —— **压根不经过
        # Repl**。于是飞书那条路修好了、Web 这条原地不动，而 Web 才是
        # 主要入口。
        #
        # 教训：**问「谁调用了我」而不是「我以为谁调用我」。** 三个入口
        # （终端 / 飞书 / Web）里，只有一个走 Repl。
        router = self._capability_router
        if router is not None:
            cap = router.try_route(text, self._capabilities)
            if cap is not None:
                return cap
        stripped = text.strip()
        if not stripped:
            return ChatReply(ChatReplyKind.HELP, "说点什么吧。", suggestions=CAN_DO)
        if self._is_help_request(stripped):
            return ChatReply(ChatReplyKind.HELP, self._help_text(), suggestions=CAN_DO)

        # 指代优先于意图：上一轮给了 7 条，现在问「第二个」不该被当成
        # 一个新任务（`read_intent` 只会说它不是问句）—— 先试着接上下文。
        referred = self._resolve_reference(stripped, context_ids)
        if referred is not None:
            return self._answer_about(stripped, referred)

        # **只读视图先试**，然后才谈写。
        #
        # 顺序是这里的核心，不是风格问题。理由：``read_intent`` 判不出
        # 「全部未结束的事」这类句子 —— 它没有疑问词、没有问号，落到
        # RECORD。而 RECORD 是**默认分支**，于是它真的建出了一条垃圾事务。
        # （见 tests/test_routing_eval.py::test_noun_phrase_query_is_not_recorded）
        #
        # 两条能力按**后果不对称**排序：只读视图答错只是读错一次，
        # 写错是往用户的组织结构里塞脏数据。所以先把便宜的、不后果的
        # 那条路走掉，剩下的才允许落到写。
        view = self.try_view(stripped)
        if view is not None:
            return view

        intent = read_intent(stripped)
        if intent is InputIntent.MUTATE:
            return self._cannot_mutate(stripped)

        if intent is InputIntent.QUESTION:
            return self._answer(stripped)

        # **到这儿是 RECORD，但 RECORD 需要一个正向信号才成立。**
        #
        # 踩过的坑（真实对话实测，2026-09-29）：`你好` 被记成了事务，
        # 真库里因此有**两条**「你好」（0277e93d / 0fbbdb13）—— 不是偶发。
        # 根因是 ``read_intent`` 的最后一行 ``return RECORD``：
        # **没被认出来 = 当成要记的事**。
        #
        # 修法不是「把 RECORD 从兜底里拿掉」（那会误伤真任务），而是
        # **要求一个正向信号**（见 :func:`is_confidently_recordable`）。
        # 代价不对称：多问一句 vs 往角色结构里塞脏数据（角色还会反过来驱动
        # 分类，污染自我强化）。所以失败方向必须是「问」。
        if not is_confidently_recordable(stripped):
            return self._clarify(stripped)
        return self._record(stripped)

    def _clarify(self, text: str) -> ChatReply:
        """没把握时不猜，给选项。**这是 A+B 方案的 B**（文字版）。

        为什么不给「今天」视图：那是**只读**兜底，而这里的输入既不像查询
        也不像指令。答一个不相干的视图，等于**用答非所问掩盖我没听懂**。
        设计文档 12.1.1 原话：「未标注的兜底会把『我不知道』伪装成答案」。

        选项里既有读类也有写类 —— 因为此刻**我分不清**他要读还是要记，
        那就两个都给，让他点/回一句即可。**分不清就别替他决定。**
        """
        options = ("记一件事", "今天该做什么", "全部未结束的事", "都不是")
        return ChatReply(
            ChatReplyKind.CLARIFY,
            f"「{text}」我没把握 —— 你是要记一件事，还是想看点什么？\n"
            f"回一句就行，比如：{options[0]}。\n"
            f"不确定的话直接把那件事说完整些，我记下来。",
            suggestions=options,
        )

    def try_view(self, text: str) -> ChatReply | None:
        """这句是不是在**要一个视图**？是就答，不是返回 ``None``。

        **公开接口，且必须由每个入口都问一遍。** 这不是内部实现细节，
        而是一条防线：入口若自己先调 :func:`read_intent` 再决定要不要
        问它，防线就被绕过了 —— 那正是实测出来的那个 bug。
        「全部未结束的事」没有疑问词，``read_intent`` 判成 RECORD，
        而 Repl 自己判完就直接建事务，从不调用 :meth:`respond`。
        于是在 :meth:`respond` 里加的只读优先，对终端和飞书**完全无效**。

        只接受**查询形状**的短句：没有动作动词、不是改动既有事务。
        这两条守卫缺一不可：

        - 含动作动词 → 那是**指令**。「把今天的会议纪要整理一下」里有
          「今天」，可它是叫你干活，不是问今天干什么。这类句子绝不能被
          视图抢走，否则真任务会被当成查询丢掉 —— 那比建错事务更糟，
          因为它**无声无息地没发生**。
        - 像改动 → 如实说做不到。
        """
        if len(text) > _VIEW_QUERY_MAX_LEN:
            return None
        if not query_shaped(text):
            return None
        # 复用同一个路由器。它对没命中的会兜底成「今天」并标
        # ``inferred``，所以这里用**这个标记**区分「命中」与「没命中」——
        # 不必把路由表再抄一份（抄一份就会漂移）。
        reply = self._answer(text)
        return None if reply.inferred else reply

    # -- 指代 ---------------------------------------------------------------- #
    def _resolve_reference(
        self, text: str, context_ids: Sequence[str]
    ) -> Task | None:
        """把「第二个」「它」接到上一轮的具体事务上。

        没有上下文时返回 ``None`` —— 「它」在没有上文时是胡说，
        这时应当走正常路由，而不是硬指一条。
        """
        if not context_ids:
            return None
        by_id = {t.id: t for t in self._repo.list_all()}

        found = _ORDINAL_RE.search(text)
        if found:
            raw = found.group(1)
            index = int(raw) if raw.isdigit() else _ORDINAL_DIGITS.get(raw, 0)
            if 1 <= index <= len(context_ids):
                return by_id.get(context_ids[index - 1])
            return None

        if any(p in text for p in _PRONOUNS) and _TAIL_ONLY_RE.match(text):
            return by_id.get(context_ids[0]) if context_ids else None
        return None

    def _answer_about(self, text: str, task: Task) -> ChatReply:
        """针对**某一条**的追问：是什么 / 进展 / 为什么排。"""
        if any(w in text for w in _WHY_WORDS):
            return self._answer_why_for(task)
        return self._answer_progress_for(task)

    def _answer_progress_for(self, task: Task) -> ChatReply:
        view = self._restore.open_task(task.id)
        bits: list[str] = []
        if view.task.intent:
            bits.append(f"意图：{view.task.intent}")
        bits.append(f"状态：{view.task.state.label}｜形状 {view.task.kind.label}")
        if view.effective_definition_of_done:
            bits.append(f"完成标准：{view.effective_definition_of_done}")
        if view.progress_note:
            bits.append(f"进度：{view.progress_note}")
        if view.waiting_on is not None:
            bits.append(f"在等：{view.waiting_on.who_or_what}")
        if view.current_artifact is not None:
            art = view.current_artifact
            bits.append(
                f"当前稿 v{art.version}（{_ARTIFACT_STATUS_LABEL[art.status]}）："
                f"{art.title}"
            )
        if view.next_actions:
            bits.append("下一步：" + "；".join(view.next_actions))
        return ChatReply(
            ChatReplyKind.ANSWER,
            f"「{view.task.title}」\n" + "\n".join(bits),
            items=(self._detail_item(view.task),),
            suggestions=("今天该做什么", f"/task {view.task.id[:8]}"),
        )

    def _answer_why_for(self, task: Task) -> ChatReply:
        scored = self._scored_for(task)
        item = self._item(scored) if scored is not None else self._detail_item(task)
        body = (
            "\n".join(f"· {r}" for r in item.reasons)
            if item.reasons else "· 没命中任何信号，只是排进了今天"
        )
        return ChatReply(
            ChatReplyKind.ANSWER,
            f"「{task.title}」{item.total_weight} 分，来自：\n{body}\n"
            "（启发式提示，不是评分 —— 你可以反驳它）",
            items=(item,),
            suggestions=("今天该做什么",),
        )

    # -- 路由 ---------------------------------------------------------------- #
    def _answer(self, text: str) -> ChatReply:
        # 先问模型「你要看哪张表」（真模型时）。它只**选名字**，
        # 事实仍旧由下面的规则读库渲染 —— 「答案必须等于数据」这条地基
        # 没有被动摇，动摇的只是「怎么听懂这句话」。
        #
        # 为什么排在关键词表**前面**：关键词表是「命中即认」，于是
        # 「我这周有什么安排」里的「安排」把它拽去 today 且不自报 ——
        # **误命中比没命中更坏**，因为连承认的机会都没有。
        picked = self._llm_route(text)
        if picked is not None:
            rendered = self._dispatch_route(picked, text)
            if rendered is not None:
                return rendered
            # 模型选了一个当前这句满足不了的视图（如「closed」但句子里
            # 没有时间范围）→ 落回关键词表，而不是硬渲染一个空窗口。

        window = self._time_window(text)
        if window is not None:
            return self._tag(self._answer_closed(text, *window), "closed")
        role = self._match_role(text)
        if role is not None:
            return self._tag(self._answer_role(role), "role")
        # 「有哪些脉络」：先点名后列举。顺序反过来会坏 ——
        # 「有哪些角色」里没有真名，_match_role 本来就返回 None，
        # 所以先查名单不影响点名的那条路。
        if any(w in text for w in _ROLE_LIST_WORDS):
            return self._tag(self._answer_roles(text), "role")
        if any(w in text for w in _WAITING_WORDS):
            return self._tag(self._answer_waiting(), "waiting")
        if any(w in text for w in _REMINDER_WORDS):
            return self._tag(self._answer_reminders(), "reminders")
        if any(w in text for w in _WHY_WORDS):
            return self._tag(self._answer_why(text), "why")
        if any(w in text for w in _PROGRESS_WORDS):
            return self._tag(self._answer_progress(text), "progress")
        if any(w in text for w in _TODAY_WORDS):
            return self._tag(self._answer_today(), "today")
        if any(w in text for w in _ALL_WORDS):
            return self._tag(self._answer_all(), "all")
        # 兜底给「今天」：永远有答案，比「我不知道」好用。
        #
        # 但**必须自报家门**（``inferred=True``，见 ChatReply）。原先这里
        # 悄悄返回今天视图，于是问「我这周有什么安排」会拿到今天的清单，
        # 而输出与真问「今天」逐字相同 —— 用户无从分辨自己被猜了。
        # 不报家门等于把「我不知道」伪装成答案，比直接答错更坏。
        return self._tag(self._answer_today(), "today", inferred=True)

    def _llm_route(self, text: str) -> str | None:
        """问模型选视图。**没有真意见就返回 ``None``**（含离线/降级/未启用）。

        三道门，任何一道不过都直接回落到关键词表：

        1. ``view_routing`` 开关（默认关）。实测没赢，理由见
           :attr:`freeagent.config.Config.llm_view_routing`。
        2. ``can_select_view`` 能力位 —— ``RuleBasedProvider`` 恒为 ``False``，
           于是离线时一次网络调用都不会发生，行为与从前逐字一致。
        3. 异常兜底。选路是锦上添花，没有资格掀翻主流程。
        """
        if not self._view_routing:
            return None
        if not getattr(self._llm, "can_select_view", False):
            return None
        try:
            picked = self._llm.select_view(text, _VIEWS)
            # 类型必须**先**判，再做成员检查。
            #
            # 踩过的坑（接缝测试当场抓到的）：``picked in allowed`` 拿 set
            # 做成员检查时，模型若返回 list 会抛 ``TypeError: unhashable``
            # —— 而上面的 try 只包住了调用本身，**没包住这行**，于是整个
            # 问答路径被掀翻。选路是锦上添花，它没有资格弄坏主流程。
            if not isinstance(picked, str):
                return None
        except Exception:      # 选路是锦上添花，绝不能因此掀翻问答
            return None
        allowed = {name for name, _ in _VIEWS}
        return picked if picked in allowed else None

    def _dispatch_route(self, name: str, text: str) -> ChatReply | None:
        """按名字渲染一个视图。**满足不了就返回 ``None``**，不硬来。"""
        if name == "today":
            return self._tag(self._answer_today(), "today")
        if name == "waiting":
            return self._tag(self._answer_waiting(), "waiting")
        if name == "reminders":
            return self._tag(self._answer_reminders(), "reminders")
        if name == "why":
            return self._tag(self._answer_why(text), "why")
        if name == "progress":
            return self._tag(self._answer_progress(text), "progress")
        if name == "all":
            return self._tag(self._answer_all(), "all")
        if name == "closed":
            window = self._time_window(text)
            if window is None:
                return None            # 句子里没有时间范围，别编一个
            return self._tag(self._answer_closed(text, *window), "closed")
        if name == "role":
            role = self._match_role(text)
            if role is None:
                # 没点名，**不等于没有这个信息** —— 脉络列表明明查得到。
                #
                # 原先这里 ``return None``，于是「我有哪些角色脉络?」一路
                # 落到硬编码的 today 兜底：用户问脉络，拿到今天清单。实测
                # （真实投诉截图）。把「没点名」当成「没有这条信息」——
                # 这跟今天我在别处犯的错是同一个。
                return self._answer_roles(text)
            return self._tag(self._answer_role(role), "role")
        return None

    @property
    def capability_router(self) -> CapabilityRouter | None:
        """能力问题的路由器；没注入就是 ``None``（那条路闭嘴）。"""
        return self._capability_router

    @property
    def capabilities(self) -> CapabilityView | None:
        """助手自己有哪些能力（供 :mod:`capability` 通道读取）。"""
        return self._capabilities

    #: 兵底时插在答复最前面的自报家门。
    #:
    #: 刻意说「**按今天理解了你这句**」而不是「没听懂」：这句已经给了
    #: 可能有用的东西，否认它反而是撒谎。关键是让用户知道**这是我的
    #: 猜测**，好让他自己判断该信几分。
    #: 兜底时插在答复最前面的自报家门。
    #:
    #: 列出的候选**必须是真的能问到的**。原来的三选一
    #: （在等什么 / 提醒 / 全部）漏掉了脉络、项目、能力这些
    #: 同样存在的路 —— 于是它把用户往一条不存在的路上引。
    _INFERRED_NOTICE: str = (
        "（没听懂你问的是哪一类，先按「今天」答 —— "
        "不对就问：在等什么 / 有什么提醒 / 全部 / "
        "我有哪些脉络 / 有哪些项目 / 你能做什么）\n"
    )

    @staticmethod
    def _tag(reply: ChatReply, route: str, *, inferred: bool = False) -> ChatReply:
        """给答复盖上「走了哪条路 / 是不是猜的」两个观测戳。

        ``inferred=True`` 时**同时改写 text** —— 光在数据上留个标记没用，
        用户看到的是 text。数据标记是给测试和排障看的，那行提示才是给
        用户看的，两者都要有。
        """
        if inferred and not reply.text.startswith(ChatService._INFERRED_NOTICE):
            reply = replace(reply, text=ChatService._INFERRED_NOTICE + reply.text)
        return replace(reply, route=route, inferred=inferred)

    # -- 记事 ---------------------------------------------------------------- #
    def _record(self, text: str) -> ChatReply:
        from ..cli.parse import parse_date_expr, parse_time_expr

        existing = self._roles.list_roles()
        if not existing:
            from ..domain import DEFAULT_ROLE_NAME

            role = self._roles.create(DEFAULT_ROLE_NAME)
            role_ids = [role.id]
        else:
            result = self._llm.classify(
                text, [RoleHint(r.name, r.note) for r in existing]
            )
            if result.need_clarification:
                names = [g.role_name for g in result.role_guesses]
                options = tuple(names) or tuple(r.name for r in existing)
                question = (
                    f"「{text}」放到「{names[0]}」里吗？" if len(names) == 1
                    else f"「{text}」放到哪条脉络？"
                )
                return ChatReply(
                    ChatReplyKind.CLARIFY, question, suggestions=options
                )
            role = self._match_role(result.role_guesses[0].role_name) if result.role_guesses else None
            if role is None:
                return ChatReply(
                    ChatReplyKind.CANNOT,
                    "没定归属，我先不记 —— 我只从已有脉络里选，不替你新建。",
                    suggestions=tuple(r.name for r in existing),
                )
            role_ids = [role.id]

        today = self._clock.today()
        scheduled = parse_date_expr(text, today)
        reminder = parse_time_expr(text, scheduled) if scheduled is not None else None
        # classify 给的是字符串枚举，create 要 TaskKind —— 这里显式转换，
        # 不让类型不匹配溜到仓储层才炸
        kind = TaskKind(self._llm.classify(text, []).kind)
        task = self._tasks.create(
            self._llm.refine_title(text),
            role_ids,
            kind=kind,
            scheduled_for=scheduled,
            reminder_time=reminder,
        )
        bits = [f"形状 {task.kind.label}", f"角色 {'、'.join(self._role_names(task))}"]
        if scheduled is not None:
            bits.append(f"排到 {scheduled}")
        else:
            # 必须明说。不说的话用户记完一件事、再问「今天该做什么」，
            # 会拿到「今天没有排进来的事务」—— 而这条明明刚记进去。
            # 静默的代价是用户以为没记上，于是再记一遍。
            bits.append("没排期，不在今天视图里")
        if reminder is not None:
            bits.append(f"提醒 {reminder:%H:%M}")
        return ChatReply(
            ChatReplyKind.RECORDED,
            "已记下 —— " + "，".join(bits) + f"。id {task.id[:8]}",
            items=(self._detail_item(task),),
            suggestions=("今天该做什么", f"/draft {task.id[:8]}"),
            task_id=task.id,
        )

    # -- 具体回答 ------------------------------------------------------------ #
    def _answer_today(self) -> ChatReply:
        view = self._today.view()        # 顺延仍在这里触发
        if not view.items:
            # 「没有排进来的」不等于「没有事」。没排期的那些按设计不进
            # 今天视图，但**必须报出来** —— 否则用户刚记的事凭空消失，
            # 而回答还斩钉截铁地说「没有」。
            extra = (
                f"另有 {view.unscheduled_count} 条没排期，它们也不在今天视图里。"
                if view.unscheduled_count
                else ""
            )
            return ChatReply(
                ChatReplyKind.ANSWER,
                f"{view.day} 今天没有排进来的事务。{extra}",
                suggestions=("我上周完成了什么", "有什么提醒"),
            )
        items = tuple(self._item(s) for s in view.items)
        lines = [
            f"{i}. {it.title}（{it.total_weight} 分）"
            + (f" —— {it.reasons[0]}" if it.reasons else " —— 无命中信号")
            for i, it in enumerate(items, 1)
        ]
        return ChatReply(
            ChatReplyKind.ANSWER,
            f"{view.day} 有 {len(items)} 件事，按信号排：\n" + "\n".join(lines)
            + "\n（启发式提示，不是评分）",
            items=items,
            suggestions=("我在等什么", "为什么这条排第一", "有什么提醒"),
        )

    def _answer_waiting(self) -> ChatReply:
        waiting = [t for t in self._repo.list_all() if t.state.value == "blocked"]
        if not waiting:
            return ChatReply(
                ChatReplyKind.ANSWER, "现在没有卡住的事。",
                suggestions=("今天该做什么",),
            )
        items = tuple(self._detail_item(t, self._waiting_text(t)) for t in waiting)
        return ChatReply(
            ChatReplyKind.ANSWER,
            f"{len(items)} 件在等别人/别的东西：\n"
            + "\n".join(f"{i}. {it.title} —— {it.detail}"
                        for i, it in enumerate(items, 1)),
            items=items,
            suggestions=("今天该做什么",),
        )

    def _waiting_text(self, task: Task) -> str:
        if task.waiting_on is None:
            return "在等，但没说清等谁"
        text = f"在等 {task.waiting_on.who_or_what}"
        due = task.waiting_on.follow_up_at
        if due is None:
            return text + "（没设跟进时间，可能忘了催）"
        if due < self._clock.now():
            return text + "（跟进日已过，该催了）"
        return text + f"（跟进 {due:%m-%d}）"

    def _answer_reminders(self) -> ChatReply:
        now = self._clock.now()
        soon = sorted(
            (t for t in self._repo.list_open()
             if t.reminder_time is not None and t.reminder_time >= now),
            key=lambda t: t.reminder_time or now,
        )
        if not soon:
            return ChatReply(
                ChatReplyKind.ANSWER, "接下来没有到点的提醒。",
                suggestions=("今天该做什么",),
            )
        items = tuple(
            self._detail_item(t, f"提醒 {t.reminder_time:%m-%d %H:%M}")
            for t in soon
        )
        return ChatReply(
            ChatReplyKind.ANSWER,
            f"接下来 {len(items)} 条到点提醒：\n"
            + "\n".join(f"{i}. {it.title} —— {it.detail}"
                        for i, it in enumerate(items, 1)),
            items=items,
            suggestions=("今天该做什么",),
        )

    def _answer_closed(
        self, _text: str, start: datetime, end: datetime, label: str
    ) -> ChatReply:
        done = self._repo.list_closed_between(start, end)
        if not done:
            return ChatReply(
                ChatReplyKind.ANSWER, f"{label}没有已结束的事务。",
                suggestions=("今天该做什么",),
            )
        items = tuple(
            self._detail_item(
                t,
                t.state.label
                + (f" {t.completed_at:%m-%d %H:%M}" if t.completed_at else ""),
            )
            for t in done
        )
        return ChatReply(
            ChatReplyKind.ANSWER,
            f"{label}结束 {len(items)} 件：\n"
            + "\n".join(f"{i}. {it.title} —— {it.detail}"
                        for i, it in enumerate(items, 1)),
            items=items,
            suggestions=("今天该做什么", "有什么提醒"),
        )

    def _answer_roles(self, text: str) -> ChatReply:
        """"我有哪些脉络 / 有哪些角色" —— 列出全部，不用点名。

        与 :meth:`_answer_role` 的分工：那个答**某一条**名下的事，这个答
        **有哪些条**。原先只有后者，且它要求先点名 —— 于是列表类问题
        无路可走。
        """
        roles = self._roles.list_roles()
        if not roles:
            return ChatReply(
                ChatReplyKind.ANSWER,
                "你还没有任何脉络 —— 说一件事我就给你建第一条。",
            )
        lines = [f"你有 {len(roles)} 条脉络："]
        # 只数**未结束**的事：脉络的价值就在这儿，而「0 件」也是有用
        # 信息（说明这条脉络该收了）。
        #
        # 用 ``_OPEN_STATES`` 闭集 —— 见它的定义处。
        open_tasks = [t for t in self._repo.list_all() if t.state in _OPEN_STATES]
        for r in roles:
            n = sum(1 for t in open_tasks if r.id in t.role_ids)
            lines.append(f"  {r.name} —— {n} 件未结束")
        return ChatReply(
            ChatReplyKind.ANSWER,
            "\n".join(lines),
            suggestions=("今天该做什么",),
        )

    def _answer_role(self, role) -> ChatReply:
        tasks = [t for t in self._repo.list_by_role(role.id) if t.is_open]
        if not tasks:
            return ChatReply(
                ChatReplyKind.ANSWER, f"「{role.name}」下没有未结束的事务。",
                suggestions=("今天该做什么",),
            )
        items = tuple(
            self._detail_item(t, t.state.label) for t in tasks
        )
        return ChatReply(
            ChatReplyKind.ANSWER,
            f"「{role.name}」下 {len(items)} 件未结束：\n"
            + "\n".join(
                f"{i}. {it.title}（{it.state_label}"
                + (f"｜排 {it.scheduled_for}" if it.scheduled_for else "｜没排期")
                + "）"
                for i, it in enumerate(items, 1)
            ),
            items=items,
            suggestions=("今天该做什么",),
        )

    def _answer_why(self, text: str) -> ChatReply:
        view = self._today.view()
        if not view.items:
            return ChatReply(
                ChatReplyKind.ANSWER, "今天没有排进来的事务，没什么可解释的。",
                suggestions=("今天该做什么",),
            )
        target = self._resolve_task(text)
        scored = None
        if target is not None:
            scored = next((s for s in view.items if s.task.id == target.id), None)
            if scored is None:
                scored = self._scored_for(target)
        if scored is None:
            scored = view.items[0]
        item = self._item(scored)
        body = (
            "\n".join(f"· {r}" for r in item.reasons)
            if item.reasons else "· 没命中任何信号，只是排进了今天"
        )
        return ChatReply(
            ChatReplyKind.ANSWER,
            f"「{scored.task.title}」{item.total_weight} 分，来自：\n{body}\n"
            "（启发式提示，不是评分 —— 你可以反驳它）",
            items=(item,),
            suggestions=("今天该做什么",),
        )

    def _answer_progress(self, text: str) -> ChatReply:
        target = self._resolve_task(text)
        if target is None:
            return self._cannot_identify(text)
        return self._answer_progress_for(target)

    def _answer_all(self) -> ChatReply:
        open_tasks = self._repo.list_open()
        if not open_tasks:
            return ChatReply(
                ChatReplyKind.ANSWER, "没有未结束的事务。",
                suggestions=("今天该做什么",),
            )
        items = tuple(self._detail_item(t) for t in open_tasks)
        return ChatReply(
            ChatReplyKind.ANSWER,
            f"全部 {len(items)} 件未结束：\n"
            + "\n".join(
                f"{i}. {it.title}（{it.state_label}"
                + (f"｜排 {it.scheduled_for}" if it.scheduled_for else "｜没排期")
                + "）"
                for i, it in enumerate(items, 1)
            ),
            items=items,
            suggestions=("今天该做什么", "我在等什么"),
        )

    def _cannot_identify(self, text: str) -> ChatReply:
        view = self._today.view()
        if not view.items:
            return ChatReply(
                ChatReplyKind.ANSWER, "没找到你说的是哪一条。现在也没排进来的事务。",
                suggestions=("今天该做什么",),
            )
        return ChatReply(
            ChatReplyKind.ANSWER,
            "你说的是哪一条？猜错对象比不答更糟，所以我不猜。"
            "今天排着的是：\n"
            + "\n".join(f"· {s.task.title}" for s in view.items),
            items=tuple(self._item(s) for s in view.items),
            suggestions=tuple(s.task.title for s in view.items[:3]),
        )

    # -- 时间范围 ------------------------------------------------------------ #
    def _time_window(self, text: str) -> tuple[datetime, datetime, str] | None:
        """「上周」「最近3天」→ (起, 止, 人话标签)。只对回顾类提问生效。"""
        if not any(w in text for w in _DONE_WORDS):
            return None
        today = self._clock.today()
        now = self._clock.now()
        midnight = datetime.combine(today, datetime.min.time())
        monday = today - timedelta(days=today.weekday())

        if _YESTERDAY_RE.search(text):
            y = today - timedelta(days=1)
            return (datetime.combine(y, datetime.min.time()), midnight, "昨天")
        if _LAST_WEEK_RE.search(text):
            return (datetime.combine(monday - timedelta(days=7), datetime.min.time()),
                    datetime.combine(monday, datetime.min.time()), "上周")
        if _THIS_WEEK_RE.search(text):
            return (datetime.combine(monday, datetime.min.time()),
                    midnight + timedelta(days=1), "本周")
        if _TODAY_ONLY_RE.search(text):
            return (midnight, now + timedelta(seconds=1), "今天")
        found = _RANGE_TOKEN_RE.search(text)
        if found:
            amount, unit = int(found.group(1)), found.group(2)
            days = amount * {"天": 1, "周": 7, "个月": 30}[unit]
            return (now - timedelta(days=days), now, f"最近 {amount} {unit}")
        return None

    # -- 解析与匹配 ---------------------------------------------------------- #
    def _match_role(self, text: str):
        for role in self._roles.list_roles(include_inactive=True):
            if role.name and role.name in text:
                return role
        return None

    def _resolve_task(self, text: str) -> Task | None:
        """从提问里认出「说的是哪一条事务」。

        只接受**足够明确**的匹配；认不准返回 ``None`` 让上层反问。
        答错对象比不答更糟。
        """
        cleaned = text
        for noise in _QUESTION_NOISE:
            cleaned = cleaned.replace(noise, " ")
        needle = cleaned.strip()
        if not needle:
            return None

        tasks = self._repo.list_all()
        for task in tasks:                       # id 前缀
            if len(needle) >= 4 and (
                needle.startswith(task.id[:4]) or task.id.startswith(needle)
            ):
                return task
        containing = [t for t in tasks if needle in t.title]
        if len(containing) == 1:
            return containing[0]
        if containing:                           # 多条同名 → 不猜
            return None
        partial = [t for t in tasks if t.title and t.title in needle]
        return partial[0] if len(partial) == 1 else None

    def _scored_for(self, task: Task) -> ScoredTask | None:
        scored = score_and_sort([task], self._clock.now(), self._clock.today())
        return scored[0] if scored else None

    # -- 杂项 ---------------------------------------------------------------- #
    def _is_help_request(self, text: str) -> bool:
        """这句是在问「**你能做什么**」吗？

        踩过的坑（真实对话实测，2026-09-29）：原来是一张词表
        ``("你能做什么", "能做什么", "会什么", "帮助", "怎么用", "help")``，
        而真人说的是「你**可以**做什么？」—— **差一个字，miss**，
        于是被当成查询、答了「今天没有排进来的事务」。

        结论：**词表是无底洞**。补了「可以」还会有「能干嘛」「有啥功能」
        「能帮我做啥」。所以改成判**结构**：

            (能|可以|会|能够) (做|干|搞|办|帮我)? (什么|啥|哪些|哪个|嘛)

        外加「帮助 / 怎么用 / help」三个不含上述结构的整词 ——
        它们单独出现时确实是求助。

        保留原有的**排除**守卫：含「提醒 / 在等 / 等谁 / 周报 / 进展 / 完成」
        的一律不算求助 —— 「等周报进展如何」是真问题，答案该是数据
        而不是使用说明。
        """
        if any(w in text for w in ("提醒", "在等", "等谁", "周报", "进展", "完成")):
            return False
        lowered = text.lower()
        if _HELP_STRUCTURE_RE.search(lowered):
            return True
        return any(w in lowered for w in ("帮助", "怎么用", "help", "help?"))

    def _help_text(self) -> str:
        # 刻意不用 markdown 记号：气泡是纯文本渲染的，「**」会原样显示出来。
        # 强调靠换行，不靠星号。
        return (
            "我能读你已记的事，也能记新的，但不改已有事务。\n"
            "可以问我：\n"
            + "\n".join(f"· {s}" for s in CAN_DO)
            + "\n\n直接说一件事（「下周二交周报」）我就记下。"
        )

    def _cannot_mutate(self, _text: str) -> ChatReply:
        return ChatReply(
            ChatReplyKind.CANNOT,
            "「改已有的一条」我还做不到，所以没动你的数据。\n"
            "改排期 /today-pin <id> <日期>｜放弃 /drop <id>｜"
            "改形状 /kind <id> <action|wait|reminder>",
            suggestions=("今天该做什么", "你能做什么"),
        )

    # -- 渲染辅助 ------------------------------------------------------------ #
    def _role_names(self, task: Task) -> tuple[str, ...]:
        return tuple(
            r.name for r in self._roles.list_roles(include_inactive=True)
            if r.id in task.role_ids
        )

    def _item(self, scored: ScoredTask) -> ChatItem:
        task = scored.task
        return ChatItem(
            task_id=task.id,
            short_id=task.id[:8],
            title=task.title,
            roles=self._role_names(task),
            kind_label=task.kind.label,
            state_label=task.state.label,
            total_weight=scored.total_weight,
            reasons=tuple(s.reason for s in scored.signals),
            scheduled_for=(
                task.scheduled_for.isoformat() if task.scheduled_for else None
            ),
        )

    def _detail_item(self, task: Task, detail: str = "") -> ChatItem:
        return ChatItem(
            task_id=task.id,
            short_id=task.id[:8],
            title=task.title,
            roles=self._role_names(task),
            kind_label=task.kind.label,
            state_label=task.state.label,
            detail=detail,
            scheduled_for=(
                task.scheduled_for.isoformat() if task.scheduled_for else None
            ),
        )
