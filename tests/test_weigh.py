"""代价对照（设计文档 7.5 / 20.1.14）。

断言纪律（同 test_care.py）：按「状态正确 / 关联正确 / 计数正确」写，
不按「输出里有没有某个词」写 —— 但**代价项的纪律本身就是「不许出现某类
措辞」**，所以那几条只能查措辞（那是它要守的东西本身）。

最易错的地方有四件：
① **争用单位是日期**（不是精力档 —— 档太粗，说不硬）；
② **已过期的那天换成今天**（过期的事占的是此刻，不是未来某天）；
③ **纯等候不参与争用**（在等别人的事占不了你的时间）；
④ **没有真取舍必须返回 ``None``** —— 降格成「建议做 X」是这一刀最坏的
失败模式，比不给更坏。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from freeagent.app import build_app
from freeagent.domain import (
    PromiseStatus,
    TaskKind,
    TaskState,
    WaitingKind,
    WaitingOn,
)
from freeagent.services.clock import FrozenClock
from freeagent.services.sorting import EnergyWindows, score_and_sort
from freeagent.services.weigh import (
    FACET_EXTERNAL,
    FACET_HARD_DEADLINE,
    FACET_NO_DEADLINE,
    FACET_SELF_ONLY,
    FACET_WAITING_ON_OTHER,
    MAX_OPTIONS,
    WeighResult,
    weigh,
)

#: 2026-09-26 是周六，钉住时刻。
SATURDAY = datetime(2026, 9, 26, 9, 0)


@pytest.fixture
def env(tmp_path):
    frozen = FrozenClock(SATURDAY)
    application = build_app(
        tmp_path / "agent.db", clock=frozen, energy_windows=EnergyWindows()
    )
    role = application.roles.create("工作")
    try:
        yield application, frozen, role
    finally:
        application.close()


def _scored(app, clock):
    """全部未结束事务按信号权重排序（weigh 的入参形态）。"""
    return score_and_sort(
        app.tasks.list_open(),
        clock.now(),
        clock.today(),
        dependents_of=app.task_repo.count_dependents,
        energy_windows=app.energy_windows,
        promise_of=app.promise.promise_of,
    )


def _weigh(app, clock):
    return weigh(
        _scored(app, clock),
        clock.now(),
        today=clock.today(),
        promises_by_task={p.task_id: p for p in app.promise.open_promises()},
    )


def _make_task(app, role, title, *, due=None, scheduled=None, kind=None):
    task = app.tasks.create(
        role_ids=[role.id], title=title, kind=kind or TaskKind.ACTION
    )
    if due is not None:
        task = app.tasks.set_due_time(task.id, due)
    if scheduled is not None:
        task = app.tasks.schedule(task.id, scheduled)
    return task


def _pair_on_same_day(app, role):
    """两件都占今天，且**性质不同**（一件有硬期限、一件没有）。

    为什么要性质不同：呈现层那几条守卫要验的是「摆出来的样子」，
    而「性质相同」的输出里没有「一件是…另一件是…」可查。
    """
    _make_task(
        app, role, "甲", due=SATURDAY, scheduled=SATURDAY.date()
    )
    _make_task(app, role, "乙", scheduled=SATURDAY.date())


# ----------------------------------------------------------------- 事实层
class TestWeighFacts:
    def test_no_tasks_gives_none(self, env):
        app, clock, _role = env
        assert _weigh(app, clock) is None

    def test_single_task_gives_none(self, env):
        app, clock, role = env
        _make_task(app, role, "一件事", due=SATURDAY + timedelta(days=1))
        assert _weigh(app, clock) is None

    def test_facets_carry_source(self, env):
        """每项代价都要能指回库里的一列 —— 这是本模块能不能存在的前提。"""
        app, clock, role = env
        _make_task(app, role, "甲", due=SATURDAY + timedelta(days=1))
        _make_task(app, role, "乙", due=SATURDAY + timedelta(days=1))
        result = _weigh(app, clock)
        assert result is not None
        for opt in result.options:
            assert opt.facets, "代价清单不能是空的"
            for facet in opt.facets:
                assert facet.source, f"代价项 {facet.code} 没有出处"

    def test_external_promise_makes_facet_external(self, env):
        """有未兑现的对外承诺 → 代价维度是「对外」。"""
        app, clock, role = env
        task = _make_task(app, role, "甲", due=SATURDAY + timedelta(days=1))
        app.promise.promise(
            task_id=task.id,
            statement="给客户回话",
            promised_for=SATURDAY + timedelta(days=1),
        )
        _make_task(app, role, "乙", due=SATURDAY + timedelta(days=1))
        result = _weigh(app, clock)
        assert result is not None
        codes = {f.code for opt in result.options for f in opt.facets}
        assert FACET_EXTERNAL in codes
        assert FACET_SELF_ONLY in codes

    def test_promise_facet_source_is_promised_for(self, env):
        """**有对外承诺时**，那一项代价的出处必须是 ``promises.promised_for``。

        为什么单独一条：``test_facets_carry_source`` 只查「出处非空」，
        而「有承诺」那个分支**平时走不到** —— 变异探针改它的 source，
        测试全绿（V1.109 的坑：改的地方不对，探针不会红）。
        """
        app, clock, role = env
        task = _make_task(app, role, "甲", due=SATURDAY + timedelta(days=1))
        app.promise.promise(
            task_id=task.id,
            statement="给客户回话",
            promised_for=SATURDAY + timedelta(days=1),
        )
        _make_task(app, role, "乙", due=SATURDAY + timedelta(days=1))
        result = _weigh(app, clock)
        assert result is not None
        sources = {
            facet.source
            for opt in result.options
            for facet in opt.facets
            if facet.code == FACET_EXTERNAL
        }
        assert sources == {"promises.promised_for"}

    def test_settled_promise_is_not_a_cost(self, env):
        """已兑现的承诺不算代价 —— 那件事已经过去了。"""
        app, clock, role = env
        task = _make_task(app, role, "甲", due=SATURDAY + timedelta(days=1))
        promise = app.promise.promise(
            task_id=task.id,
            statement="给客户回话",
            promised_for=SATURDAY + timedelta(days=1),
        )
        app.promise.keep(promise.id)
        assert app.promise.promise_of(task.id).status is PromiseStatus.KEPT
        _make_task(app, role, "乙", due=SATURDAY + timedelta(days=1))
        result = _weigh(app, clock)
        assert result is not None
        codes = {f.code for opt in result.options for f in opt.facets}
        assert FACET_EXTERNAL not in codes


# ----------------------------------------------------------------- 争用判定
class TestWeighContention:
    def test_same_day_conflicts(self, env):
        """同一天到期 → 是取舍。"""
        app, clock, role = env
        _make_task(app, role, "甲", due=SATURDAY + timedelta(days=1))
        _make_task(app, role, "乙", due=SATURDAY + timedelta(days=1))
        assert _weigh(app, clock) is not None

    def test_different_days_is_not_a_conflict(self, env):
        """不在同一天 → 不是取舍（假冲突比不摆更坏）。"""
        app, clock, role = env
        _make_task(app, role, "甲", due=SATURDAY + timedelta(days=1))
        _make_task(app, role, "乙", due=SATURDAY + timedelta(days=8))
        assert _weigh(app, clock) is None

    def test_overdue_counts_as_today(self, env):
        """已过期的那天换成今天 —— 过期的事占的是此刻。"""
        app, clock, role = env
        _make_task(app, role, "甲", due=SATURDAY - timedelta(days=3))
        _make_task(app, role, "乙", due=SATURDAY)
        assert _weigh(app, clock) is not None

    def test_overdue_does_not_conflict_with_next_week(self, env):
        """过期的事不与下周三到期的事冲突。"""
        app, clock, role = env
        _make_task(app, role, "甲", due=SATURDAY - timedelta(days=3))
        _make_task(app, role, "乙", due=SATURDAY + timedelta(days=8))
        assert _weigh(app, clock) is None

    def test_unscheduled_without_deadline_takes_no_day(self, env):
        """没排期、没期限 → 不占任何一天，不参与争用。"""
        app, clock, role = env
        _make_task(app, role, "甲")
        _make_task(app, role, "乙")
        assert _weigh(app, clock) is None

    def test_waiting_task_does_not_contend(self, env):
        """纯等候（``kind == WAIT``）占不了你的时间 —— 它跟任何事都不冲突。

        建法走 ``set_kind`` + ``waiting_on``：4.3 的不变式要求
        「未结束的等候类事务必须 BLOCKED 且带 waiting_on」，
        所以 WAIT 事务**不能**是「有 waiting_on 但 kind 不是 WAIT」那样建的。

        **它必须占着一天**（有 due）—— 否则它根本不参与争用，
        去掉「纯等候」那道门也看不出差别，守卫就成了摆设
        （这条是变异探针逮出来的：最初这测试没给 due，探针全绿）。
        """
        app, clock, role = env
        waiting = _make_task(app, role, "等客户回话", due=SATURDAY)
        app.tasks.set_kind(
            waiting.id,
            TaskKind.WAIT,
            waiting_on=WaitingOn(
                kind=WaitingKind.PERSON,
                who_or_what="客户回话",
                since=SATURDAY,
            ),
        )
        _pair_on_same_day(app, role)
        result = _weigh(app, clock)
        assert result is not None
        titles = {opt.task.title for opt in result.options}
        assert "等客户回话" not in titles

    def test_same_cost_dimensions_is_still_a_tradeoff(self, env):
        """**性质相同也是取舍** —— 取舍成立看互斥（同一天），不看差异。

        这条最初写错过：曾把「代价性质不同」当成取舍的门槛，
        于是**最常见的取舍（同一天两件事）恰好被过滤掉**，
        剩下只有「性质不同」的那种才被摆出来 —— 而那种本来就更该被看见。
        """
        app, clock, role = env
        _make_task(app, role, "甲", due=SATURDAY + timedelta(days=1))
        _make_task(app, role, "乙", due=SATURDAY + timedelta(days=1))
        result = _weigh(app, clock)
        assert result is not None
        # 两件性质相同 → 没有差异可讲，但要说清「同在哪」
        assert "性质相同" in result.because

    def test_different_dimensions_give_a_difference(self, env):
        """性质不同 → 说清差在哪（这是呈现层的增强，不是门槛）。"""
        app, clock, role = env
        _make_task(app, role, "甲", due=SATURDAY, scheduled=SATURDAY.date())
        _make_task(app, role, "乙", scheduled=SATURDAY.date())
        result = _weigh(app, clock)
        assert result is not None
        assert "一件是" in result.because
        assert "性质相同" not in result.because

    def test_one_has_no_deadline_other_has(self, env):
        """有期限 vs 无期限 → 代价性质不同 → 是取舍。

        **两者必须同一天** —— 不同天不是取舍（那是对的，见上一条测试）。
        这里甲排到今天、乙也排到今天但乙没有硬期限。
        """
        app, clock, role = env
        _make_task(app, role, "甲", due=SATURDAY, scheduled=SATURDAY.date())
        _make_task(app, role, "乙", scheduled=SATURDAY.date())
        result = _weigh(app, clock)
        assert result is not None
        codes = {f.code for opt in result.options for f in opt.facets}
        assert FACET_HARD_DEADLINE in codes
        assert FACET_NO_DEADLINE in codes


# ----------------------------------------------------------------- 边界
class TestWeighBounds:
    def test_options_capped(self, env):
        """同一天挤太多 → 只列 MAX_OPTIONS 件（超载不是取舍）。"""
        app, clock, role = env
        for i in range(MAX_OPTIONS + 2):
            _make_task(
                app,
                role,
                f"件{i}",
                due=SATURDAY + timedelta(days=1),
                kind=TaskKind.ACTION if i % 2 == 0 else TaskKind.REMINDER,
            )
        result = _weigh(app, clock)
        assert result is not None
        assert len(result.options) <= MAX_OPTIONS

    def test_finished_task_not_included(self, env):
        """已结束的事不参与争用。"""
        app, clock, role = env
        done = _make_task(app, role, "甲", due=SATURDAY + timedelta(days=1))
        app.tasks.complete(done.id)
        _make_task(app, role, "乙", due=SATURDAY + timedelta(days=1))
        result = _weigh(app, clock)
        assert result is None

    def test_dropped_task_not_included(self, env):
        app, clock, role = env
        dropped = _make_task(app, role, "甲", due=SATURDAY + timedelta(days=1))
        app.tasks.drop(dropped.id)
        _make_task(app, role, "乙", due=SATURDAY + timedelta(days=1))
        assert _weigh(app, clock) is None

    def test_blocked_task_still_contends(self, env):
        """**卡在流程里**的事照样占你今天的时间。

        4.3 的不变式：未结束的事务里 ``state == BLOCKED`` **必然**有
        ``waiting_on``。所以判「纯等候」只能用 ``kind == WAIT`` ——
        按 ``waiting_on`` 判会把「卡住的事」误当成「不占时间」。

        这里造一条 ACTION + BLOCKED 的事务（等审批），它**该**参与争用。
        """
        app, clock, role = env
        blocked = _make_task(app, role, "甲", due=SATURDAY)
        app.tasks.set_state(
            blocked.id,
            TaskState.BLOCKED,
            waiting_on=WaitingOn(
                kind=WaitingKind.PERSON,
                who_or_what="等审批",
                since=SATURDAY,
            ),
        )
        _make_task(app, role, "乙", scheduled=SATURDAY.date())
        result = _weigh(app, clock)
        assert result is not None
        titles = {opt.task.title for opt in result.options}
        assert "甲" in titles


# ----------------------------------------------------------------- 呈现层
class TestWeighRendering:
    def test_render_says_no_tradeoff_when_none(self, env):
        """没有取舍时如实说，不能降格成建议。

        措辞里**不许**出现「代价不在一处」当必要条件 —— 那是本模块
        最初写错的地方（V1.111 改掉了）。若这里写回去，主人会以为
        「性质一样的事就不算取舍」，而实际上同一天就是互斥。
        """
        from freeagent.cli import render

        text = render.render_weigh(None)
        assert "没有需要你拍板的取舍" in text
        assert "不在一处" not in text
        for word in ("建议", "你应该", "最好"):
            assert word not in text, f"空态出现了祈使措辞「{word}」"

    def test_render_has_no_imperative(self, env):
        """方案用陈述句 —— 出现祈使句就是违规。

        这条只能查措辞：它要守的**就是**措辞本身
        （``delegate.py:551``：指令类语句是自动决策的前一步）。
        """
        from freeagent.cli import render

        app, clock, role = env
        _pair_on_same_day(app, role)
        result = _weigh(app, clock)
        assert result is not None
        text = render.render_weigh(result)
        for word in ("你应该", "建议", "最好", "请先", "务必"):
            assert word not in text, f"呈现层出现了祈使措辞「{word}」"

    def test_render_shows_source(self, env):
        """代价项带出处，主人能自己核对。"""
        from freeagent.cli import render

        app, clock, role = env
        _pair_on_same_day(app, role)
        result = _weigh(app, clock)
        assert result is not None
        text = render.render_weigh(result)
        assert "来源：" in text
        assert "tasks.due_time" in text

    def test_render_does_not_rank(self, env):
        """不排序 —— 呈现层不决定谁先谁后。"""
        from freeagent.cli import render

        app, clock, role = env
        _pair_on_same_day(app, role)
        result = _weigh(app, clock)
        assert result is not None
        text = render.render_weigh(result)
        for word in ("推荐", "更该", "先做", "优先"):
            assert word not in text, f"呈现层出现了排序措辞「{word}」"


# ----------------------------------------------------------------- 守卫
class TestWeighGuards:
    def test_module_never_reads_human_decisions(self, env):
        """**不读人的任何决策** —— 这是不碰 11.9.11 自我进化边界的保证。

        守卫写法：不查「代码里没有某个词」（那是形状），
        而是查**行为**：跑一遍 weigh，输出不因「人点过什么」而变。
        """
        from freeagent.services.approval import ApprovalStore

        app, clock, role = env
        _pair_on_same_day(app, role)
        before = _weigh(app, clock)
        assert before is not None

        # 人做过一次拒绝（审批流里落一行）
        store = ApprovalStore(app.conn, clock=clock.now)
        store.ask("测试", detail="测试", requested_by="me")
        after = _weigh(app, clock)

        assert after is not None
        assert before.because == after.because
        assert [o.task.id for o in before.options] == [
            o.task.id for o in after.options
        ]

    def test_weigh_is_pure(self, env):
        """纯派生视图 —— 不写库。"""
        app, clock, role = env
        _pair_on_same_day(app, role)
        before = len(app.tasks.list_all())
        _weigh(app, clock)
        assert len(app.tasks.list_all()) == before
