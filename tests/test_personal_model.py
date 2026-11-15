"""连续性个人模型（设计文档 11.17 / 20.1.1）的服务层与渲染。

与 test_week.py 同一条纪律：断言按「计数正确 / 覆盖与互斥」写，而不是按
「输出里有没有某个词」写。规律算错（少算一条顺延、把未来的完成算进窗口）
比多打一行更隐蔽——所以重点验计数与窗口边界。
"""

from __future__ import annotations

import itertools
from datetime import date, datetime, timedelta

import pytest

from freeagent.app import build_app
from freeagent.cli.render import render_model
from freeagent.domain import Contact, Promise, RecordType, Task, TaskKind
from freeagent.domain.enums import ContactKind, PromiseStatus, TaskState, WaitingKind
from freeagent.domain.models import TaskRecord, WaitingOn
from freeagent.services.clock import FrozenClock
from freeagent.services.memory import MemoryEntry
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
        app.memory.add("我最近在学赛艇", kind="偏好")
        text = render_model(_view(env), {})
        assert "连续性个人模型" in text
        assert "完成" in text
        assert "顺延" in text
        assert "卡住的等候" in text
        assert "角色活跃度" in text
        # V1.59 两分面也出现。
        assert "关系账" in text
        assert "承诺与目标" in text
        # V1.69 滋养兴趣分面：记下的偏好原样带出。
        assert "滋养兴趣" in text
        assert "我最近在学赛艇" in text
        # 收尾是「事实」而非「规律」，且不给建议。
        assert "事实" in text
        assert "建议你" not in text


def test_module_is_importable():
    assert PersonalModelService is not None
    assert build_personal_model is not None


# --------------------------------------------------- 关系账 + 承诺与目标（V1.59）
def _contact(cid: str, name: str) -> Contact:
    return Contact(
        id=cid, name=name, kind=ContactKind.PERSON,
        created_at=WEDNESDAY, updated_at=WEDNESDAY,
    )


def _promise(pid: str, cid: str, statement: str, *, promised_for=None) -> Promise:
    return Promise(
        id=pid, task_id="t-" + pid, contact_id=cid, statement=statement,
        promised_for=promised_for, status=PromiseStatus.OPEN,
        created_at=WEDNESDAY, updated_at=WEDNESDAY,
    )


def _mem(mid: str, kind: str, content: str) -> MemoryEntry:
    return MemoryEntry(
        id=mid, scope="global", kind=kind, content=content,
        created_at=WEDNESDAY, updated_at=WEDNESDAY,
    )


class TestRelationshipsAndCommitments:
    """V1.59 新增两面：关系账 + 承诺与目标（纯函数，直接喂领域对象）。"""

    def test_relationship_ledger_only_open_accounts(self):
        contacts = [_contact("c1", "张总"), _contact("c2", "李工"), _contact("c3", "王总")]
        promises = [_promise("p1", "c1", "给张总报价单"), _promise("p2", "c2", "给李工方案")]
        # c1 还被一条 open 等候事务挂着；c3 没有任何未结清账 → 不出现。
        wait = _task(
            "w" * 8, "等张总回合同", ("r1",), state=TaskState.BLOCKED,
            waiting_on=WaitingOn(kind=WaitingKind.PERSON, who_or_what="张总",
                                contact_id="c1", since=WEDNESDAY),
        )
        view = build_personal_model(
            [wait], [], {"r1": "工作"}, WEDNESDAY,
            contacts=contacts, open_promises=promises,
        )
        rel = {(r[0], r[1], r[2], r[3]) for r in view.relationships}
        assert ("c1", "张总", 1, 1) in rel            # 1 承诺 + 1 等候
        assert ("c2", "李工", 1, 0) in rel            # 仅承诺
        assert all(r[1] != "王总" for r in view.relationships)
        # 按（承诺 + 等候）账重降序：c1(2) 在 c2(1) 前。
        assert view.relationships[0][0] == "c1"

    def test_commitments_include_open_promises_with_overdue_flag(self):
        contacts = [_contact("c1", "张总")]
        overdue = _promise("p1", "c1", "周三前报价单",
                          promised_for=WEDNESDAY - timedelta(days=2))
        future = _promise("p2", "c1", "月底交付",
                         promised_for=WEDNESDAY + timedelta(days=3))
        view = build_personal_model(
            [], [], {}, WEDNESDAY,
            contacts=contacts, open_promises=[overdue, future],
        )
        kinds = {c[0] for c in view.commitments}
        assert "承诺" in kinds
        # 逾期承诺排最前且只标「已逾期」、不写「违约」。
        assert view.commitments[0] == ("承诺", "周三前报价单", True)
        assert ("承诺", "月底交付", False) in view.commitments

    def test_commitments_include_goal_and_convention_memory_not_preference(self):
        mems = [
            _mem("m1", "约定", "回复先给结论"),
            _mem("m2", "目标", "今年把英语练到能开会"),
            _mem("m3", "偏好", "不喝冰"),          # 不应进入「承诺与目标」
        ]
        view = build_personal_model([], [], {}, WEDNESDAY, memory_entries=mems)
        texts = {c[1] for c in view.commitments}
        assert "回复先给结论" in texts
        assert "今年把英语练到能开会" in texts
        assert "不喝冰" not in texts
        # 记忆条只原样列出、不标逾期。
        assert all(
            not c[2]
            for c in view.commitments
            if c[1] in ("回复先给结论", "今年把英语练到能开会")
        )

    # ------------------------------------------------- 滋养兴趣（V1.69，记录优先）
    def test_preferences_carry_interest_layer(self):
        mems = [
            _mem("m1", "偏好", "我最近在学赛艇"),
            _mem("m2", "偏好", "不喝冰"),
            _mem("m3", "目标", "今年把英语练到能开会"),   # 目标仍留在「承诺与目标」
        ]
        view = build_personal_model([], [], {}, WEDNESDAY, memory_entries=mems)
        texts = {t for _, t in view.preferences}
        assert "我最近在学赛艇" in texts
        assert "不喝冰" in texts
        # 偏好进「滋养兴趣」面，不进「承诺与目标」面（两者刻意分开）。
        ctexts = {c[1] for c in view.commitments}
        assert "我最近在学赛艇" not in ctexts
        assert "今年把英语练到能开会" in ctexts
        # 只原样列出，不评价、不标逾期。
        assert all(_kind == "偏好" for _kind, _ in view.preferences)

    def test_is_empty_counts_preferences(self):
        # 只记了「偏好」也算有内容，不算空模型 —— 有爱好本身就是规律。
        view = build_personal_model(
            [], [], {}, WEDNESDAY,
            memory_entries=[_mem("m1", "偏好", "我最近在学赛艇")],
        )
        assert not view.is_empty



class TestRelationshipsCommitmentsIntegration:
    """经 App 接线（PersonalModelService 持有 promise / memory 仓储）的端到端验证。"""

    def test_view_pulls_contacts_promises_memory(self, env):
        app, clock, role = env
        clock.set(datetime(2026, 10, 21, 14, 30))
        zhang = app.promise.contact("张总")
        # 一条逾期承诺 + 一条 open 等候，都挂在张总身上。
        task = _new(app, role, "给张总报价单")
        app.promise.promise(
            "周三前给张总报价单", contact_name="张总",
            promised_for=clock.now() - timedelta(days=2), task_id=task.id,
        )
        app.tasks.create(
            "等张总回合同", [role.id], kind=TaskKind.WAIT,
            waiting_on=WaitingOn(kind=WaitingKind.PERSON, who_or_what="张总",
                                contact_id=zhang.id,
                                since=clock.now() - timedelta(days=3)),
        )
        app.memory.add("回复先给结论", kind="约定")
        app.memory.add("今年把英语练到能开会", kind="目标")
        app.memory.add("不喝冰", kind="偏好")
        view = _view(env)
        rel = {(r[0], r[1], r[2], r[3]) for r in view.relationships}
        assert (zhang.id, "张总", 1, 1) in rel
        texts = {c[1] for c in view.commitments}
        assert "周三前给张总报价单" in texts
        assert "回复先给结论" in texts
        assert "今年把英语练到能开会" in texts
        assert "不喝冰" not in texts


class TestReadOnlyInvariant:
    """个人模型只读聚合：view() 绝不写库（与 11.17 契约约束同一条纪律）。"""

    def test_view_does_not_write(self, env):
        app, clock, role = env
        clock.set(datetime(2026, 10, 21, 14, 30))
        task = _new(app, role, "交周报")
        app.tasks.complete(task.id)
        before_records = len(app.record_repo.list_all())
        before_tasks = len(app.task_repo.list_all())
        before_open = app.promise.open_count()
        for _ in range(3):
            _view(env)
        assert len(app.record_repo.list_all()) == before_records
        assert len(app.task_repo.list_all()) == before_tasks
        assert app.promise.open_count() == before_open

    def test_is_empty_counts_relationships_and_commitments(self):
        # 只有「跟谁有账」或「记着的目标」也算有内容，不算空模型。
        contacts = [_contact("c1", "张总")]
        promises = [_promise("p1", "c1", "给张总报价单")]
        view = build_personal_model(
            [], [], {}, WEDNESDAY, contacts=contacts, open_promises=promises,
        )
        assert not view.is_empty


# ------------------------------------------- 时段凝练：集中在哪（V1.101）
_done_seq = itertools.count()


def _done(hour: int, day: int = 0) -> Task:
    """一条在「WEDNESDAY 前 day 天的 hour 点」完成的事务。

    id 走递增计数器而不是 ``hash(...)``：同一天同一小时可以完成多件事，
    id 必须条条不同（否则「一条事务完成了三次」会悄悄混进「三个人都在
    21 点完工」—— 峰是计数事实，样本本身不能有歧义）。
    """
    return _task(
        f"t{next(_done_seq):04d}", "事", ("r1",),
        state=TaskState.DONE,
        completed_at=datetime(2026, 10, 21, hour, 30) - timedelta(days=day),
    )


def _view_of(tasks) -> PersonalModelView:
    return build_personal_model(tasks, [], {"r1": "工作"}, WEDNESDAY)


class TestCompletionPeak:
    """「完成集中在哪」的凝练结论（V1.101 / 11.17 节奏面）。

    纪律（每条都有实测撑着）：

    - **每条结论带计数**：V1.96「没有计数的规律不可反驳」在框架层的落地 ——
      「集中在 21-23 点」必须连「多少次、占几成」一起说；
    - **判定不出是 None，不是「分布平坦」**：后者是把判定结果说成结论；
    - **宁可漏判**：孤例（1 次）不定义段的边界、3 次与三分之一两道线都卡着 ——
      漏判的代价是看分布原图，错判的代价是队列/结论里出现一个假规律；
    - 这是**统计描述不是评价**：「37 次里 13 次在 21-23 点」可以说，
      「你总在深夜干活」不可以说（11.17 原样成立）。
    """

    def test_adjacent_hours_merge_into_one_span(self):
        tasks = [_done(21)] * 4 + [_done(22)] * 4 + [_done(23)] * 4
        tasks += [_done(h) for h in (1, 3, 5, 7)]
        view = _view_of(tasks)
        assert view.hour_peak == (21, 23, 12)

    def test_lone_hour_does_not_extend_the_span(self):
        """20 点只完成过 1 次 —— 孤例不把「21-23 点」撑成「20-23 点」。"""
        tasks = [_done(20)] + [_done(21)] * 4 + [_done(22)] * 4 + [_done(23)] * 4
        tasks += [_done(h) for h in (1, 3, 5, 7, 9, 11, 13)]
        view = _view_of(tasks)
        assert view.hour_peak == (21, 23, 12)

    def test_single_hour_span_is_not_a_range(self):
        """3 次全在 21 点 —— 起止相同，呈现层说「21 点」不说「21-21 点」。"""
        view = _view_of([_done(21)] * 3 + [_done(h) for h in (1, 3, 5, 7, 9, 11)])
        assert view.hour_peak == (21, 21, 3)

    def test_below_min_count_is_not_a_peak(self):
        """一段里只有 2 次 —— 偶发不算规律（同 _MIN_ROLLOVERS 的精神）。"""
        view = _view_of([_done(21)] * 2 + [_done(h) for h in (1, 3, 5, 7)])
        assert view.hour_peak is None

    def test_below_share_is_not_a_peak(self):
        """3 次在 21 点但只占 3/23 —— 那是「分布平坦」，不是「集中」。"""
        view = _view_of([_done(21)] * 3 + [_done(h) for h in range(0, 20)])
        assert view.hour_peak is None

    def test_exactly_one_third_counts(self):
        """边界值：3/9 恰好三分之一 —— 判定线是「≥」，不含糊。"""
        view = _view_of([_done(21)] * 3 + [_done(h) for h in (1, 2, 3, 4, 5, 6)])
        assert view.hour_peak == (21, 21, 3)

    def test_tie_takes_earlier_span(self):
        """两段并列时取起始早的 —— 稳定、可预期，不引入随机兜底。"""
        # 两段各 8 次并列；其余 8 次散点 ⇒ 8*3=24 ≥ 24，两段都过线，
        # 于是「同数取起始早」这条真被走到（不是只有一个候选）。
        tasks = [_done(9)] * 4 + [_done(10)] * 4 + [_done(21)] * 4 + [_done(22)] * 4
        tasks += [_done(h) for h in (1, 3, 5, 7, 11, 13, 15, 17)]
        view = _view_of(tasks)
        assert view.hour_peak == (9, 10, 8)

    def test_scattered_singles_are_not_a_peak(self):
        """每小时各 1 次：没有任何一段够得上「集中」。"""
        view = _view_of([_done(h) for h in range(0, 12)])
        assert view.hour_peak is None

    def test_no_completions_no_peak(self):
        view = _view_of([])
        assert view.hour_peak is None
        assert view.dow_peak is None

    def test_dow_peak_picks_the_busiest_day(self):
        # 散日只取 1..6：day 7 是一周前的同一天，会把周三多算一次。
        # 2026-10-21 是周三（weekday 2，见 WEDNESDAY 常量的注释）。
        tasks = [_done(21, 0)] * 13
        tasks += [_done(10, d) for d in range(1, 7)]
        tasks += [_done(11, d) for d in range(1, 5)]
        view = _view_of(tasks)
        assert view.dow_peak == (2, 13)

    def test_dow_below_threshold_is_none(self):
        view = _view_of([_done(9, 0)] * 2 + [_done(10, d) for d in range(1, 5)])
        assert view.dow_peak is None

    def test_peak_count_matches_the_distribution(self):
        """峰里的计数 = 分布里那一段的小时计数之和（可对账）。"""
        tasks = [_done(21)] * 5 + [_done(22)] * 5 + [_done(23)] * 3
        tasks += [_done(h) for h in range(0, 20, 2)]
        view = _view_of(tasks)
        start, end, count = view.hour_peak
        assert count == sum(
            c for h, c in view.completion_by_hour if start <= h <= end
        )

    def test_peak_is_not_added_to_is_empty(self):
        """凝练结论不改变「有没有可归纳的事实」的判定 —— 那是 done 的事。"""
        view = _view_of([_done(21)] * 3 + [_done(h) for h in (1, 3, 5)])
        assert view.hour_peak == (21, 21, 3)
        assert not view.is_empty


class TestPeakRendering:
    """峰在 CLI 输出里长什么样（V1.101）。"""

    def test_peak_stated_with_count_and_share(self):
        tasks = [_done(21)] * 5 + [_done(22)] * 5 + [_done(23)] * 3
        tasks += [_done(h) for h in range(0, 20, 2)]
        text = render_model(_view_of(tasks), {})
        assert "其中 13 次（56%）集中在 21-23 点" in text

    def test_single_hour_peak_label(self):
        text = render_model(_view_of([_done(21)] * 3 + [_done(h) for h in (1, 3, 5, 7, 9, 11)]), {})
        assert "其中 3 次（33%）集中在 21 点" in text
        assert "21-21" not in text

    def test_dow_peak_stated(self):
        tasks = [_done(21, 0)] * 13
        tasks += [_done(10, d) for d in range(1, 7)]
        tasks += [_done(11, d) for d in range(1, 5)]
        text = render_model(_view_of(tasks), {})
        assert "其中 13 次（56%）集中在周三" in text

    def test_missing_peak_explains_the_threshold(self):
        """判不出要说清判定线 —— 一声不响只会让人以为漏印了一行。"""
        text = render_model(_view_of([_done(9), _done(14), _done(20)]), {})
        assert "没有达到「集中」的判定线" in text
        assert "至少 3 次" in text and "三分之一" in text

    def test_peak_is_a_description_not_advice(self):
        """统计描述不是评价：输出里不许出现「建议 / 应该 / 不如」。"""
        tasks = [_done(21)] * 5 + [_done(22)] * 5 + [_done(23)] * 3
        tasks += [_done(h) for h in range(0, 20, 2)]
        text = render_model(_view_of(tasks), {})
        for word in ("建议", "应该", "不如", "熬夜"):
            assert word not in text

    def test_empty_window_says_no_completions(self):
        """空库走 is_empty 早退（「还没有可归纳的记录」）—— 没有峰行可摆。"""
        text = render_model(_view_of([]), {})
        assert "还没有可归纳的记录" in text
        assert "集中在" not in text
        assert "判定线" not in text


class TestModelPayloadPeaks:
    """``/api/model`` 的峰字段（V1.101）：判定在服务层，web 只拼形状。"""

    def test_payload_exposes_peaks_as_arrays(self):
        from freeagent.web.serialize import model_payload

        tasks = [_done(21)] * 3 + [_done(h) for h in (1, 3, 5, 7, 9, 11)]
        payload = model_payload(_view_of(tasks), WEDNESDAY)
        assert payload["hour_peak"] == [21, 21, 3]
        assert payload["dow_peak"] is not None

    def test_payload_peak_is_null_when_not_judged(self):
        from freeagent.web.serialize import model_payload

        payload = model_payload(_view_of([_done(9), _done(14), _done(20)]), WEDNESDAY)
        assert payload["hour_peak"] is None
        assert "done_in_window" in payload, "占比要能由前端现算，总数必须在"
