"""领域层：纯值对象与枚举，零 I/O。"""

from __future__ import annotations

from .enums import (
    KIND_LABELS,
    SIGNAL_LABELS,
    SORT_SIGNAL_WEIGHTS,
    STATE_LABELS,
    ArtifactStatus,
    RecordType,
    SortSignalCode,
    TaskKind,
    TaskState,
    WaitingKind,
)
from .errors import (
    ConflictError,
    FreeAgentError,
    InvariantViolation,
    LLMError,
    NotFoundError,
    ValidationError,
)
from .models import (
    DEFAULT_ROLE_NAME,
    REMINDER_FIRED_WINDOW,
    Artifact,
    RestoreView,
    Role,
    ScoredTask,
    SortSignal,
    Task,
    TaskRecord,
    WaitingOn,
    new_id,
)

__all__ = [
    "Artifact",
    "ArtifactStatus",
    "ConflictError",
    "DEFAULT_ROLE_NAME",
    "FreeAgentError",
    "InvariantViolation",
    "KIND_LABELS",
    "LLMError",
    "NotFoundError",
    "REMINDER_FIRED_WINDOW",
    "RecordType",
    "RestoreView",
    "Role",
    "SIGNAL_LABELS",
    "SORT_SIGNAL_WEIGHTS",
    "STATE_LABELS",
    "ScoredTask",
    "SortSignal",
    "SortSignalCode",
    "Task",
    "TaskKind",
    "TaskRecord",
    "TaskState",
    "ValidationError",
    "WaitingKind",
    "WaitingOn",
    "new_id",
]
