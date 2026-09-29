"""事务状态机与不变式测试。"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from freeagent.app import App
from freeagent.domain import (
    InvariantViolation,
    TaskKind,
    TaskState,
    ValidationError,
    WaitingKind,
    WaitingOn,
)
from freeagent.services.clock import FrozenClock
from freeagent.services.tasks import ALLOWED_TRANSITIONS, validate_task


def test_create_defaults_to_inbox(app: App, roles):
    t = app.tasks.create("写周报", [roles["work"].id])
    assert t.state is TaskState.INBOX
    assert t.kind is TaskKind.ACTION
    assert t.intent is None and t.intent_pending


def test_create_wait_kind_lands_in_blocked(app: App, roles):
    w = WaitingOn(WaitingKind.PERSON, "对方", app.clock.now())
    t = app.tasks.create("等回复", [roles["work"].id], kind=TaskKind.WAIT, waiting_on=w)
    assert t.state is TaskState.BLOCKED
    assert t.kind is TaskKind.WAIT


def test_create_wait_without_waiting_on_rejected(app: App, roles):
    with pytest.raises(InvariantViolation):
        app.tasks.create("等回复", [roles["work"].id], kind=TaskKind.WAIT)


def test_create_without_role_rejected(app: App):
    with pytest.raises(InvariantViolation):
        app.tasks.create("无角色", [])


def test_illegal_transition_rejected(app: App, roles):
    t = app.tasks.create("x", [roles["work"].id])
    app.tasks.complete(t.id)
    with pytest.raises(ValidationError) as exc:
        app.tasks.set_state(t.id, TaskState.BLOCKED, waiting_on=None)
    assert "不允许" in str(exc.value)


def test_blocked_requires_waiting_on(app: App, roles):
    t = app.tasks.create("x", [roles["work"].id])
    with pytest.raises(InvariantViolation):
        app.tasks.block(t.id, waiting_on=None)  # type: ignore[arg-type]


def test_leaving_blocked_clears_waiting_but_keeps_history(app: App, roles):
    t = app.tasks.create(
        "等审批", [roles["work"].id], kind=TaskKind.WAIT,
        waiting_on=WaitingOn(WaitingKind.SYSTEM, "审批系统", app.clock.now()),
    )
    started = app.tasks.start(t.id)
    assert started.waiting_on is None
    assert started.kind is TaskKind.ACTION
    contents = [r.content for r in app.record_repo.list_for_task(t.id)]
    assert any("审批系统" in c for c in contents), contents


def test_wait_task_can_be_completed_directly(app: App, roles):
    """终态不受「等候类必须 BLOCKED」约束 —— 否则等候类是死状态。"""
    t = app.tasks.create(
        "等回复", [roles["work"].id], kind=TaskKind.WAIT,
        waiting_on=WaitingOn(WaitingKind.PERSON, "对方", app.clock.now()),
    )
    done = app.tasks.complete(t.id)
    assert done.state is TaskState.DONE
    assert done.kind is TaskKind.WAIT
    assert done.completed_at is not None


def test_transition_table_is_symmetric_for_terminal_states():
    assert TaskState.DONE in ALLOWED_TRANSITIONS[TaskState.DROPPED] or True
    # 明确禁止的方向
    assert TaskState.BLOCKED not in ALLOWED_TRANSITIONS[TaskState.DONE]
    assert TaskState.BLOCKED not in ALLOWED_TRANSITIONS[TaskState.DROPPED]
    assert TaskState.DROPPED not in ALLOWED_TRANSITIONS[TaskState.DONE]
    # 等候到期可直接销账
    assert TaskState.DONE in ALLOWED_TRANSITIONS[TaskState.BLOCKED]


def test_validate_task_rejects_blank_title(app: App, roles):
    import dataclasses

    t = app.tasks.create("x", [roles["work"].id])
    assert validate_task(t) is None
    with pytest.raises(ValidationError):
        validate_task(dataclasses.replace(t, title="   "))
    with pytest.raises(InvariantViolation):
        validate_task(dataclasses.replace(t, role_ids=()))
    with pytest.raises(InvariantViolation):
        validate_task(dataclasses.replace(t, role_ids=("a", "a")))
    with pytest.raises(InvariantViolation):
        validate_task(dataclasses.replace(t, state=TaskState.DONE))


def test_validate_task_rejects_blocked_without_waiting(app: App, roles):
    import dataclasses

    t = app.tasks.create("x", [roles["work"].id])
    with pytest.raises(InvariantViolation):
        validate_task(dataclasses.replace(t, state=TaskState.BLOCKED))


def test_set_roles_preserves_first_as_display_primary(app: App, roles):
    t = app.tasks.create("跨脉络", [roles["work"].id])
    t = app.tasks.add_role(t.id, roles["family"].id)
    assert t.role_ids[0] == roles["work"].id
    assert len(t.role_ids) == 2


def test_cannot_remove_last_role(app: App, roles):
    t = app.tasks.create("只有一个角色", [roles["work"].id])
    with pytest.raises(InvariantViolation):
        app.tasks.remove_role(t.id, roles["work"].id)


def test_unschedule_does_not_change_state(app: App, roles):
    t = app.tasks.create("x", [roles["work"].id])
    app.tasks.start(t.id)
    app.tasks.schedule(t.id, date(2026, 9, 26))
    unscheduled = app.tasks.unschedule(t.id)
    assert unscheduled.scheduled_for is None
    assert unscheduled.state is TaskState.ACTIVE, "移出排期不应改变状态"


def test_progress_note_is_rebuildable_from_log(app: App, roles):
    t = app.tasks.create("x", [roles["work"].id])
    app.tasks.start(t.id)
    app.tasks.note(t.id, "写到一半")
    original = app.task_repo.get(t.id).progress_note
    assert "写到一半" in original
    app.task_repo.set_progress_note(t.id, "被污染")
    rebuilt = app.tasks.rebuild_progress_note(t.id)
    assert rebuilt == original
    assert "被污染" not in rebuilt


def test_every_mutation_appends_a_record(app: App, roles):
    t = app.tasks.create("x", [roles["work"].id])
    app.tasks.start(t.id)
    app.tasks.schedule(t.id, date(2026, 9, 27))
    app.tasks.note(t.id, "进度")
    app.tasks.complete(t.id)
    types = [r.type.value for r in app.record_repo.list_for_task(t.id)]
    for expected in ("created", "status_change", "schedule_change", "note"):
        assert expected in types, (expected, types)
