"""连续性个人模型（设计文档 11.17 / 20.1.1）的服务层与渲染。

与 test_week.py 同一条纪律：断言按「计数正确 / 覆盖与互斥」写，而不是按
「输出里有没有某个词」写。规律算错（少算一条顺延、把未来的完成算进窗口）
比多打一行更隐蔽——所以重点验计数与窗口边界。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from freeagent.app import build_app
from freeagent.cli.render import render_model
from freeagent.domain import RecordType, Task, TaskKind
from freeagent.domain.enums import TaskState, WaitingKind
from freeagent.domain.models import TaskRecord, WaitingOn
from freeagent.services.clock import FrozenClock
from freeagent.services.personal_model import (
    PersonalModelService,
    PersonalModelView,
    build_personal_model,
)
from freeagent.services.sorting import EnergyWindows

#: 2026-10-21 是周三（weekday()==2）。刻意选一个有具体小时的时间，
#: 以便断言「完成时段分布」的小时维度。
WEDNESDAY = datetime(2026, 10, 21, 15, 0)
MONDAY = date(2026, 10, 19)


@pytest.fixture
def env(tmp_path):
    """一只钉在周三的 App、它自己的时钟、一条能挂事务的角色。"""
    frozen = FrozenClock(WEDNESDAY)
    application = build_app(
        tmp_path / "agent.db", clock=frozen, energy_windows=EnergyWindows()
    )
    role = application.roles.create("工作")
    try:
        yield application, frozen, role
    finally:
        application.close()


def _model(env) -> PersonalModelService:
    app, _clock, _role = env
    return app.personal_model


def _view(env) -> PersonalModelView:
    return _model(env).view()


def _new(app, role, title, kind=TaskKind.ACTION):
    return app.tasks.create(title=title, role_ids=(role.id,), kind=kind)


def _waiting(app, role, title, who, since: datetime):
    task = _new(app, role, title)
    return app.tasks.set_kind(
        task.id, TaskKind.WAIT,
        waiting_on=WaitingOn(kind=WaitingKind.PERSON, who_or_what=who, since=since),
    )


def _task(tid, title, role_ids, *, state=TaskState.ACTIVE, completed_at=None, waiting_on=None):
    """纯函数测试用的极简 Task（不碰库）。"""
    return Task(
        id=tid, title=title, role_ids=role_ids, state=state, kind=TaskKind.ACTION,
        entered_at=WEDNESDAY, created_at=WEDNESDAY, updated_at=WEDNESDAY,
        completed_at=completed_at, waiting_on=waiting_on,
    )


# --------------------------------------------------------------------- 纯函数
class TestBuildPersonalModel:
    def test_counts_completion_by_hour_and_dow(self):
        t = _task("a" * 8, "交周报", ("r1",), state=TaskState.DONE,
                  completed_at=datetime(2026, 10, 21, 14, 30))
        view = build_personal_model([t], [], {"r1": "工作"}, WEDNESDAY)
        assert view.done_in_window == 1
        assert (14, 1) in view.completion_by_hour
        assert (2, 1) in view.completion_by_dow          # 周三 = weekday 2

    def test_completion_outside_window_is_ignored(self):
        # 完成时间早于 90 天窗口 → 不计入。
        old = WEDNESDAY - timedelta(days=200)
        t = _task("b" * 8, "去年的事", ("r1",), state=TaskState.DONE,
                  completed_at=old)
        view = build_personal_model([t], [], {"r1": "工作"}, WEDNESDAY)
        assert view.done_in_window == 0
        assert view.completion_by_hour == ()
        assert view.completion_by_dow == ()

    def test_counts_rollovers_and_repeated_threshold(self):
        t = _task("c" * 8, "写方案", ("r1",))
        records = [
            TaskRecord(id="r1", task_id=t.id, ts=datetime(2026, 10, 20, 9, 0),
                       type=RecordType.ROLLOVER, content="x"),
            TaskRecord(id="r2", task_id=t.id, ts=datetime(2026, 10, 21, 9, 0),
                       type=RecordType.ROLLOVER, content="y"),
        ]
        view = build_personal_model([t], records, {"r1": "工作"}, WEDNESDAY)
        assert view.rollover_total == 2
        # 顺延 ≥2 次才进「反复顺延」清单。
        assert view.tasks_rolled_over == ((t.id[:8], "写方案", 2),)

    def test_single_rollover_not_flagged_as_repeated(self):
        t = _task("d" * 8, "写方案", ("r1",))
        records = [
            TaskRecord(id="r1", task_id=t.id, ts=datetime(2026, 10, 21, 9, 0),
                       type=RecordType.ROLLOVER, content="x"),
        ]
        view = build_personal_model([t], records, {"r1": "工作"}, WEDNESDAY)
        assert view.rollover_total == 1
        assert view.tasks_rolled_over == ()

    def test_other_record_types_do_not_count_as_rollover(self):
        t = _task("e" * 8, "写方案", ("r1",))
        records = [
            TaskRecord(id="r1", task_id=t.id, ts=datetime(2026, 10, 21, 9, 0),
                       type=RecordType.NOTE, content="顺延了一次"),
            TaskRecord(id="r2", task_id=t.id, ts=datetime(2026, 10, 21, 10, 0),
                       type=RecordType.ROLLOVER, content="x"),
        ]
        view = build_personal_model([t], records, {"r1": "工作"}, WEDNESDAY)
        assert view.rollover_total == 1

    def test_stuck_waiting_threshold(self):
        since = WEDNESDAY - timedelta(days=20)
        w = WaitingOn(kind=WaitingKind.PERSON, who_or_what="张总", since=since)
        t = _task("f" * 8, "等合同", ("r1",), state=TaskState.BLOCKED, waiting_on=w)
        view = build_personal_model([t], [], {"r1": "工作"}, WEDNESDAY)
        assert view.stuck_waiting == ((t.id[:8], "等合同", 20),)

    def test_fresh_waiting_not_flagged(self):
        since = WEDNESDAY - timedelta(days=2)
        w = WaitingOn(kind=WaitingKind.PERSON, who_or_what="张总", since=since)
        t = _task("g" * 8, "等合同", ("r1",), state=TaskState.BLOCKED, waiting_on=w)
        view = build_personal_model([t], [], {"r1": "工作"}, WEDNESDAY)
        assert view.stuck_waiting == ()

    def test_role_activity_counts_and_uses_names(self):
        t1 = _task("h" * 8, "a", ("r1",))
        t2 = _task("i" * 8, "b", ("r1", "r2"))
        view = build_personal_model(
            [t1, t2], [], {"r1": "工作", "r2": "生活"}, WEDNESDAY
        )
        assert ("工作", 2) in view.role_activity
        assert ("生活", 1) in view.role_activity

    def test_role_name_fallback_to_id_prefix(self):
        t = _task("j" * 8, "a", ("unknown-role",))
        view = build_personal_model([t], [], {}, WEDNESDAY)
        assert view.role_activity == (("unknown-", 1),)


# --------------------------------------------------------------------- 服务/集成
class TestPersonalModelService:
    def test_empty_is_honest(self, env):
        view = _view(env)
        assert view.is_empty
        text = render_model(view, {})
        assert "还没有可归纳的记录" in text

    def test_completion_via_service(self, env):
        app, clock, role = env
        clock.set(datetime(2026, 10, 21, 14, 30))
        task = _new(app, role, "交周报")
        app.tasks.complete(task.id)
        view = _view(env)
        assert view.done_in_window == 1
        assert (14, 1) in view.completion_by_hour
        assert (2, 1) in view.completion_by_dow

    def test_rollover_via_records(self, env):
        app, _clock, role = env
        task = _new(app, role, "写方案")
        app.record_repo.append(task.id, RecordType.ROLLOVER, "顺延", datetime(2026, 10, 20, 9, 0))
        app.record_repo.append(task.id, RecordType.ROLLOVER, "又顺延", datetime(2026, 10, 21, 9, 0))
        view = _view(env)
        assert view.rollover_total == 2
        assert view.tasks_rolled_over == ((task.id[:8], "写方案", 2),)

    def test_stuck_waiting_via_service(self, env):
        app, clock, role = env
        since = clock.now() - timedelta(days=15)
        _waiting(app, role, "等合同", "张总", since)
        view = _view(env)
        assert len(view.stuck_waiting) == 1
        assert view.stuck_waiting[0][2] == 15

    def test_role_activity_resolves_name(self, env):
        app, _clock, role = env
        _new(app, role, "事一")
        _new(app, role, "事二")
        view = _view(env)
        assert ("工作", 2) in view.role_activity

    def test_render_lists_all_sections(self, env):
        app, clock, role = env
        clock.set(datetime(2026, 10, 21, 14, 30))
        task = _new(app, role, "交周报")
        app.tasks.complete(task.id)
        app.record_repo.append(task.id, RecordType.ROLLOVER, "顺延", datetime(2026, 10, 20, 9, 0))
        app.record_repo.append(task.id, RecordType.ROLLOVER, "又顺延", datetime(2026, 10, 21, 9, 0))
        _waiting(app, role, "等合同", "张总", clock.now() - timedelta(days=10))
        text = render_model(_view(env), {})
        assert "连续性个人模型" in text
        assert "完成" in text
        assert "顺延" in text
        assert "卡住的等候" in text
        assert "角色活跃度" in text
        # 不给建议：没有「建议你…」这种句子。
        assert "建议你" not in text


def test_module_is_importable():
    assert PersonalModelService is not None
    assert build_personal_model is not None
