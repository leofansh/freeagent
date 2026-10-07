"""能力类问答：**由 LLM 路由，规则只负责渲染事实**。

## 为什么不能用关键词（一次真实的翻车）

这个模块第一版是关键词表（``_PROJECT_Q`` / ``_CAPABILITY_Q`` /
``_ACTION_VERBS``）。它在真实链路上立刻自己绊倒了：

    「有哪些执行器?」  →  ``_looks_like_action`` 为真（命中「执行」）
                      →  能力通道闭嘴
                      →  落进硬编码的 today 兜底
                      →  **正是这次要修的那个症状**

**规则只在写表的人和用户说同一句话时成立。** 只要用户换个说法
（「能动哪些工程」「opencode 在哪」），表就漏了。而漏了没有报错 ——
它只是悄悄退回「今天」，用户看到的是一个**自信的错误答案**。

用户当场给出的判断是对的：**基于规则的交流大部分是失败的**，
规则只配得上菜单和命令。所以分工反过来：

- **LLM 决定「这是在问哪一类能力」** —— 语义理解是模型的强项；
- **规则只负责把选定的事实渲染成句子** ——「答案必须等于数据」这条
  地基不动摇，动摇的只是「怎么听懂这句话」。

这与 ``LLMProvider.select_view`` 是**同一个模式**，只是选项集不同：
那边选「读哪张事务表」，这里选「问的是哪一类能力」。契约也一样：
**返回封闭集里的名字，或 ``None``**；``None`` 是一等公民，不是失败。

## 没有 LLM 时怎么办

规则层答不了就说**不知道**并指路到菜单。它**不猜** —— 猜出来的能力
清单会让人以为助手真的能改那个项目，而它不能，那比「我不知道」
危险得多。
"""

from __future__ import annotations

from .chat import ChatReply, ChatReplyKind

__all__ = [
    "CAPABILITY_TOPICS",
    "CapabilityRouter",
    "CapabilityView",
]

#: 封闭话题集。``(名字, 说明)`` —— 说明是给**模型**读的。
#:
#: 刻意比第一版细：``projects`` / ``executors`` / ``can_do`` / ``greeting``
#: 分开。封闭集是**给模型选**的，合并成一个大类只会让「能动哪些工程」
#: 和「有哪些执行器」落进同一格。
CAPABILITY_TOPICS: tuple[tuple[str, str], ...] = (
    (
        "projects",
        "**在问**能用/能改哪些项目、仓库、工程、代码库；问项目列表、哪些项目"
        "已授权、某个项目名认不认识。"
        "如果这句话是**要求我动手改代码**（帮我改、在某项目里加、把某函数"
        "改成…），不要选这里 —— 那是委派请求，不是能力提问。",
    ),
    (
        "executors",
        "**在问**接了哪些执行器 / worker / agent / 外部工具；问 opencode "
        "在不在、谁替我干活。"
        "如果这句话是**要求执行器去干活**（让 opencode 改、在某项目里修），"
        "不要选这里 —— 那是委派请求。",
    ),
    (
        "can_do",
        "**在问**这个助手能做什么、会什么、能帮我干什么、怎么用、有什么"
        "功能、有哪些能力。"
        "如果这句话是**要我去做什么**（帮我记、提醒我、下周要…），不要选"
        "这里 —— 那是记事请求。",
    ),
    (
        "greeting",
        "打招呼、确认助手在不在、寒暄、「在吗」「你好」「忙吗」这类"
        "不带信息量的开场白",
    ),
)

_TOPIC_NAMES = frozenset(name for name, _ in CAPABILITY_TOPICS)

#: 没有 LLM 时的指路。**只说去哪儿，不编答案。**
_MENU_HINT = (
    "（这句话我得问模型才懂，而模型现在没连上 —— 所以不猜。"
    "命令清单在 /help，网页那边有按钮）"
)


class CapabilityView:
    """助手**自己**的事实：有哪些项目 / 接了什么执行器。

    ## 为什么是数据而不是函数

    回答这些问题要的两样东西 —— 已授权项目、执行器 —— 都不在
    ``ChatService`` 手里。所以由组装层
    （:func:`freeagent.app.build_app`）注入，而不是让 ``ChatService``
    去 ``import config``。

    这里**只有事实，没有判断** —— 判断在 :class:`CapabilityRouter`。
    同一份事实能配任何路由器，且事实只来自一处。
    """

    def __init__(
        self,
        *,
        projects: tuple[str, ...] = (),
        project_names: tuple[str, ...] = (),
        executors: tuple[str, ...] = (),
    ) -> None:
        self.projects = projects
        self.project_names = project_names
        self.executors = executors

    @property
    def enabled(self) -> bool:
        """委派可用吗？（白名单非空即真）"""
        return bool(self.projects)

    def labels(self) -> tuple[str, ...]:
        """给人看的名字。取不到显示名时退回路径 —— **不静默变空**。

        空列表会让「有哪些项目」回答成「没有授权任何项目」，那是在撒谎。
        """
        return self.project_names or self.projects


class CapabilityRouter:
    """把一句话路由到能力话题。**LLM 唯一裁决，规则不参与判断。**

    ## 刻意没有关键词兜底

    因为它制造「自信的错误答案」：没命中时不说话，上游就退回「今天」，
    而用户看到的是一份**无关的清单** —— 那看起来像是回答过了。

    所以 :meth:`try_route` 没命中就返回 ``None``（闭嘴），
    :meth:`fallback` 明说不知道。两者都不是猜。
    """

    def __init__(self, llm=None, *, enabled: bool = True) -> None:
        self._llm = llm
        #: **独立于 ``view_routing`` 的开关。**
        #:
        #: 为什么不复用 ``config.llm_view_routing``：那个开关量的是
        #: 「事务**视图**路由要不要问模型」，而它 2026-09-29 的结论是
        #: 13/15 vs 11/15（输在模型不认「不匹配」）。能力路由是**另一次**
        #: 独立的测量：实测 11/11 全对。把两件事塞进一个开关，等于让
        #: 一次测量替另一次做决定 —— 那正是「量出来的结论就照着量」
        #: 这条规矩**只对被量的那件事成立**。
        self._enabled = enabled

    @property
    def llm_available(self) -> bool:
        if not self._enabled or self._llm is None:
            return False
        # ``can_select_view`` 是规则层实现上的属性；缺这个方法的
        # provider（如测试替身）视为可用，由调用方自己保证。
        return bool(getattr(self._llm, "can_select_view", True))

    def try_route(self, text: str, view: CapabilityView) -> ChatReply | None:
        """这句话在问能力吗？是就答，不是就闭嘴（返回 ``None``）。"""
        stripped = text.strip()
        if not stripped or view is None or not self.llm_available:
            return None
        picked = self._ask_llm(stripped)
        if picked is not None:
            return self._render(picked, view)
        return self._fallback_route(stripped, view)

    def _fallback_route(self, text: str, view: CapabilityView) -> ChatReply | None:
        """能力话题没命中时的兜底 —— **不是猜，是换一个问题问**。

        「无法穷举」的正确应对：不再往能力话题里加格子（那是无限赛跑），
        而是把问题换成**答案空间小得多**的那个：这句该做什么动作。

        返回 ``None`` 仍然意味着闭嘴 —— 动作也说不出，就真的不归我管。
        """
        # **必须点名我认识的东西**，否则一律闭嘴。
        #
        # 实测踩到的：兜底上线后「我有哪些角色脉络?」被 ``describe`` 接走，
        # 答成「授权给我改代码的项目：OpenMOS、XiaoYuan」—— 而用户问的是
        # **他自己的脉络**。那是事务层的活（``_answer_roles`` 已经会答），
        # 被抢走后用户拿到一份项目清单。
        #
        # 所以这里加一道**按名字**的闸：句子里出现授权项目名或执行器名，
        # 才轮到能力通道答。理由不是「关键词穷举」（那正是失败的做法），
        # 而是**同名判定**：「XiaoYuan」这个名字要么在授权清单里，要么不在，
        # 这件事是确定的，不依赖用户怎么措辞。
        #
        # 「小袁能改吗」能接住，是因为「小袁」和「XiaoYuan」指向同一个项目 ——
        # 这条由模型判断，闸门只查「有没有一个我认识的名字」。
        if not self._mentions_known_thing(text, view):
            return None

        action = self._ask_action(text)
        if action in (None, "none"):
            return None
        if action == "howto":
            return self._how_to(view)
        if action == "authorize":
            return self._authorize_hint(text, view)
        return self._describe(text, view)

    def _mentions_known_thing(self, text: str, view: CapabilityView) -> bool:
        """句子里有没有我认识的项目 / 执行器 / 脉络名？

        项目与执行器查授权清单。**脉络名也查** —— 因为「工作项目A 里有什么」
        是事务层的事，落到能力通道就是又一次抢活。

        「小袁」这种简称认不出来，那就不接管 —— 宁可漏答，
        不给一份无关的清单。实测漏答的代价是「没听懂 + 兜底自报」，
        而误捕的代价是**一个自信的错误答案**。
        """
        low = text.casefold()
        known = [*view.labels(), *(view.executors or ())]
        return any(k.casefold() in low for k in known if k)

    #: 兜底用的**闭合动作集**。这是「模型答不出能力话题时」的出口。
    #:
    #: 刻意**不再往能力话题里加格子** —— 加第 5、第 6、第 7 格永远追不上
    #: 用户的说法（实测：「OpenMOS是什么」「怎么改OpenMOS」「我要改X」
    #: 三个问法，三次都要新加一格）。所以兜底换成**另一个问题**：
    #: 不问「这是哪一类能力」，而问「**这句话该做什么动作**」。
    #:
    #: 这仍然是封闭集、仍然由模型选、``None`` 仍是一等公民。变的只是
    #: **问的问题**：从「归哪类」变成「做什么」。动作集比话题集小得多，
    #: 因为**动作是有限的**（看项目 / 讲流程 / 记账 / 不动手），
    #: 而说法是无限的。
    FALLBACK_ACTIONS: tuple[tuple[str, str], ...] = (
        ("describe", "用户在**问某个东西是什么**（项目/脉络/事务/角色）——"
                     "要的是**说明**，不是清单。"),
        ("howto", "用户在**问怎么做**（怎么改、怎么用、怎么发起）"
                  "—— 要的是**步骤**。"),
        ("authorize", "用户要**动手**（改代码、委派、动手做）"
                      "—— 要建委派事务。"),
        ("none", "都不是以上：用户是在**记事**或**闲聊**，"
                 "不该由能力通道回答。"),
    )

    def _ask_llm(self, text: str) -> str | None:
        """问模型「这是问能力，还是有别的含义」。

        **任何异常都当 ``None``**：模型答不出不是故障。
        """
        try:
            picked = self._llm.select_view(text, CAPABILITY_TOPICS)
            # 类型**先**判，再做成员检查。模型返回 list 会抛
            # ``TypeError: unhashable``（踩过一次）。
            if not isinstance(picked, str):
                return None
        except Exception:
            return None
        return picked if picked in _TOPIC_NAMES else None

    def wants_code_change(self, text: str, view: CapabilityView | None = None) -> bool:
        """这句话是在要求**改代码**吗？

        给 :mod:`freeagent.web.endpoints_read` 用：那边要判断「这句话能不能
        只读回答」，而建委派要写库、必须转 :class:`~freeagent.cli.app.Repl`。

        ## 问的是 ``parse_delegation``，不是 ``_ask_action``

        原先问 ``_ask_action(...) == "authorize"``，而那个动作集里还有
        ``howto`` —— 于是「怎么改OpenMOS?」（**问做法**）与
        「帮我改OpenMOS」（**要动手**）分在了两处判定上，而它们其实
        共用同一个回答（怎么改 = 三步流程）。

        ``parse_delegation`` 直接问「是不是在要求改代码」，是这件事
        本来的问法；``_ask_action`` 留给**兜底回答**用（见
        :meth:`_fallback_route`）。

        **任何异常都当否** —— 判错方向是把一句闲聊送去建委派。
        """
        if view is not None and not view.enabled:
            return False
        # **不要读 ``self._llm.llm_enabled``** —— 那是 :class:`Config` 上的
        # 字段，``LLMProvider`` 协议里没有（provider 只有
        # ``parse_delegation`` / ``select_view``）。上一版这么写，于是每条
        # 消息都 AttributeError，而它发生在 ``try`` **之外**，于是直接
        # 冒到 Web 层变成 **HTTP 500**，整条对话不可用。
        #
        # 全量 2300 条测试没抓到，是因为断言用的 provider 恰好带了那个属性，
        # 或者压根没走到这行 —— 症状（只有真 provider 才犯）与覆盖面正好错开。
        #
        # 「智能层关掉了就别问它」这个意图由**规则层自己**满足：
        # :meth:`RuleBasedProvider.parse_delegation` 如实返回
        # ``is_delegation=False``（它明确拒绝用规则猜项目，见那里的理由），
        # 所以离线或降级时这里自然返回 False —— 不需要额外判一次。
        if self._llm is None:
            return False
        try:
            return bool(self._llm.parse_delegation(text, view.labels() if view else ()))
        except Exception:
            return False

    def _ask_action(self, text: str) -> str | None:
        """兜底问法：这句话**该做什么动作**。

        这是「没法穷举」的正确应对 —— 不是继续加能力话题，而是把
        问题换成一个**答案空间小得多**的问题。
        """
        try:
            picked = self._llm.select_view(text, self.FALLBACK_ACTIONS)
            if not isinstance(picked, str):
                return None
        except Exception:
            return None
        return picked if picked in ("describe", "howto", "authorize", "none") else None

    # -- 渲染：这里只有「事实 → 句子」 ──────────────────────────────────── #
    def _render(self, topic: str, view: CapabilityView) -> ChatReply:
        return {
            "greeting": self._greeting,
            "executors": self._executors,
            "projects": self._projects,
            "can_do": self._can_do,
        }.get(topic, self._can_do)(view)

    def _greeting(self, view: CapabilityView) -> ChatReply:
        lines = ["我在。"]
        if view.enabled:
            names = view.labels()
            lines.append(f"现在能改这 {len(names)} 个：{'、'.join(names)}")
        else:
            lines.append("（还没授权任何项目，所以改代码那条路暂时不可用）")
        lines.append("说一件事我就记下来；问「今天」我给你清单。")
        return ChatReply(ChatReplyKind.ANSWER, "\n".join(lines))

    def _projects(self, view: CapabilityView) -> ChatReply:
        if not view.projects:
            return ChatReply(
                ChatReplyKind.ANSWER,
                "现在没有授权任何项目，所以我还不能改代码。\n"
                "在网页的「设置」里授权一个项目就能开这条。",
                suggestions=("你能做什么",),
            )
        labels = view.labels()
        lines = [f"能改这 {len(labels)} 个："]
        lines.extend(f"  {n}" for n in labels)
        return ChatReply(
            ChatReplyKind.ANSWER,
            "\n".join(lines),
            suggestions=("你能做什么",),
        )

    def _can_do(self, view: CapabilityView) -> ChatReply:
        lines = [
            "我能做三件事：",
            "  记事 —— 直接说一句就行（「下周二交周报」）",
            "  查 —— 问我「今天」「在等什么」「提醒」「某条为什么排在这」",
            "  改代码 —— 交给 opencode 做，它每动一次都问你批不批",
        ]
        if view.enabled:
            lines.append(f"    现在授权的是：{'、'.join(view.labels())}")
        else:
            lines.append("    （还没授权项目，暂时不能改代码）")
        lines.append("改代码的说法：「让 opencode 改 <项目> <要做的事>」。")
        return ChatReply(ChatReplyKind.ANSWER, "\n".join(lines))

    def _how_to(self, view: CapabilityView) -> ChatReply:
        """「怎么改 X」—— 要**步骤**，不是清单。

        实测：原先落到「有哪些项目」—— 答案错的，但看起来像对的。
        用户问步骤拿到一份清单，这比拒答更坏。
        """
        if not view.enabled:
            return ChatReply(
                ChatReplyKind.ANSWER,
                "改代码这条路还没开 —— 现在没有授权任何项目。",
            )
        return ChatReply(
            ChatReplyKind.ANSWER,
            "三步：\n"
            "  1. 说「让 opencode 改 <项目> <要做的事>」（一整行，别换行）\n"
            "  2. 我复述一遍，你确认\n"
            "  3. 执行器动手前，每一步都发卡问你批不批"
            "\n现在授权的是：" + "、".join(view.labels()),
        )

    def _authorize_hint(self, text: str, view: CapabilityView) -> ChatReply:
        """「我要改 X」—— **真要动手**。

        刻意**不直接建事务**：用户说的是「我要改 XiaoYuan」，而没说要改
        什么。猜一个需求写进简报、发出去给执行器改代码，比不做更坏。

        所以先回一句「我要改什么」，把球打回去 —— 这一步只花一次交互，
        而它换掉的是「在错误的简报上动手」。
        """
        if not view.enabled:
            return ChatReply(
                ChatReplyKind.ANSWER,
                "改代码这条路还没开 —— 现在没有授权任何项目。",
            )
        return ChatReply(
            ChatReplyKind.ANSWER,
            f"好，用 {view.executors[0] if view.executors else '执行器'} 改。\n"
            f"授权里的项目：{'、'.join(view.labels())}\n"
            "你要改的是哪个项目、要改成什么样？（一整行说一句就行）",
            suggestions=view.labels(),
        )

    def _describe(self, text: str, view: CapabilityView) -> ChatReply:
        """「X 是什么」—— 要**说明**。

        ## 为什么不能拿清单顶替

        实测（用户截图）：「OpenMOS 是什么?」原先落到今天兜底，
        而「OpenMOS 是什么项目」却被当成清单回答 —— 差一个「项目」二字，
        两种结果。因为当时能力表只有「有哪些项目」一格，装不下两种问法。

        这里**如实说清边界**：我知道有这些项目、也知道自己没读过它们的内容。
        说不出就是说不出 —— 拿一份清单冒充详情，是**看着像答案的错答案**。
        """
        return ChatReply(
            ChatReplyKind.ANSWER,
            "我能说的只有这些（这是授权给我改代码的项目，我**没读过**"
            "它们的内容）：\n"
            + "\n".join(f"  {l}" for l in view.labels())
            + "\n想看某个项目具体是什么，直接让我读：「让 opencode 看看 "
            "<项目名> 是干嘛的」。",
            suggestions=("你能做什么", "今天该做什么"),
        )

    def _executors(self, view: CapabilityView) -> ChatReply:
        if not view.executors:
            return ChatReply(
                ChatReplyKind.ANSWER,
                "现在没接任何执行器，所以我不能自己写代码。",
            )
        return ChatReply(
            ChatReplyKind.ANSWER,
            f"接的执行器：{'、'.join(view.executors)}。\n"
            "它动手时会一条一条问你批不批。",
        )

    def fallback(self) -> ChatReply:
        """没有 LLM 时的**诚实**回答：不知道 + 指路，不猜。"""
        return ChatReply(ChatReplyKind.ANSWER, _MENU_HINT)