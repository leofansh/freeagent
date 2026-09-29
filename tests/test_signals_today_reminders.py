"""弱排序信号、今天视图顺延、提醒引擎。"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from freeagent.app import App
from freeagent.domain import (
    TaskKind,
    TaskState,
    WaitingKind,
    WaitingOn,
)
from freeagent.services.clock import FrozenClock
from freeagent.services.sorting import (
    DEADLINE_RISK_HOURS,
    RESUME_STALE_DAYS,
    WAITING_TOO_LONG_DAYS,
    EnergyWindows,
    compute_signals,
    score_and_sort,
    sort_tasks,
)

NOW = datetime(2026, 9, 26, 9, 0)
TODAY = date(2026, 9, 26)


def _codes(task, now=NOW, today=TODAY, **kw):
    return [s.code.value for s in compute_signals(task, now, today, **kw)]


# --------------------------------------------------------------------- signals
def test_overdue_wait_signal(app: App, roles):
    t = app.tasks.create(
        "等客户", [roles["work"].id], kind=TaskKind.WAIT,
        waiting_on=WaitingOn(
            WaitingKind.PERSON, "客户", datetime(2026, 9, 1),
            follow_up_at=datetime(2026, 9, 25),
        ),
    )
    sigs = compute_signals(t, NOW, TODAY)
    codes = [s.code.value for s in sigs]
    assert "overdue_wait" in codes
    assert "waiting_too_long" in codes
    reason = next(s.reason for s in sigs if s.code.value == "overdue_wait")
    assert "客户" in reason


def test_wait_not_yet_due_has_no_overdue(app: App, roles):
    t = app.tasks.create(
        "等客户", [roles["work"].id], kind=TaskKind.WAIT,
        waiting_on=WaitingOn(
            WaitingKind.PERSON, "客户", NOW, follow_up_at=datetime(2026, 9, 30)
        ),
    )
    assert "overdue_wait" not in _codes(t)


def test_deadline_risk_signal(app: App, roles):
    t = app.tasks.create("交材料", [roles["work"].id])
    t = app.tasks.set_due_time(t.id, NOW + timedelta(hours=DEADLINE_RISK_HOURS - 1))
    assert "deadline_risk" in _codes(app.task_repo.get(t.id))
    far = app.tasks.set_due_time(
        app.tasks.create("远期", [roles["work"].id]).id,
        NOW + timedelta(hours=DEADLINE_RISK_HOURS + 5),
    )
    assert "deadline_risk" not in _codes(far)


def test_deadline_risk_includes_overdue(app: App, roles):
    t = app.tasks.create("过期了", [roles["work"].id])
    t = app.tasks.set_due_time(t.id, NOW - timedelta(days=2))
    assert "deadline_risk" in _codes(app.task_repo.get(t.id))


def test_depended_on_signal(app: App, roles):
    blocker = app.tasks.create("先做这个", [roles["work"].id])
    waiter = app.tasks.create("等它", [roles["work"].id], blocked_by=[blocker.id])
    assert "depended_on" in _codes(app.task_repo.get(blocker.id), dependents=1)
    assert "depended_on" not in _codes(app.task_repo.get(waiter.id))


def test_depended_on_uses_repo_count(app: App, roles):
    blocker = app.tasks.create("先做这个", [roles["work"].id])
    app.tasks.create("等它 A", [roles["work"].id], blocked_by=[blocker.id])
    app.tasks.create("等它 B", [roles["work"].id], blocked_by=[blocker.id])
    scored = score_and_sort(
        [app.task_repo.get(blocker.id)], NOW, TODAY,
        dependents_of=app.task_repo.count_dependents,
    )
    assert "depended_on" in [s.code.value for s in scored[0].signals]


def test_resume_stale_signal(app: App, roles):
    t = app.tasks.create("做一半", [roles["work"].id])
    t = app.tasks.start(t.id)
    fresh = app.task_repo.get(t.id)
    assert "resume_stale" not in _codes(fresh)
    import dataclasses

    stale = dataclasses.replace(
        fresh, last_resumed_at=NOW - timedelta(days=RESUME_STALE_DAYS + 1)
    )
    assert "resume_stale" in _codes(stale)


def test_resume_stale_falls_back_to_updated_at(app: App, roles, clock):
    """刚 start 完就再没碰过 —— 没有 last_resumed_at，但同样算中断。"""
    t = app.tasks.create("做一半", [roles["work"].id])
    app.tasks.start(t.id)
    assert app.task_repo.get(t.id).last_resumed_at is None
    clock.advance(days=RESUME_STALE_DAYS + 1)
    codes = _codes(app.task_repo.get(t.id), now=clock.now())
    assert "resume_stale" in codes, codes


def test_progress_note_rebuild_does_not_count_as_activity(app: App, roles, clock):
    """写派生缓存不是用户活动 —— 否则 RESUME_STALE 永远不会触发。"""
    t = app.tasks.create("做一半", [roles["work"].id])
    app.tasks.start(t.id)
    clock.advance(days=RESUME_STALE_DAYS + 1)
    before = app.task_repo.get(t.id).updated_at
    app.tasks.rebuild_progress_note(t.id)
    assert app.task_repo.get(t.id).updated_at == before, "重建进度摘要不得更新时间戳"
    assert "resume_stale" in _codes(app.task_repo.get(t.id), now=clock.now())


def test_scheduled_today_and_inbox(app: App, roles):
    t = app.tasks.create("今天做", [roles["work"].id])
    t = app.tasks.schedule(t.id, TODAY)
    codes = _codes(app.task_repo.get(t.id))
    assert "scheduled_today" in codes and "inbox_unsorted" in codes


def test_energy_fit_requires_windows(app: App, roles):
    t = app.tasks.create("晚上做", [roles["work"].id])
    t = app.tasks.set_reminder_time(t.id, datetime(2026, 9, 26, 20, 30))
    task = app.task_repo.get(t.id)
    assert "energy_fit" not in _codes(task)
    assert "energy_fit" in _codes(task, energy_windows=EnergyWindows())


def test_energy_fit_windows_boundaries():
    w = EnergyWindows()
    assert w.label_for(datetime(2026, 1, 1, 5)) == "早晨"
    assert w.label_for(datetime(2026, 1, 1, 11, 59)) == "早晨"
    assert w.label_for(datetime(2026, 1, 1, 12)) == "下午"
    assert w.label_for(datetime(2026, 1, 1, 18)) == "晚上"
    assert w.label_for(datetime(2026, 1, 1, 3)) is None


def test_closed_tasks_have_no_signals(app: App, roles):
    t = app.tasks.create("完了", [roles["work"].id])
    t = app.tasks.schedule(t.id, TODAY)
    t = app.tasks.complete(t.id)
    assert _codes(app.task_repo.get(t.id)) == []


def test_sort_is_weight_desc_then_fifo(app: App, roles):
    import dataclasses

    now = NOW
    today = TODAY
    a = app.tasks.create("A", [roles["work"].id])
    b = app.tasks.create("B", [roles["work"].id])
    # B 权重更高
    app.tasks.set_due_time(b.id, now + timedelta(hours=2))
    scored = score_and_sort(
        [app.task_repo.get(a.id), app.task_repo.get(b.id)], now, today
    )
    assert [s.task.id for s in scored] == [b.id, a.id]
    assert scored[0].total_weight > scored[1].total_weight


def test_sort_tie_breaks_by_creation_order(app: App, roles, clock):
    """同权重按「先进先出」，且不依赖 id 格式（id 必须保持随机以支持前缀匹配）。"""
    a = app.tasks.create("A", [roles["work"].id])
    b = app.tasks.create("B", [roles["work"].id])
    c = app.tasks.create("C", [roles["work"].id])
    scored = score_and_sort(
        app.task_repo.list_all(), NOW, TODAY
    )
    assert [s.task.id for s in scored] == [a.id, b.id, c.id]
    # 即使打乱输入，稳定排序也按原顺序处理同分项
    shuffled = list(reversed(scored))
    assert [s.task.id for s in sort_tasks(shuffled)] == [c.id, b.id, a.id]


# --------------------------------------------------------------------- rollover
def test_rollover_moves_open_tasks_forward(app: App, roles, clock):
    t = app.tasks.create("上周的", [roles["work"].id])
    t = app.tasks.schedule(t.id, date(2026, 9, 20))
    clock.set(datetime(2026, 9, 26, 9, 0))
    report = app.today.rollover()
    assert report.count == 1
    assert report.items[0].from_day == date(2026, 9, 20)
    assert app.task_repo.get(t.id).scheduled_for == date(2026, 9, 26)
    assert "顺延" in report.summary()


def test_rollover_is_idempotent(app: App, roles, clock):
    t = app.tasks.create("上周的", [roles["work"].id])
    app.tasks.schedule(t.id, date(2026, 9, 20))
    assert app.today.rollover().count == 1
    assert app.today.rollover().count == 0
    assert app.today.view().rollover.count == 0
    types = [r.type.value for r in app.record_repo.list_for_task(t.id)]
    assert types.count("rollover") == 1


def test_rollover_skips_closed_tasks(app: App, roles):
    done = app.tasks.create("做完了", [roles["work"].id])
    app.tasks.schedule(done.id, date(2026, 9, 20))
    app.tasks.complete(done.id)
    dropped = app.tasks.create("放弃了", [roles["work"].id])
    app.tasks.schedule(dropped.id, date(2026, 9, 20))
    app.tasks.drop(dropped.id)
    assert app.today.rollover().count == 0


def test_rollover_appends_record(app: App, roles):
    t = app.tasks.create("上周的", [roles["work"].id])
    app.tasks.schedule(t.id, date(2026, 9, 20))
    app.today.rollover()
    contents = [r.content for r in app.record_repo.list_for_task(t.id)]
    assert any("顺延" in c for c in contents)


def test_today_view_marks_rolled_over_but_ranks_globally(app: App, roles):
    late = app.tasks.create("顺延的", [roles["work"].id])
    app.tasks.schedule(late.id, date(2026, 9, 20))
    normal = app.tasks.create("今天的", [roles["work"].id])
    app.tasks.schedule(normal.id, date(2026, 9, 26))
    view = app.today.view()
    assert {s.task.id for s in view.items} == {late.id, normal.id}
    assert view.rolled_over_ids == {late.id}
    # 全局有序，而不是分区各自有序
    weights = [s.total_weight for s in view.items]
    assert weights == sorted(weights, reverse=True)


def test_today_view_excludes_future(app: App, roles):
    t = app.tasks.create("下周的", [roles["work"].id])
    app.tasks.schedule(t.id, date(2026, 10, 1))
    view = app.today.view()
    assert all(s.task.id != t.id for s in view.items)


def test_empty_rollover_summary_is_none(app: App, roles):
    assert app.today.rollover().summary() is None


# --------------------------------------------------------------------- reminders
def test_reminder_not_yet_due(app: App, roles, clock):
    t = app.tasks.create("修窗户", [roles["work"].id], kind=TaskKind.REMINDER,
                         reminder_time=datetime(2026, 9, 26, 15, 0))
    assert app.reminders.check().is_empty


def test_reminder_fires_when_due(app: App, roles, clock):
    t = app.tasks.create("修窗户", [roles["work"].id], kind=TaskKind.REMINDER,
                         reminder_time=datetime(2026, 9, 26, 15, 0))
    clock.set(datetime(2026, 9, 26, 15, 5))
    digest = app.reminders.check()
    assert digest.count == 1
    assert digest.missed == () and len(digest.fired) == 1
    assert "修窗户" in digest.text()


def test_reminder_is_not_refired(app: App, roles, clock):
    app.tasks.create("修窗户", [roles["work"].id], kind=TaskKind.REMINDER,
                     reminder_time=datetime(2026, 9, 26, 15, 0))
    clock.set(datetime(2026, 9, 26, 15, 5))
    assert app.reminders.check().count == 1
    assert app.reminders.check().count == 0


def test_missed_reminder_is_labelled(app: App, roles, clock):
    app.tasks.create("很久以前", [roles["work"].id], kind=TaskKind.REMINDER,
                     reminder_time=datetime(2026, 9, 25, 10, 0))
    clock.set(datetime(2026, 9, 26, 20, 0))
    digest = app.reminders.check()
    assert len(digest.missed) == 1 and digest.fired == ()
    assert "已错过" in digest.text()


def test_reminder_digest_is_merged_single_message(app: App, roles, clock):
    for i in range(3):
        app.tasks.create(f"提醒{i}", [roles["work"].id], kind=TaskKind.REMINDER,
                         reminder_time=datetime(2026, 9, 26, 15, i))
    clock.set(datetime(2026, 9, 26, 15, 30))
    digest = app.reminders.check()
    assert digest.count == 3
    text = digest.text()
    # 单条摘要，一行说完，三条都在里面
    assert text is not None and "\n" not in text
    assert "3 条" in text
    for i in range(3):
        assert f"提醒{i}" in text


def test_reminder_digest_merges_fired_and_missed(app: App, roles, clock):
    app.tasks.create("刚到点", [roles["work"].id], kind=TaskKind.REMINDER,
                     reminder_time=datetime(2026, 9, 26, 15, 0))
    app.tasks.create("早错过", [roles["work"].id], kind=TaskKind.REMINDER,
                     reminder_time=datetime(2026, 9, 25, 10, 0))
    clock.set(datetime(2026, 9, 26, 20, 0))
    text = app.reminders.check().text()
    assert text is not None and "\n" not in text
    assert "到点" in text and "已错过" in text
    assert "；" in text, "两组之间应有分隔"


def test_closed_task_does_not_remind(app: App, roles, clock):
    t = app.tasks.create("取消的提醒", [roles["work"].id], kind=TaskKind.REMINDER,
                         reminder_time=datetime(2026, 9, 26, 15, 0))
    app.tasks.drop(t.id)
    clock.set(datetime(2026, 9, 26, 15, 5))
    assert app.reminders.check().is_empty
