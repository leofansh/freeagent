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
    "WaitingKind",
    "SortSignalCode",
    "SORT_SIGNAL_WEIGHTS",
    "STATE_LABELS",
    "KIND_LABELS",
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


class ArtifactStatus(str, Enum):
    """草稿产物的生命周期。"""

    DRAFT = "draft"
    ACCEPTED = "accepted"
    SUPERSEDED = "superseded"


class WaitingKind(str, Enum):
    """等候对象类型。"""

    PERSON = "person"
    SYSTEM = "system"
    TIME = "time"


class SortSignalCode(str, Enum):
    """透明弱信号代码。排序只由这些确定性信号构成，不做价值打分。"""

    OVERDUE_WAIT = "overdue_wait"
    DEADLINE_RISK = "deadline_risk"
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
