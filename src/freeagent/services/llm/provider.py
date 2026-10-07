"""``LLMProvider`` 抽象接缝。

**本模块刻意不 import ``freeagent.domain`` 或 ``freeagent.storage``。**
边界类型（``TaskRef`` / ``RoleHint`` / ``SignalRef``）在这里自带定义，
目的是让智能层可以被整体替换成真实模型客户端而不牵动领域层，
同时避免包之间的循环导入。

V1 只提供 :class:`~freeagent.services.llm.rules.RuleBasedProvider`；
真实 LLM 实现是后续阶段的事，但接缝在此已经就位。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, Sequence, runtime_checkable

__all__ = [
    "RoleHint",
    "RoleGuess",
    "TaskRef",
    "SignalRef",
    "ClassificationResult",
    "DelegationIntent",
    "LLMProvider",
    "KIND_ACTION",
    "KIND_WAIT",
    "KIND_REMINDER",
    "InputIntent",
    "read_intent",
    "query_shaped",
    "is_question_like",
    "NOT_A_TASK_HINT",
    "CANNOT_MUTATE_HINT",
]

KIND_ACTION = "action"
KIND_WAIT = "wait"
KIND_REMINDER = "reminder"


@dataclass(frozen=True, slots=True)
class RoleHint:
    """供角色匹配使用的角色线索。"""

    name: str
    note: str | None = None


@dataclass(frozen=True, slots=True)
class RoleGuess:
    """角色推断结果。``confidence`` 落在 0..1。"""

    role_name: str
    confidence: float


@dataclass(frozen=True, slots=True)
class TaskRef:
    """传给智能层的事务视图（领域对象的窄投影）。"""

    id: str
    title: str
    intent: str | None = None
    kind: str = KIND_ACTION
    definition_of_done: str | None = None


@dataclass(frozen=True, slots=True)
class SignalRef:
    """排序信号的窄投影。"""

    code: str
    weight: int
    reason: str


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    """新事务的分类结果。"""

    kind: str
    role_guesses: tuple[RoleGuess, ...] = ()
    need_clarification: bool = False
    clarifying_question: str | None = None


#: 疑问词。命中即倾向「用户是在问，不是在记事」。
#:
#: 这不是锦上添花 —— 没有它，问一句「周报进展怎么样」会被当成一件要记的事，
#: 追问「这是哪个脉络」之后，**下一句输入就会被当作角色名建出角色**。
#: 角色是整个模型的组织结构，用问话污染它代价很大。
_QUESTION_WORDS = (
    "为什么", "为啥", "怎么", "怎样", "如何", "什么", "啥", "哪", "谁", "多少",
    "是不是", "能不能", "可不可以", "有没有", "该不该", "要不要", "可以吗",
    # 「进展/进度/咋样」也是问-progress 的词。不加的话「周报素材进展」
    # 会被当成一件要记的事 —— 这正是最初那类数据污染。
    "进展", "进度", "咋样", "情况",
)

#: 明确以疑问词**开头** —— 几乎不会误伤。
_QUESTION_PREFIXES = (
    "为什么", "怎么", "怎样", "如何", "什么", "谁", "哪", "多少", "能否",
    "是不是", "有没有", "该不该", "要不要", "什么时",
)

#: 以这些动词**开头**的，是「去做某事」，不是「问什么」。
#:
#: 用来兜住「写清楚需求到底是什么」这类句子 —— 它有疑问词，但整个句子
#: 是在下指令。没有这张表，12 字以内的真任务会被当问句拒绝掉。
_TASK_VERB_PREFIXES = (
    "写", "做", "修", "交", "买", "整理", "打印", "准备", "完成", "联系",
    "确认", "发", "寄", "取", "送", "读", "看", "学", "开发", "设计", "安排",
    "约", "订", "还", "付", "报", "问客户", "催", "起草", "画", "录", "搬",
    "洗", "修好", "搞定", "落实", "跟", "查", "搜", "下载", "安装", "配置",
)

#: 以这些动词开头，是在**要求改动已有事务**。工具目前做不到（只能新建），
#: 所以必须明确拒绝 —— 否则「把周报改到下周三」会变成一条垃圾事务。
_MUTATE_PREFIXES = (
    "改", "换", "挪", "移", "推迟", "提前", "延后", "删", "取消", "撤",
    "重排", "调到", "改成", "换成",
)

#: 会**改状态**的动词。比 _MUTATE_PREFIXES 宽，因为它们常出现在句尾：
#: 「把它标完成」「这条删了」—— 句首那个词是「把它」，不是动词。
_STATE_VERBS = (
    "完成", "做完", "搞定", "放弃", "结束", "删", "取消", "撤销",
    "移到", "挪到", "延后", "延期", "提前", "推迟", "拖到",
    "改", "换", "重排", "改期", "往前挪", "往后推",
)

#: 指代已有对象的开头。后面跟状态动词就是在命令改动，不是在记事。
_REFERENTIAL_HEADS = ("把", "它", "这条", "那条", "这个", "那个", "此", "该")

#: 序数指代（「第一个改个时间」）也算 —— 主体不是动作，是上一轮的某一条。
_ORDINAL_HEAD_RE = re.compile(r"^第\s*[一二三四五六七八九十两0-9]+\s*(?:个|条|项|件)?")


def _looks_like_mutation(text: str) -> bool:
    if text.startswith(_MUTATE_PREFIXES):
        return True
    # 「把 X 标完成」「把它删了」：主体是已有对象 + 状态动词
    if text.startswith(_REFERENTIAL_HEADS) and any(v in text for v in _STATE_VERBS):
        return True
    # 「第一个改个时间」：序数指代 + 状态动词
    if _ORDINAL_HEAD_RE.match(text) and any(v in text for v in _STATE_VERBS):
        return True
    return False


class InputIntent(str, Enum):
    """一句话到底想干什么。

    区分三件事是这次事故的直接教训：把它们混成「是不是任务」一个布尔值，
    就会在「问」和「改」之间漏判，进而造出垃圾数据。
    """

    #: 像是要记一件新的事
    RECORD = "record"
    #: 在问信息
    QUESTION = "question"
    #: 要改动已有事务（目前不支持）
    MUTATE = "mutate"


#: 「够长就算有语境」的分界。短命令由动词信号兜住，带日期的长任务由时间信号
#: 兜住，剩下的短句（你好 / ok / 在吗 / 收到）由这条挡掉。
_RECORD_MIN_LEN = 10

#: 时间/紧迫标记。**正向**表：加词只会让更多句子**更愿意被记**，
#: 那是我们要的方向（反之做成负向黑名单，加词会让系统更爱乱记）。
#:
#: 刻意包含「要交」「得」「需要」这类不带具体时间的紧迫词 ——
#: 「下周二要交的销售周报初稿」既有日期也有「要交」，任一命中即可。
_RECORD_TIME_MARKS = (
    "下周", "下个", "下周", "本周", "这周", "明天", "后天", "今天", "昨天",
    "月底", "月初", "早上", "上午", "中午", "下午", "晚上", "今晚", "明早",
    "点", "号", "月", "号前", "之前", "之前要", "别忘", "记得", "务必",
    "要交", "截止", "deadline", "要写", "要做", "要买", "要发", "得",
)


def is_confidently_recordable(text: str) -> bool:
    """这句话**像是**一件要记的事吗？**不通过就应当反问，而不是记下。**

    为什么要有这个函数：``read_intent`` 的最后一行是 ``return RECORD``，
    于是**任何没被认出来的句子都变成一条事务**。实测的真实对话里，
    ``你好`` 建了两条事务（真库 ``0277e93d`` / ``0fbbdb13``）——
    而 ``你好`` 既不是问句也不是指令，它只是**没被认出来**。

    判定是「**三个正向信号，任一满足**」，不是「三个都满足才记」：

    1. 以任务动词**开头** —— ``_TASK_VERB_PREFIXES``（复用，不另抄一份）
    2. 含时间/紧迫标记 —— 「下周二」「下午三点」「别忘了」「要交」
    3. **够长**（≥ :data:`_RECORD_MIN_LEN` 字）—— 长句自带语境

    方向是刻意的：**代价不对称**。问错一句 = 多一轮对话；
    记错一条 = 往用户的角色结构里塞脏数据，而角色**反过来驱动分类**，
    污染会自我强化。所以「没把握」必须往「问」的方向失败。

    2 和 3 是**正向**表：往里加词只会让更多句子**更愿意被记**，
    那正是我们要的方向；反之若做成负向黑名单，加词会让系统更爱乱记。

    :data:`_RECORD_MIN_LEN` 的来历：短命令（修窗户 / 交周报）由信号 1 兜住，
    带日期的长任务由信号 2 兜住，剩下的短句（你好 / ok / 在吗）由信号 3 挡掉。
    10 字是量出来的分界，不是拍脑袋。
    """
    stripped = text.strip()
    if not stripped:
        return False
    if stripped.startswith(_TASK_VERB_PREFIXES):
        return True
    if any(mark in stripped for mark in _RECORD_TIME_MARKS):
        return True
    return len(stripped) >= _RECORD_MIN_LEN


def read_intent(text: str) -> InputIntent:
    """判断这句话是「记事 / 提问 / 改已有事务」。

    判定刻意**偏向拒绝**：宁可漏判（把问句当任务，行为退化但不毁数据），
    也不误放（造出垃圾事务或垃圾角色）。原因很直接 ——
    问错一句的后果是多一条脏记录，而拒错一句的后果是用户重打一遍。

    三层，从强到弱：

    1. 以动词开头要求改动 → ``MUTATE``
    2. 句尾问号，或以疑问词开头 → ``QUESTION``
    3. 含疑问词、句子够短、且**不是**以动作动词开头 → ``QUESTION``
       （第 3 条的「不是动作动词」很关键，否则「写清楚需求到底是什么」
       这类真任务会被误拒）
    """
    stripped = text.strip()
    if not stripped:
        return InputIntent.RECORD

    if _looks_like_mutation(stripped):
        return InputIntent.MUTATE

    if stripped[-1] in "？?" or stripped.startswith(_QUESTION_PREFIXES):
        return InputIntent.QUESTION

    if (len(stripped) <= 14
            and not stripped.startswith(_TASK_VERB_PREFIXES)
            and any(word in stripped for word in _QUESTION_WORDS)):
        return InputIntent.QUESTION

    return InputIntent.RECORD


def is_question_like(text: str) -> bool:
    """是不是**在问**（兼容旧调用点）。"""
    return read_intent(text) is InputIntent.QUESTION


def query_shaped(text: str) -> bool:
    """这句像**在要一个视图**，而不是**在要求做某事**。

    给 :class:`~freeagent.services.chat.ChatService` 的只读优先路由用。
    放在这里而不是 chat 里，是因为词表归意图判定管 —— 抄一份到别处
    就会漂移，而漂移的表现是「这里拦得住、那里拦不住」。

    两条守卫，缺一不可：

    1. 不像要改已有事务（「把周报改到下周三」）—— 那要如实说做不到，
       不能拿一个视图糊过去。
    2. **不含任何动作动词**。判的是**子串**不是前缀：「把今天的会议纪要
       整理一下」里有「今天」，但它是叫你干活。这类句子被视图抢走比
       建错事务更糟 —— 它会**无声无息地没发生**。
    """
    if _looks_like_mutation(text):
        return False
    return not any(verb in text for verb in _TASK_VERB_PREFIXES)


NOT_A_TASK_HINT = (
    "我只会做两件事：记一件事（说人话）、执行命令（/today、/task 等）。\n"
    "你这条像是问句 —— 查看用命令：/today 今天要动的，/all 全部，"
    "/task <id> 看某一条的上下文。"
)

CANNOT_MUTATE_HINT = (
    "我还没学会「改已有的一条」—— 现在只能新建事务。\n"
    "改排期用 /today-pin <id> <日期>，放弃用 /drop <id>，"
    "改形状用 /kind <id> <action|wait|reminder>。"
)


@dataclass(frozen=True, slots=True)
class DelegationIntent:
    """一句自然语言里「要改哪个项目、改什么」的**结构化**抽取结果。

    ## 为什么让模型抽，而不是规则剥

    第一版是「从句子里剥掉动词与项目名，剩下的就是要做的事」。它必然出错：
    「帮我改一下 README」剥出来是空的；「把 greet 函数改成返回你好」剥出来
    是「greet 函数返回你好」而不是「把 greet 函数改成返回你好」——
    **动词本身就是要��的一部分**。

    规则只在写表的人和用户说同一句话时成立。所以这件事交给模型，产出
    **结构**而不是字符串。

    ## 字段都可能为空 —— 那不是失败

    用户常常只说一半（「我要改 XiaoYuan」没说改什么）。空字段由上层**追问**
    补齐，而不是硬猜：猜出来的简报会被发出去让执行器改代码。
    """

    #: 要改的项目名。**必须原样取自**给定清单；抽不出是空串。
    project: str = ""
    #: 要做的事。抽不出是空串。
    brief: str = ""
    #: 这句话**到底是不是**在要求改代码。False 时上面两项无意义。
    is_delegation: bool = False

    def __bool__(self) -> bool:
        return self.is_delegation

@runtime_checkable

class LLMProvider(Protocol):
    """助手智能能力的统一接口。"""

    def classify(
        self, text: str, role_hints: Sequence[RoleHint]
    ) -> ClassificationResult:
        """推断事务形状（动作/等候/提醒）与角色候选。

        置信度不足时必须返回 ``need_clarification=True`` 并带上
        ``clarifying_question``，由上层去问用户 —— 不允许静默猜测。
        """
        ...

    @property
    def can_select_view(self) -> bool:
        """这个实现**是否真的会在** ``select_view`` 上给出意见。

        必须区分两件被 ``None`` 混在一起的事：

        - 「我看了，没匹配上任何视图」（真意见）
        - 「我压根不做选路」（``RuleBasedProvider``）

        混起来的后果很具体：离线路径下每个问句都会被当成「没匹配」，
        于是全部打上「猜的」标记，用户一开口就看到免责声明。

        所以：``RuleBasedProvider`` 为 ``False``，``DeepSeekProvider`` 为
        ``True``；上层据此决定是采信还是继续用关键词表。
        """
        ...

    def parse_delegation(
        self, text: str, known_projects: Sequence[str]
    ) -> DelegationIntent:
        """抽「要改哪个项目、改什么」。**抽不出就说抽不出。**

        ``known_projects`` 是授权清单里的项目名，``project`` **必须原样取自**
        它 —— 自创名字一律丢弃。闭集是这条接口的安全边界，与
        :meth:`select_view` 同一条道理。
        """
        ...

    def select_view(
        self, text: str, views: Sequence[tuple[str, str]]
    ) -> str | None:
        """从**封闭集合** ``views`` 里挑一个最贴切的视图名，挑不出返回 ``None``。

        ``views`` 是 ``(名字, 中文说明)`` 的序列。说明由拥有视图的那一层
        给出 —— 每个视图「到底显示什么」是领域知识，放在视图旁边才不会
        和实现漂移；本方法只负责把选择收窄到这些名字之内。

        这只决定「**读哪张表**」，不产出任何事实 —— 事实仍由规则读库渲染。
        所以它可以交给模型：语义理解是模型的强项，而「答案必须等于数据」
        这条地基并没有被动摇。

        契约（实现必须遵守）：

        - 只能返回 ``views`` 里的名字，**或** ``None``。自创名字一律丢弃
          并当 ``None`` 处理 —— 封闭集是这条接口的安全边界。
        - ``None`` 是**一等公民**，不是失败。用户问「你这周过得咋样」
          就该是 ``None``。若没有这个出口，模型会被迫挑一个，于是
          「不确定」就被渲染成了「自信地答错」。
        """
        ...

    def refine_title(self, text: str) -> str:
        """把一段口语化输入收敛成一句话标题。"""
        ...

    def split_steps(
        self, task: TaskRef, instruction: str | None = None
    ) -> tuple[str, ...]:
        """把大事务拆成几个步骤。

        **只拆，不排序。** 不得产出优先级判断。
        """
        ...

    def draft(self, task: TaskRef, instruction: str) -> str:
        """产出一份可供用户圈改的结构化草稿。"""
        ...

    def suggest_schedule(
        self, task: TaskRef, signals: Sequence[SignalRef]
    ) -> str:
        """把排序信号渲染成一句建议文本。

        只描述信号，**不得给出优先级结论**。
        """
        ...
