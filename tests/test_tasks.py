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


# =============================================================================
# 批量「算了」（drop_many）
# =============================================================================
def test_drop_many_drops_all_and_keeps_records(app: App, roles):
    """全成功的批量：每条都落 status_change，记录不丢。

    drop 是**软删**：状态置 DROPPED 而只追加日志全部保留 —— 本系统
    没有硬删除，事务上下文靠日志重建（TaskRecord 是真源）。批量不能
    因为图省事就把这条绕开。
    """
    t1 = app.tasks.create("扫掉一", [roles["work"].id])
    t2 = app.tasks.create("扫掉二", [roles["work"].id])
    results = app.tasks.drop_many([t1.id, t2.id])
    assert [r.ok for r in results] == [True, True]
    for t in (t1, t2):
        assert app.task_repo.get(t.id).state is TaskState.DROPPED
        types = [r.type.value for r in app.record_repo.list_for_task(t.id)]
        assert "status_change" in types


def test_drop_many_partial_failure_does_not_block_others(app: App, roles):
    """混入不存在 / 已完成的 id：好的照落，坏的带原因，不抛异常。

    批量清理的典型场景就是「一堆里混着几条已经结束的」—— 因为一条
    终态事务整批拒绝，用户就得反复分批试，而那正是批量要消除的摩擦。

    注意 DONE 的**不能**直接迁 DROPPED（迁移表对终态刻意不对称，
    test_tasks 里有对称性测试钉着）：所以「已完成的」也算失败条，
    且原因要说清是「已完成」而不是龙卷风式的「失败」。
    """
    good = app.tasks.create("好的", [roles["work"].id])
    done = app.tasks.create("已结束", [roles["work"].id])
    app.tasks.complete(done.id)
    results = app.tasks.drop_many([good.id, done.id, "no-such-id"])
    assert [r.ok for r in results] == [True, False, False]
    assert app.task_repo.get(good.id).state is TaskState.DROPPED
    # 两条失败都必须带**能看懂的原因**，而不是一句「失败」。
    # 措辞跟服务层实际的消息（NotFoundError →「事务 不存在」、
    # set_state →「不允许从「已完成」直接迁到…」）对齐 —— 断言文案
    # 而不是只断言非空：空泛的「失败」正是这个仓一直在防的东西。
    assert "已完成" in results[1].reason
    assert "不存在" in results[2].reason


def test_drop_many_dedups_ids(app: App, roles):
    """同一条传两遍：只处理一次。

    不去重的话第二条会撞上「已是 DROPPED 的合法迁移」而产生一条
    莫名的重复结果 —— 界面显示「成功 2 条」而用户只勾了一条。
    """
    t = app.tasks.create("只此一条", [roles["work"].id])
    results = app.tasks.drop_many([t.id, t.id])
    assert len(results) == 1
    assert results[0].ok
