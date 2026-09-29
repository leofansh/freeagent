"""domain 层单元测试。"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from freeagent.domain import (
    SORT_SIGNAL_WEIGHTS,
    Artifact,
    ArtifactStatus,
    RecordType,
    SortSignal,
    SortSignalCode,
    Task,
    TaskKind,
    TaskRecord,
    TaskState,
    WaitingKind,
    WaitingOn,
    new_id,
)

NOW = datetime(2026, 9, 26, 9, 0)


def test_enums_are_str_valued():
    assert TaskState("inbox") is TaskState.INBOX
    assert TaskKind("wait") is TaskKind.WAIT
    assert ArtifactStatus("superseded") is ArtifactStatus.SUPERSEDED
    assert RecordType("rollover") is RecordType.ROLLOVER
    assert WaitingKind("person") is WaitingKind.PERSON
    assert SortSignalCode("overdue_wait") is SortSignalCode.OVERDUE_WAIT


def test_state_open_flag():
    assert TaskState.INBOX.is_open
    assert TaskState.ACTIVE.is_open
    assert TaskState.BLOCKED.is_open
    assert not TaskState.DONE.is_open
    assert not TaskState.DROPPED.is_open


def test_weights_match_design_doc():
    """回归：权重表是契约，改动必须同步文档 7.2 节。"""
    assert SORT_SIGNAL_WEIGHTS == {
        SortSignalCode.OVERDUE_WAIT: 50,
        SortSignalCode.DEADLINE_RISK: 40,
        SortSignalCode.DEPENDED_ON: 30,
        SortSignalCode.RESUME_STALE: 20,
        SortSignalCode.WAITING_TOO_LONG: 15,
        SortSignalCode.SCHEDULED_TODAY: 10,
        SortSignalCode.ENERGY_FIT: 8,
        SortSignalCode.INBOX_UNSORTED: 5,
    }


def test_new_id_is_unique_hex():
    a, b = new_id(), new_id()
    assert a != b
    assert len(a) == 32
    int(a, 16) and int(b, 16), "必须是纯 hex"


def test_new_id_prefix_is_discriminating():
    """id 前缀必须能区分实体 —— 界面和 CLI 都靠 ``id[:8]`` 前缀匹配。

    若前缀带时间戳，几秒内创建的多个实体会共用前缀，匹配就会张冠李戴。
    """
    ids = [new_id() for _ in range(50)]
    assert len({i[:8] for i in ids}) >= 45, "8 位前缀碰撞过多，前缀匹配不可用"


def _task(**kw) -> Task:
    base = dict(
        id="t", title="T", role_ids=("r",), state=TaskState.INBOX,
        kind=TaskKind.ACTION, entered_at=NOW, created_at=NOW, updated_at=NOW,
    )
    base.update(kw)
    return Task(**base)


def test_task_helpers():
    t = _task(role_ids=("a", "b"), intent="  ")
    assert t.primary_role_id == "a"
    assert t.intent_pending
    assert t.is_open
    assert not _task(intent="做出来").intent_pending


def test_task_is_frozen():
    t = _task()
    with pytest.raises(Exception):
        t.title = "x"  # type: ignore[misc]


def test_waiting_on_describe():
    w = WaitingOn(
        kind=WaitingKind.PERSON, who_or_what="客户", since=NOW,
        follow_up_at=datetime(2026, 9, 28, 10, 0),
    )
    assert "客户" in w.describe()
    assert "2026-09-28" in w.describe()


def test_artifact_editable_flag():
    art = Artifact(
        id="a", task_id="t", version=1, title="x", content="y",
        status=ArtifactStatus.DRAFT, created_at=NOW,
    )
    assert art.is_editable
    superseded = Artifact(
        id="b", task_id="t", version=2, title="x", content="y",
        status=ArtifactStatus.SUPERSEDED, created_at=NOW,
    )
    assert not superseded.is_editable


def test_task_record_fields():
    r = TaskRecord(id="r", task_id="t", ts=NOW, type=RecordType.NOTE, content="c")
    assert r.type is RecordType.NOTE
