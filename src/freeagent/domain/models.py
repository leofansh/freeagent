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
    active: bool = True
    icon: str | None = None
    color: str | None = None
    merged_into: str | None = None

    @property
    def is_merged(self) -> bool:
        return self.merged_into is not None


@dataclass(frozen=True, slots=True)
class WaitingOn:
    """等候对象：回答「为什么卡住」「在等谁」。"""

    kind: WaitingKind
    who_or_what: str
    since: datetime
    follow_up_at: datetime | None = None
    note: str | None = None

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
