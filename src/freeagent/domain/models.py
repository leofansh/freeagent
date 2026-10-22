"""领域值对象。

纯数据，零 I/O、零业务判断。所有集合类字段使用 ``tuple`` 以保持不可变；
设计契约见 ``docs/个人事务助手设计方案.md`` 第十一章。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime

from .enums import (
    ArtifactStatus,
    CapturedStatus,
    ContactKind,
    PromiseStatus,
    RecordType,
    SortSignalCode,
    TaskKind,
    TaskState,
    WaitingKind,
)

__all__ = [
    "new_id",
    "Role",
    "WaitingOn",
    "Task",
    "TaskRecord",
    "Artifact",
    "CapturedItem",
    "Contact",
    "Promise",
    "SortSignal",
    "ScoredTask",
    "RestoreView",
    "REMINDER_FIRED_WINDOW",
    "DEFAULT_ROLE_NAME",
]

#: 恢复契约默认返回的最近记录条数（设计文档 8.1 节）。
REMINDER_FIRED_WINDOW = 20

#: 第一条事务在「还没有任何角色」时归入的默认脉络名。
#:
#: 它住在**领域层**而不是 CLI：``cli`` 和 ``services.chat`` 都要用，而后者是
#: 飞书通道的入口。原先只定义在 ``cli/app.py``，于是 ``chat.py`` 去
#: ``from ..domain import DEFAULT_ROLE_NAME`` 直接 ImportError ——
#: 飞书里发第一条事务必崩，而 CLI 正常，两边都测过就更容易漏。
#:
#: 也不能反过来让 ``chat`` 去 import ``cli``：那是 services 依赖 cli，
#: 分层就倒了。
DEFAULT_ROLE_NAME = "默认脉络"


def new_id() -> str:
    """生成实体 id（随机 hex）。

    **刻意不做单调前缀**：界面和 CLI 都用 ``id[:8]`` 做前缀匹配，
    若前缀是时间戳，几秒内创建的多个实体会共用同一前缀，匹配就会张冠李戴。
    排序的确定性不靠 id，而靠 :func:`freeagent.services.sorting.sort_tasks`
    的稳定排序 + 仓储层的创建顺序（见该函数说明）。
    """
    return uuid.uuid4().hex


@dataclass(frozen=True, slots=True)
class Role:
    """事务脉络。一个事务至少属于一个角色，且可以属于多个。"""

    id: str
    name: str
    created_at: datetime
    updated_at: datetime
    note: str | None = None
    default_definition_of_done: str | None = None
    #: 该角色默认的**工作模式**（OpenCode 的 agent 精确名）。
    #: 选段（Web「编程」页签）留空时，用这个兜底；
    #: 优先级：显式选择 > 角色默认 > 策略默认（设计文档 11.13.6 / 11.14）。
    default_agent: str | None = None
    active: bool = True
    icon: str | None = None
    color: str | None = None
    merged_into: str | None = None

    @property
    def is_merged(self) -> bool:
        return self.merged_into is not None


@dataclass(frozen=True, slots=True)
class WaitingOn:
    """等候对象：回答「为什么卡住」「在等谁」。

    ``contact_id`` 是 11.19 加的**可选**指向：``who_or_what`` 是自由文本，
    「在等张总」与「在等张总回合同」都是它 —— 关系一等化之后，
    「等张总」这一维应该能连到 ``contacts`` 表上，否则「我跟张总之间有几笔
    在等的事」还是得靠人眼认文本。

    **刻意可选、且不建外键**：历史行没有这个字段，读出来是 ``None``，
    与「没挂关系」是同一件事；用户也不必为记一句「在等对方回话」先建人。
    """

    kind: WaitingKind
    who_or_what: str
    since: datetime
    follow_up_at: datetime | None = None
    note: str | None = None
    contact_id: str | None = None

    def describe(self) -> str:
        base = f"{self.kind.value} · {self.who_or_what}"
        if self.follow_up_at is not None:
            base += f"（跟进 {self.follow_up_at:%Y-%m-%d %H:%M}）"
        return base


@dataclass(frozen=True, slots=True)
class Task:
    """一件事务。

    ``state`` / ``kind`` / ``scheduled_for`` 三者正交：
    生命周期、形状、调度属性互不覆盖。
    """

    id: str
    title: str
    role_ids: tuple[str, ...]
    state: TaskState
    kind: TaskKind
    entered_at: datetime
    created_at: datetime
    updated_at: datetime
    intent: str | None = None
    definition_of_done: str | None = None
    scheduled_for: date | None = None
    due_time: datetime | None = None
    reminder_time: datetime | None = None
    #: 重复提醒的**生成器**（原始 JSON 文本），空 = 一次性提醒。
    #:
    #: 刻意存**文本**而不是 ``RecurrenceRule`` 对象：那个类型的
    #: ``next_after()`` 要用 ``zoneinfo``（读 tzdata）且含业务判断，
    #: 而 domain 层纪律是「零 I/O、零业务判断」。解释留给
    #: :mod:`freeagent.services.recurrence`。
    #:
    #: 分工：``reminder_time`` 始终是**下一次触发的瞬时**（指针），
    #: 本字段是产生它的规则。确认送达后引擎问规则要下一个，写回指针 ——
    #: 于是到期查询、合并摘要、错过窗口全都不需要知道规则存在。
    reminder_rule: str | None = None
    #: 每次写入自增。**乐观并发**用：读到的 revision 与写入时不一致，
    #: 说明中间有人改过 —— 这时该拒绝而不是覆盖。
    #:
    #: 为什么不用 ``updated_at``：它是时间戳，同一秒内的两次写入分不出先后。
    #: 并发控制要的是**单调计数**，不是「什么时候改的」。
    revision: int = 0
    waiting_on: WaitingOn | None = None
    blocked_by: tuple[str, ...] = ()
    completed_at: datetime | None = None
    dropped_at: datetime | None = None
    last_resumed_at: datetime | None = None
    progress_note: str | None = None
    #: 非空 = 这是一条**委派**事务，要交给外部执行器（如 opencode）在
    #: 这个项目目录里干活。``None`` = 普通事务。
    #:
    #: 刻意复用 ``tasks`` 表而不是另开一张表：委派不需要新概念，
    #: 它就是「一条知道自己该在哪个项目里被完成的事务」。
    project_path: str | None = None
    #: 这条委派是从哪个飞书会话发起的（``oc_`` 开头的 chat_id）。
    #: 非空 = 执行完要把结果**推回那个会话**，闭环才算合上。
    #:
    #: 没有它的话，从飞书发起的委派只能你主动去终端看结果 ——
    #: 「在飞书里派一件事，然后结果自己飞回来」这件事就不成立。
    #: ``None`` = 从终端发起的，结果只进产物链与日志（终端本来就能看）。
    delegate_chat_id: str | None = None
    #: **谁**发起的这条委派（飞书 ``open_id``）。
    #:
    #: 与 :attr:`delegate_chat_id` 是两回事：那是「发到哪」（会话），
    #: 这是「谁发起的」（人）。而**后者才是「只有发起人能批」那条规则的输入**。
    #:
    #: 可空：终端发起的委派没有飞书身份，那是**正常情况**；
    #: 不该为了非空而填一个「随便某个人」——
    #: 那等于凭空造一个越权面。
    delegate_requested_by: str | None = None
    current_artifact_id: str | None = None

    @property
    def is_open(self) -> bool:
        return self.state.is_open

    @property
    def primary_role_id(self) -> str | None:
        """仅用于展示的第一个角色。语义上所有角色平级。"""
        return self.role_ids[0] if self.role_ids else None

    @property
    def intent_pending(self) -> bool:
        """意图待澄清：``intent`` 为空。"""
        return self.intent is None or not self.intent.strip()


@dataclass(frozen=True, slots=True)
class TaskRecord:
    """只追加、永不修改的历史记录。事务上下文的唯一真源。"""

    id: str
    task_id: str
    ts: datetime
    type: RecordType
    content: str
    # 每任务单调递增（RecordRepo.append 取号）。缺省 None 是给存量构造点
    # 留的兼容面：老记录（迁移回填前读出）没有它，排序会落到 ts 后面。
    seq: int | None = None


@dataclass(frozen=True, slots=True)
class Artifact:
    """带版本的草稿产物。旧版永不删除。"""

    id: str
    task_id: str
    version: int
    title: str
    content: str
    status: ArtifactStatus
    created_at: datetime
    accepted_at: datetime | None = None
    supersedes: str | None = None

    @property
    def is_editable(self) -> bool:
        return self.status is not ArtifactStatus.SUPERSEDED


@dataclass(frozen=True, slots=True)
class CapturedItem:
    """待处理收集器里的一条捕获（设计文档 11.18）。

    **它不是事务**：这是本类型存在的全部理由。事务是「你承诺要做什么」，
    而捕获只是「东西先进来了」—— 一张截图、一个文件、一条引用。
    两者之间隔着一个**用户显式动作**（``/pending N`` 澄清），
    没有自动迁移。混成一张表的代价见 11.18：排序会把没澄清的东西
    算成承诺，而「它到底是什么」只有用户知道。

    字段分工：

    * ``media_key``：飞书侧的 key（``image_key`` / ``file_key``），
      **重新下载的唯一凭据** —— 本地文件丢了还能捞回来；
    * ``path``：本地落盘的路径。下载失败时是 ``.meta.json`` 引用文件，
      此时 ``size`` 是消息里声称的大小、不是实测字节数；
    * ``task_id``：澄清后指向生成的事务；未澄清为 ``None``。
    """

    id: str
    source: str
    kind: str
    media_key: str
    name: str
    size: int
    path: str
    status: CapturedStatus
    created_at: datetime
    resolved_at: datetime | None = None
    task_id: str | None = None


@dataclass(frozen=True, slots=True)
class Contact:
    """关系对象：**人**或组织（设计文档 11.19 / 20.1.5）。

    **它为什么不是角色**：角色是「事务的脉络」（工作 / 家庭 / 投资），
    回答「这件事属于哪一块」；关系对象是「事务背后的人」，
    回答「我跟谁之间有这笔账」。两者维度不同，可以正交 ——
    一条「给张总交付报价」的事务既属于「工作」角色，也指向「张总」这个关系。

    **它为什么不是自由文本**：``WaitingOn.who_or_what`` 是文本，
    「在等张总」与「在等张总回合同」都是它，但没法聚合 ——
    你问「我跟张总往来过几次」，得去翻所有事务的文本再人工认。
    有了关系对象就能直接数、直接列（11.19.3）。

    ``note`` 放「怎么找他」「答应了什么」这类自由文本 ——
    结构化不进来的部分老实放在一边，不假装它能进（11.19.2）。
    """

    id: str
    name: str
    kind: ContactKind
    created_at: datetime
    updated_at: datetime
    note: str | None = None
    #: 合并重定向（与角色同一条纪律，3.5）。读时沿它追到根。
    merged_into: str | None = None

    @property
    def is_merged(self) -> bool:
        return self.merged_into is not None


@dataclass(frozen=True, slots=True)
class Promise:
    """一条**对外承诺**（设计文档 11.19 / 19.5.3）。

    **它是什么**：我答应别人（或自己）要做的事。9.1 的 ``WaitingOn``
    只记了「我等别人」那一半；这一半是反方向的 —— **我欠别人的**。

    **为什么它单独一张表而不是事务的一个字段**：

    * 承诺需要**自己的生命周期**（未兑现 → 已兑现 / 已违约 / 已收回），
      而事务的生命周期是「待办 → 进行中 → 完成」，两套状态机对不齐；
    * 承诺要指向**关系对象**（``contact_id``）才能聚合，
      而事务上挂的是 ``role_ids``（脉络）；
    * 一条事务可以承载多条承诺吗？刻意**不**允许 —— 一条承诺对应
      一个兑现物（``task_id``），一对一。多对多会让「谁欠谁多少」变糊。

    **终态只由用户显式动作产生**（``PromiseStatus``）：过了承诺日期
    **不等于**违约 —— 可能延期了、可能对方改主意了。系统只摆事实，
    判「违约」是替用户下结论（第十五章）。
    """

    id: str
    task_id: str
    #: 承诺给谁。指向 :class:`Contact`。``None`` = 不指向具体关系
    #: （「我答应过要搞定这件事」，对象不明）。
    contact_id: str | None
    #: 承诺了什么。**一句话**，不是自由文本块 —— 它是给列表看的。
    statement: str
    #: 答应什么时候做到。``None`` = 没给日期（「尽快」也是一种承诺）。
    promised_for: datetime | None
    status: PromiseStatus
    created_at: datetime
    updated_at: datetime
    #: 兑现/违约/收回的时刻。
    resolved_at: datetime | None = None
    note: str | None = None

    @property
    def is_open(self) -> bool:
        return self.status is PromiseStatus.OPEN


@dataclass(frozen=True, slots=True)
class SortSignal:
    """一条透明弱信号。``reason`` 面向用户，必须随任务一起展示。"""

    code: SortSignalCode
    weight: int
    reason: str


@dataclass(frozen=True, slots=True)
class ScoredTask:
    """排序结果：任务 + 命中的全部信号 + 总权重。"""

    task: Task
    signals: tuple[SortSignal, ...]
    total_weight: int


@dataclass(frozen=True, slots=True)
class RestoreView:
    """恢复契约的返回值。

    打开事务时必须返回本结构；缺任何一项都算恢复失败。
    见设计文档第八章。
    """

    task: Task
    effective_definition_of_done: str | None
    current_artifact: Artifact | None
    progress_note: str | None
    recent_records: tuple[TaskRecord, ...]
    waiting_on: WaitingOn | None
    next_actions: tuple[str, ...]
