"""周回顾（设计文档 6.5）的服务层与渲染。

## 为什么这些断言值得写

``/week`` 的价值全在「**四个分区互不重叠且合起来覆盖该看见的全部**」
这条上。分区漏一条 = 静默（事务从视图里消失），重叠一条 = 同一件事
报两次（用户会以为有两件）。所以断言按「覆盖与互斥」写，而不是按
「输出里有没有某个词」写。
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from freeagent.app import build_app
from freeagent.cli.render import render_week
from freeagent.domain import RecordType, TaskKind
from freeagent.domain.models import WaitingOn
from freeagent.domain.enums import WaitingKind
from freeagent.services.clock import FrozenClock
from freeagent.services.sorting import EnergyWindows
from freeagent.services.week import WeekService

#: 2026-10-21 是周三。刻意选周三而不是周一：周一起止的边界错误在
#: 「今天正好是周一」时会被掩盖（上周的结束时间 = 本周开始时间）。
WEDNESDAY = datetime(2026, 10, 21, 10, 0)
MONDAY = date(2026, 10, 19)


@pytest.fixture
def env(tmp_path):
    """一只钉在周三的 App、它自己的时钟、一条能挂事务的角色。

    **不复用 conftest 的 `clock`**：那个固定在 2026-09-26（周六），
    而本文件要断言「本周 = 10-19 ~ 10-25」这种具体边界 —— 用别人的
    时钟就得改它，而改夹具时钟会连坐所有请求了 `clock` 的用例。
    """
    frozen = FrozenClock(WEDNESDAY)
    application = build_app(
        tmp_path / "agent.db", clock=frozen, energy_windows=EnergyWindows()
    )
    # 事务至少属于一个角色（4.3 不变式），所以先备一条脉络给用例挂。
    role = application.roles.create("工作")
    try:
        yield application, frozen, role
    finally:
        application.close()


def _week(env) -> WeekService:
    app, clock, _role = env
    return WeekService(app.task_repo, app.record_repo, clock, today=app.today)


def _view(env):
    return _week(env).view()


def _new(app, role, title, kind=TaskKind.ACTION):
    return app.tasks.create(title=title, role_ids=(role.id,), kind=kind)


def _waiting(app, role, title, who):
    """建一条等候类事务。

    刻意**不** ``create(kind=WAIT)`` —— 4.3 不变式要求未结束的等候类
    必须同时带 ``waiting_on`` 且处于 BLOCKED，所以只能先建成普通事务，
    再 ``set_kind`` 转过去（这也是 CLI ``/kind <id> wait 等谁`` 的路径）。
    """
    task = _new(app, role, title)
    return app.tasks.set_kind(
        task.id, TaskKind.WAIT,
        waiting_on=WaitingOn(
            kind=WaitingKind.PERSON, who_or_what=who, since=app.clock.now()
        ),
    )


class TestWeekView:
    def test_empty_week_is_honest(self, env):
        view = _view(env)
        assert view.is_empty
        text = render_week(view, {})
        assert "本周还没有可回顾的事实" in text

    def test_closed_this_week_is_counted(self, env):
        app, _clock, role = env
        task = _new(app, role, "交周报")
        app.tasks.complete(task.id)
        view = _view(env)
        assert [t.id for t in view.closed] == [task.id]
        assert [t.id for t in view.completed] == [task.id]
        assert view.dropped == ()

    def test_dropped_this_week_counts_as_closed(self, env):
        app, _clock, role = env
        task = _new(app, role, "算了的那件")
        app.tasks.drop(task.id)
        view = _view(env)
        assert [t.id for t in view.dropped] == [task.id]
        assert view.completed == ()

    def test_last_week_is_not_this_week(self, env):
        """上周结束的不进本周回顾 —— 边界是「本周一零点」，不是「七天内」。"""
        app, clock, role = env
        task = _new(app, role, "上周就做完的")
        app.tasks.complete(task.id)          # 周三完成
        clock.set(datetime(2026, 10, 26, 10, 0))   # 下周一：上周已结束
        view = _view(env)
        assert view.closed == ()
        assert "上周就做完的" not in render_week(view, {})

    def test_slipped_task_counts_rollovers(self, env):
        app, _clock, role = env
        task = _new(app, role, "写方案")
        app.tasks.schedule(task.id, MONDAY)
        # 周三访问今天视图 = 周一的事顺延过来一次。顺延的实现只有一份，
        # 住在 TodayService 里。
        app.today.view()
        view = _view(env)
        assert [(i.task.id, i.times) for i in view.slips] == [(task.id, 1)]
        assert view.slipped_times == 1

    def test_rollover_count_ignores_other_record_types(self, env):
        """顺延次数数的是 ``ROLLOVER`` 记录的条数，不是正文里的数字。"""
        app, clock, role = env
        task = _new(app, role, "写方案")
        app.tasks.schedule(task.id, MONDAY)
        app.today.view()
        app.record_repo.append(
            task.id, RecordType.NOTE, "顺延 99 次也不该被数成 99", clock.now()
        )
        assert _view(env).slipped_times == 1

    def test_closed_task_is_not_a_slip(self, env):
        """顺延过又在本周结束的，只算「结束」，不该再算「卡住」。"""
        app, _clock, role = env
        task = _new(app, role, "拖了才做完")
        app.tasks.schedule(task.id, MONDAY)
        app.today.view()             # 顺延一次
        app.tasks.complete(task.id)
        view = _view(env)
        assert [t.id for t in view.closed] == [task.id]
        assert view.slips == ()

    def test_next_week_promise(self, env):
        app, _clock, role = env
        task = _new(app, role, "下周一交稿")
        app.tasks.schedule(task.id, date(2026, 10, 26))
        assert [t.id for t in _view(env).next_week] == [task.id]

    def test_next_week_excludes_this_week_and_fortnight(self, env):
        app, _clock, role = env
        this_week = _new(app, role, "本周五")
        app.tasks.schedule(this_week.id, date(2026, 10, 23))
        far = _new(app, role, "下下周")
        app.tasks.schedule(far.id, date(2026, 11, 2))
        view = _view(env)
        assert view.next_week == ()
        # 「本周五」也不该掉进任何一个分区 —— 它属于「今天/本周」那张视图。
        seen = {t.id for t in (*view.closed, *view.unscheduled, *view.waiting)}
        assert this_week.id not in seen
        assert far.id not in seen

    def test_unscheduled_open_task_is_not_silent(self, env):
        app, _clock, role = env
        task = _new(app, role, "忘了排期")
        assert [t.id for t in _view(env).unscheduled] == [task.id]

    def test_waiting_is_listed(self, env):
        app, _clock, role = env
        task = _waiting(app, role, "等客户回话", "客户法务")
        assert [t.id for t in _view(env).waiting] == [task.id]

    def test_view_is_read_only(self, env):
        """回顾只读：看完不留痕、不改状态。"""
        app, _clock, role = env
        task = _new(app, role, "写方案")
        app.tasks.schedule(task.id, MONDAY)
        app.today.view()
        before_state = app.task_repo.get(task.id).state
        before_records = len(app.record_repo.list_for_task(task.id))
        _view(env)
        assert app.task_repo.get(task.id).state == before_state
        assert len(app.record_repo.list_for_task(task.id)) == before_records

    def test_no_rollover_service_still_works(self, env):
        """不给 TodayService 也能算 —— 只是不做收口。"""
        app, clock, _role = env
        view = WeekService(app.task_repo, app.record_repo, clock).view()
        assert view.start == MONDAY
        assert view.end == date(2026, 10, 26)


class TestRenderWeek:
    def test_disclaimer_and_no_advice(self, env):
        app, _clock, role = env
        task = _new(app, role, "交周报")
        app.tasks.complete(task.id)
        text = render_week(_view(env), {})
        assert "启发式提示，不是评分" in text
        assert "事实" in text and "由你决定" in text
        for word in ("建议你", "你应该", "最好"):
            assert word not in text

    def test_sections_show_each_fact(self, env):
        app, _clock, role = env
        done = _new(app, role, "交周报")
        app.tasks.complete(done.id)
        slipped = _new(app, role, "写方案")
        app.tasks.schedule(slipped.id, MONDAY)
        app.today.view()
        promised = _new(app, role, "下周交稿")
        app.tasks.schedule(promised.id, date(2026, 10, 26))
        _new(app, role, "忘了排期")
        awaited = _waiting(app, role, "等客户回话", "客户法务")

        text = render_week(_view(env), {})
        assert "本周回顾 · 2026-10-19 ~ 2026-10-25" in text
        assert "本周结束 1 件（完成 1 / 放弃 0）" in text
        assert "交周报" in text
        assert "卡在哪：本周顺延 1 次" in text
        assert "写方案 —— 顺延 1 次" in text
        assert "下周承诺 1 件" in text
        assert "10-26 周一" in text and "下周交稿" in text
        assert "没排期 2 件" in text and "忘了排期" in text
        assert "在等 1 件" in text and "客户法务" in text

    def test_role_label_shown_when_known(self, env):
        app, _clock, role = env
        task = _new(app, role, "交周报")
        app.tasks.complete(task.id)
        text = render_week(_view(env), {role.id: "工作"})
        assert "【工作】交周报" in text

    def test_empty_sections_say_so(self, env):
        """每个分区都要**明说**没有，而不是整段消失 —— 消失就是静默。"""
        app, _clock, role = env
        _new(app, role, "悬空的一件")
        text = render_week(_view(env), {})
        assert "（没有结束的事务）" in text
        assert "卡在哪：本周没有顺延记录。" in text
        assert "下周承诺：下周还没有排期。" in text
        # 分区在且说了实话 —— 有悬空事务，所以那行不该出现。
        assert "没排期：没有悬空的事务。" not in text
        assert "没排期 1 件" in text
