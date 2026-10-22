"""领域枚举与规范性常量。

本模块是纯声明，不含 I/O 与业务判断。设计契约见
``docs/个人事务助手设计方案.md`` 第四章与第七章。
"""

from __future__ import annotations

from enum import Enum

__all__ = [
    "TaskState",
    "TaskKind",
    "RecordType",
    "ArtifactStatus",
    "CapturedStatus",
    "WaitingKind",
    "ContactKind",
    "PromiseStatus",
    "SortSignalCode",
    "SORT_SIGNAL_WEIGHTS",
    "STATE_LABELS",
    "KIND_LABELS",
    "CAPTURED_STATUS_LABELS",
    "CONTACT_KIND_LABELS",
    "PROMISE_STATUS_LABELS",
    "SIGNAL_LABELS",
]


class TaskState(str, Enum):
    """事务生命周期状态（与 ``kind``、``scheduled_for`` 三者正交）。"""

    INBOX = "inbox"
    ACTIVE = "active"
    BLOCKED = "blocked"
    DONE = "done"
    DROPPED = "dropped"

    @property
    def is_open(self) -> bool:
        """未结束（``DONE`` / ``DROPPED`` 之外的都算未结束）。"""
        return self not in _TERMINAL_STATES

    @property
    def label(self) -> str:
        return STATE_LABELS[self]


_TERMINAL_STATES: frozenset[TaskState] = frozenset({TaskState.DONE, TaskState.DROPPED})


class TaskKind(str, Enum):
    """事务形状：解决「是动作还是提醒」的边界模糊问题。"""

    ACTION = "action"
    WAIT = "wait"
    REMINDER = "reminder"

    @property
    def label(self) -> str:
        return KIND_LABELS[self]

    @property
    def needs_deliverable(self) -> bool:
        """动作类才需要真正的产出物，提醒类到点即销账。"""
        return self is TaskKind.ACTION


class RecordType(str, Enum):
    """只追加日志的事件类型。"""

    CREATED = "created"
    NOTE = "note"
    STATUS_CHANGE = "status_change"
    KIND_CHANGE = "kind_change"
    ROLE_CHANGE = "role_change"
    SCHEDULE_CHANGE = "schedule_change"
    ARTIFACT_CREATED = "artifact_created"
    ARTIFACT_ACCEPTED = "artifact_accepted"
    ARTIFACT_SUPERSEDED = "artifact_superseded"
    PAUSE = "pause"
    RESUME = "resume"
    ROLLOVER = "rollover"
    REMINDER_FIRED = "reminder_fired"
    #: 委派：把这件事交给外部执行器（opencode）。
    #: 记「派发出去了」是为了让**重跑幂等** —— 派过一次就不该再派。
    DELEGATION_DISPATCHED = "delegation_dispatched"
    DELEGATION_SUCCEEDED = "delegation_succeeded"
    DELEGATION_FAILED = "delegation_failed"
    #: 「这次尝试作废，允许重新派发」——**人显式要求重来**时才落这条。
    #:
    #: 为什么需要它：``delegation_failed`` 若是**派发前就被拒**（白名单不符 /
    #: 没有闸门），重试一万次也是同一个结果，所以判定侧**刻意不自动重派**。
    #: 但那是「没改配置」的判定 —— 一旦用户改好配置，就该有个办法让它重来，
    #: 而落库那句「可以 /note 记下原因后重派」是**空头承诺**：``note`` 不在
    #: terminal 集合里，对判定完全不可见，什么也重置不了。
    #:
    #: 形状上它是**只追加**的第四条路，不改写历史：判定侧认它，于是
    #: 「它之后才算新的一次尝试」。这与本项目「判据从记录形状推出来，
    #: 不给失败加标签」是同一条纪律 —— reset 本身就是形状的一部分，
    #: 而且天然可审计（谁、何时、为何要求重来）。
    DELEGATION_RESET = "delegation_reset"


class ArtifactStatus(str, Enum):
    """草稿产物的生命周期。"""

    DRAFT = "draft"
    ACCEPTED = "accepted"
    SUPERSEDED = "superseded"


class CapturedStatus(str, Enum):
    """待处理收集器里一条捕获的生命周期（设计文档 11.18）。

    **刻意与 ``TaskState`` 分开**：收集器里放的还不是事务 —— 它是「收下了
    但还没澄清」的原材料。``pending`` 是唯一入口态；``clarified`` 意味着
    「用户显式 /pending N 之后它变成了一条事务」，``dismissed`` 意味着
    「用户明确说这条不要了」。两个终态都只由**用户显式动作**产生，
    没有任何自动迁移 —— 自动澄清就等于替用户决定「这是什么」。
    """

    PENDING = "pending"
    CLARIFIED = "clarified"
    DISMISSED = "dismissed"


class WaitingKind(str, Enum):
    """等候对象类型。"""

    PERSON = "person"
    SYSTEM = "system"
    TIME = "time"


class ContactKind(str, Enum):
    """关系对象的类型（设计文档 11.19）。

    **为什么要有这张表**：``WaitingOn.who_or_what`` 是**自由文本**，
    「在等张总」与「在等张总回合同」都是它，但没法聚合 ——
    你问「我跟张总有几次往来」，得去翻所有事务的文本。
    关系对象把它变成**可检索的一等公民**（20.1.5）。

    刻意只分这么几类：分类的价值在于**能聚合**，分太细就退化成自由文本
    （11.10.2「能力声明一旦手写就会陈旧」同源的问题）。用户不写标识符，
    只从这几个里选。
    """

    PERSON = "person"
    """具体的人：同事、客户、老师、孩子。"""

    ORG = "org"
    """组织：公司、团队、学校、对方公司。"""

    SELF = "self"
    """自己。刻意单列 —— 「我答应自己」是一种真实且常见的承诺，
    与「我答应别人」的违约代价不同（前者代价小，后者有社会代价）。"""


class PromiseStatus(str, Enum):
    """对外承诺的生命周期（设计文档 11.19）。

    承诺的三个终态都只由**用户显式动作**产生：

    * ``KEPT`` —— 兑现了（用户说「交了」）；
    * ``BROKEN`` —— 违约了（用户说「没做到」）；
    * ``WITHDRAWN`` —— 对方取消了/我收回去了。

    刻意**没有「自动违约」**：过了承诺日期不等于违约 ——
    可能是延期了、可能是对方改主意了。系统只能摆事实，
    判「违约」是替用户下结论（第十五章）。
    """

    OPEN = "open"
    """未兑现（唯一入口态）。"""

    KEPT = "kept"
    BROKEN = "broken"
    WITHDRAWN = "withdrawn"



class SortSignalCode(str, Enum):
    """透明弱信号代码。排序只由这些确定性信号构成，不做价值打分。"""

    OVERDUE_WAIT = "overdue_wait"
    DEADLINE_RISK = "deadline_risk"
    PROMISE_OVERDUE = "promise_overdue"
    DEPENDED_ON = "depended_on"
    RESUME_STALE = "resume_stale"
    WAITING_TOO_LONG = "waiting_too_long"
    SCHEDULED_TODAY = "scheduled_today"
    INBOX_UNSORTED = "inbox_unsorted"
    ENERGY_FIT = "energy_fit"


#: 规范性权重表，与设计文档 7.2 节逐项一致。改这里等于改契约。
SORT_SIGNAL_WEIGHTS: dict[SortSignalCode, int] = {
    SortSignalCode.OVERDUE_WAIT: 50,
    SortSignalCode.DEADLINE_RISK: 40,
    #: 承诺逾期未兑现（11.19）—— 权重**高于** ``DEADLINE_RISK``。
    #:
    #: 19.5.3 的理由：自己的截止日改了没什么，**答应别人的没有内部宽限期**。
    #: 逾期的代价是社会性的（对方失望、关系受损），而 ``DEADLINE_RISK``
    #: 说的只是「你自己的时间不够了」。所以 45 —— 排第二，低于
    #: ``OVERDUE_WAIT``（那是在等别人，代价是**卡住**，是全局性的）。
    #:
    #: ⚠️ 名字里**没有「违约」**：这个信号只陈述事实「逾期了、还没兑现」，
    #: 判「违约」是用户的事（11.19.5）—— 可能延期了、可能对方改主意了。
    #: 系统的职责是把事实摆到最显眼处，不是替用户下结论。
    SortSignalCode.PROMISE_OVERDUE: 45,
    SortSignalCode.DEPENDED_ON: 30,
    SortSignalCode.RESUME_STALE: 20,
    SortSignalCode.WAITING_TOO_LONG: 15,
    SortSignalCode.SCHEDULED_TODAY: 10,
    SortSignalCode.ENERGY_FIT: 8,
    SortSignalCode.INBOX_UNSORTED: 5,
}

STATE_LABELS: dict[TaskState, str] = {
    TaskState.INBOX: "待办",
    TaskState.ACTIVE: "进行中",
    TaskState.BLOCKED: "等候中",
    TaskState.DONE: "已完成",
    TaskState.DROPPED: "已放弃",
}

KIND_LABELS: dict[TaskKind, str] = {
    TaskKind.ACTION: "动作",
    TaskKind.WAIT: "等候",
    TaskKind.REMINDER: "提醒",
}

CAPTURED_STATUS_LABELS: dict[CapturedStatus, str] = {
    CapturedStatus.PENDING: "待处理",
    CapturedStatus.CLARIFIED: "已澄清",
    CapturedStatus.DISMISSED: "已忽略",
}

CONTACT_KIND_LABELS: dict[ContactKind, str] = {
    ContactKind.PERSON: "人",
    ContactKind.ORG: "组织",
    ContactKind.SELF: "自己",
}

PROMISE_STATUS_LABELS: dict[PromiseStatus, str] = {
    PromiseStatus.OPEN: "未兑现",
    PromiseStatus.KEPT: "已兑现",
    PromiseStatus.BROKEN: "已违约",
    PromiseStatus.WITHDRAWN: "已收回",
}

SIGNAL_LABELS: dict[SortSignalCode, str] = {
    SortSignalCode.OVERDUE_WAIT: "该跟进了",
    SortSignalCode.DEADLINE_RISK: "临近截止",
    SortSignalCode.DEPENDED_ON: "别人在等它",
    SortSignalCode.RESUME_STALE: "中断已久",
    SortSignalCode.WAITING_TOO_LONG: "等太久了",
    SortSignalCode.SCHEDULED_TODAY: "排在今天",
    SortSignalCode.INBOX_UNSORTED: "还没开始",
    SortSignalCode.ENERGY_FIT: "精力档位匹配",
}
