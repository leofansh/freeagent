"""决策日志（设计文档 7.6 / 19.5.4 候选 D）。

断言纪律（同 test_weigh.py）：按「状态正确 / 关联正确 / 计数正确」写，
不按「输出里有没有某个词」写 —— 但**呈现层的纪律本身就是「不许出现某类
措辞」**，所以那几条只能查措辞（那是它要守的东西本身）。

本模块最容易做错的地方有六件：
① **快照必须冻结** —— 落库那一刻的代价文本，之后不随事实变；
   若对着峙时现算，就变成「拿今天的事实跟今天的事实比」，什么也照不出来；
② **task_id 只用于查现况，不用于重建当时的文本** —— 重建即漂移；
③ **只摆事实不评价** —— 不说「你选对了」也不说「你选错了」；
④ **不读人的审批决策** —— 输出与「人点过什么」无关（11.9.11 第 ② 条）；
⑤ **快照读不出来就抛** —— 静默跳过会让人以为没拍过板；
⑥ **同一板不记两遍** —— 重复拍板不是两次拍板；但换了选择、跨天都得留两条。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from freeagent.app import build_app
from freeagent.domain import TaskKind
from freeagent.services.clock import FrozenClock
from freeagent.services.sorting import EnergyWindows, score_and_sort
from freeagent.services.weigh import weigh

#: 钉住时刻。
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


def _weigh(app, clock):
    scored = score_and_sort(
        app.tasks.list_open(),
        clock.now(),
        clock.today(),
        dependents_of=app.task_repo.count_dependents,
        energy_windows=app.energy_windows,
        promise_of=app.promise.promise_of,
    )
    return weigh(
        scored,
        clock.now(),
        today=clock.today(),
        promises_by_task={p.task_id: p for p in app.promise.open_promises()},
    )


def _make_task(app, role, title, *, due=None, scheduled=None):
    task = app.tasks.create(role_ids=[role.id], title=title, kind=TaskKind.ACTION)
    if due is not None:
        task = app.tasks.set_due_time(task.id, due)
    if scheduled is not None:
        task = app.tasks.schedule(task.id, scheduled)
    return task


def _two_contending(app, role):
    """两件都占今天 —— 一件有对外承诺，一件没有。"""
    a = _make_task(app, role, "甲", due=SATURDAY, scheduled=SATURDAY.date())
    app.promise.promise(
        task_id=a.id,
        statement="给客户回话",
        promised_for=SATURDAY,
    )
    b = _make_task(app, role, "乙", due=SATURDAY, scheduled=SATURDAY.date())
    return a, b


# --------------------------------------------------------------- 记
class TestRecord:
    def test_record_persists_one_row(self, env):
        app, clock, role = env
        _two_contending(app, role)
        result = _weigh(app, clock)
        assert result is not None

        app.decision.record(result, 0)

        history = app.decision.history()
        assert len(history) == 1
        assert history[0].chosen_index == 0

    def test_record_freezes_the_facets(self, env):
        """**快照冻结** —— 落库后事实变了，快照里的代价文本不变。"""
        app, clock, role = env
        a, b = _two_contending(app, role)
        result = _weigh(app, clock)
        assert result is not None

        app.decision.record(result, 0)
        snapshot = app.decision.history()[0].options_json

        # 事实变了：承诺兑现了、事务做完了
        promise = app.promise.promise_of(a.id)
        assert promise is not None
        app.promise.keep(promise.id)
        app.tasks.complete(a.id)

        after = app.decision.history()[0].options_json
        assert after == snapshot, "快照必须冻结，不随事实漂移"
        # 且冻结的那份**说的是当时**：仍带着「未兑现」
        from freeagent.services.decision import parse_frozen_options

        frozen = parse_frozen_options(after)
        texts = [text for f in frozen[0].facets for text in (f[0],)]
        assert any("未兑现" in t for t in texts), (
            f"冻结的快照应保留当时的「未兑现」，实际：{texts}"
        )

    def test_snapshot_keeps_task_id_for_lookup(self, env):
        """快照里带 task_id —— 「现在」那一侧要靠它查现况。"""
        app, clock, role = env
        a, b = _two_contending(app, role)
        result = _weigh(app, clock)
        app.decision.record(result, 0)

        payload = json.loads(app.decision.history()[0].options_json)
        ids = {item["task_id"] for item in payload}
        assert ids == {a.id, b.id}

    def test_out_of_range_raises(self, env):
        """选一个不存在的选项 → 抛，不静默夹到范围内。

        静默夹会把「选错了」变成「记了别的那个」，那比报错坏得多。
        """
        app, clock, role = env
        _two_contending(app, role)
        result = _weigh(app, clock)
        with pytest.raises(ValueError):
            app.decision.record(result, 5)
        with pytest.raises(ValueError):
            app.decision.record(result, -1)
        assert app.decision.history() == []

    def test_confront_date_is_seven_days_later(self, env):
        """对峙日 = 拍板日 + 7（CONFRONT_AFTER_DAYS）。"""
        app, clock, role = env
        _two_contending(app, role)
        result = _weigh(app, clock)
        app.decision.record(result, 0)

        from freeagent.services.decision import CONFRONT_AFTER_DAYS

        decision = app.decision.history()[0]
        assert decision.confront_on == (
            SATURDAY.date() + timedelta(days=CONFRONT_AFTER_DAYS)
        )


# ------------------------------------------------- 同一板不记两遍（幂等）
class TestIdempotent:
    """**同一板、同一选择、同一天 → 只记一条**（设计文档 7.6.6）。

    连按两次 ``/weigh 1`` 不是两次拍板，是**一次拍板被记了两遍** ——
    日志里出现两条逐字相同的记录，对峙时会对着质两次同一件事。

    **但合并要有边界**：换了选择是「另一板」，跨天是「另一次想」——
    那两种情况都必须留下两条。合并只发生在「同一板、同一选择、同一天」。
    """

    def test_same_board_same_choice_records_once(self, env):
        app, clock, role = env
        _two_contending(app, role)
        result = _weigh(app, clock)

        first = app.decision.record(result, 0)
        again = app.decision.record(result, 0)

        assert first.is_new is True
        assert again.is_new is False, "重复拍同一块板要说成「还是那一条」"
        assert len(app.decision.history()) == 1, "库里不能多出一条"
        assert again.decision.id == first.decision.id, "命中的是同一条"

    def test_changed_choice_keeps_both_rows(self, env):
        """改了主意 → **两条都留** —— 那是真实历史，不是噪音。"""
        app, clock, role = env
        _two_contending(app, role)
        result = _weigh(app, clock)

        app.decision.record(result, 0)
        second = app.decision.record(result, 1)

        assert second.is_new is True, "换了选择就是另一板"
        history = app.decision.history()
        assert len(history) == 2
        assert {d.chosen_index for d in history} == {0, 1}

    def test_next_day_records_a_second_row(self, env):
        """跨天 → 另记一条 —— 对峙日按各自拍板日 + 7 天算。

        合成一条会让后来的那次**提前对质**（对峙日会跟着第一次走）。
        """
        app, clock, role = env
        _two_contending(app, role)
        result = _weigh(app, clock)

        app.decision.record(result, 0)
        clock.advance(days=1)
        second = app.decision.record(result, 0)

        assert second.is_new is True, "跨天是另一次想"
        assert len(app.decision.history()) == 2
        confronts = {d.confront_on for d in app.decision.history()}
        assert len(confronts) == 2, "两条的对峙日必须各自算"

    def test_wavering_keeps_the_final_choice(self, env):
        """甲 → 乙 → 甲：三条都在，「最后落在甲」不能被合并掉。

        **判据若拿「历史里任何一条」比**，最后那次甲会并进第一次甲 ——
        于是「最后落在甲」这件事从日志里消失了。
        """
        app, clock, role = env
        _two_contending(app, role)
        result = _weigh(app, clock)

        app.decision.record(result, 0)
        clock.advance(hours=1)
        app.decision.record(result, 1)
        clock.advance(hours=1)
        third = app.decision.record(result, 0)

        assert third.is_new is True
        history = app.decision.history()
        assert len(history) == 3
        # 最近的先列 —— 最后一次拍板必须排在最前
        assert [d.chosen_index for d in history] == [0, 1, 0]

    def test_same_moment_orders_by_insertion(self, env):
        """同刻多条 → 按**插入序**倒序，不是随机 id。

        ``id`` 是 ``new_id()`` 的随机 hex，拿它定序等于随机排序
        （仓库那条纪律：同刻定序不要用随机 id 兜底）。``rowid`` 是插入序。
        """
        app, clock, role = env
        _two_contending(app, role)
        result = _weigh(app, clock)

        first = app.decision.record(result, 0)
        second = app.decision.record(result, 1)

        history = app.decision.history()
        assert [d.id for d in history] == [second.decision.id, first.decision.id]


# ----------------------------------------------------------- 到期与对峙
class TestConfront:
    def test_not_due_before_the_day(self, env):
        app, clock, role = env
        _two_contending(app, role)
        result = _weigh(app, clock)
        app.decision.record(result, 0)

        assert app.decision.due(clock.today()) == []
        clock.advance(days=6)
        assert app.decision.due(clock.today()) == []
        clock.advance(days=1)
        assert len(app.decision.due(clock.today())) == 1

    def test_confront_shows_then_and_now(self, env):
        """**当时 vs 现在并排** —— 这是本章存在的理由。"""
        app, clock, role = env
        a, b = _two_contending(app, role)
        result = _weigh(app, clock)
        app.decision.record(result, 0)

        clock.advance(days=7)
        app.tasks.complete(a.id)

        decision = app.decision.due(clock.today())[0]
        c = app.decision.confront(decision)

        # 当时那侧：选的是甲，facets 带出处
        assert c.chosen is not None
        assert c.chosen.title == "甲"
        assert c.chosen.facets, "当时那侧必须带代价项"
        for text, source in c.chosen.facets:
            assert text and source, "每项代价都要有文本与出处"

        # 现在那侧：甲已完成、乙还在
        joined = "\n".join(c.current_lines)
        assert "甲" in joined and "乙" in joined
        assert any("已完成" in line for line in c.current_lines)

    def test_confront_reports_vanished_task(self, env):
        """事务被删了 → 如实说「已经不在库里了」，不静默跳过。"""
        app, clock, role = env
        a, b = _two_contending(app, role)
        result = _weigh(app, clock)
        app.decision.record(result, 0)

        clock.advance(days=7)
        app.task_repo.delete(a.id)

        decision = app.decision.due(clock.today())[0]
        c = app.decision.confront(decision)

        assert c.vanished_titles == ("甲",)
        assert "甲" not in "\n".join(c.current_lines)

    def test_confront_uses_the_frozen_facets(self, env):
        """对峙的「当时那侧」必须是**冻结的那份**，不是现算的。

        ``test_record_freezes_the_facets`` 查的是**存进去的那份**没变；
        这一条查**呈现出来的那份**没变 —— **存对了但取错了，同样是漂移**
        （对着今天的事实比今天的事实，什么也照不出来）。
        """
        app, clock, role = env
        a, b = _two_contending(app, role)
        result = _weigh(app, clock)
        app.decision.record(result, 0)

        from freeagent.services.decision import parse_frozen_options

        frozen = parse_frozen_options(app.decision.history()[0].options_json)

        # 事实变了：承诺兑现了 —— 现算的话「未兑现」就不见了
        promise = app.promise.promise_of(a.id)
        assert promise is not None
        app.promise.keep(promise.id)
        clock.advance(days=7)

        c = app.decision.confront(app.decision.due(clock.today())[0])
        assert c.chosen is not None
        assert c.chosen.facets == frozen[0].facets, (
            "当时那侧的代价必须逐字等于冻结的那份，不许现算"
        )

    def test_confront_never_reads_human_decisions(self, env):
        """**不读人的任何决策** —— 不碰 11.9.11 的自我进化边界。

        守卫写法：不查「代码里没有某个词」（那是形状），查**行为** ——
        跑一遍对峙，输出不因「人点过什么」而变。

        落决策的方式是**直接写 ``pending_approvals`` 表**：审批存储不是
        App 上的公开字段，而这张表正是「人的决策」真正住的地方 ——
        从真源下手，比调一个公开 API 更有说服力。
        """
        app, clock, role = env
        _two_contending(app, role)
        result = _weigh(app, clock)
        app.decision.record(result, 0)

        clock.advance(days=7)
        before = app.decision.confront(app.decision.due(clock.today())[0])

        # 人在审批流里点了一次（真源落一行：decision='deny'）
        app.conn.execute(
            "INSERT INTO pending_approvals"
            " (credential, subject, detail, asked_at, expires_at, decision,"
            "  decided_at, decided_by)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "test-credential",
                "测试",
                "测试",
                clock.now().isoformat(),
                (clock.now() + timedelta(hours=1)).isoformat(),
                "deny",
                clock.now().isoformat(),
                "me",
            ),
        )
        app.conn.commit()

        after = app.decision.confront(app.decision.due(clock.today())[0])
        assert before.current_lines == after.current_lines
        assert before.chosen == after.chosen

    def test_broken_snapshot_raises(self, env):
        """快照读不出来 → 抛，不静默跳过。

        静默跳过 = 让人以为没拍过板 —— 本仓最坏的失效形态。
        """
        app, clock, role = env
        _two_contending(app, role)
        result = _weigh(app, clock)
        app.decision.record(result, 0)

        decision = app.decision.history()[0]
        broken = decision.__class__(
            id=decision.id,
            chosen_index=decision.chosen_index,
            options_json="不是 JSON",
            reason_text=decision.reason_text,
            conflict_text=decision.conflict_text,
            decided_at=decision.decided_at,
            confront_on=decision.confront_on,
        )
        with pytest.raises(ValueError):
            app.decision.confront(broken)


# ------------------------------------------------------------- 呈现层
class TestRender:
    def test_picked_receipt_shows_which_one(self, env):
        from freeagent.cli import render

        app, clock, role = env
        _two_contending(app, role)
        result = _weigh(app, clock)
        text = render.render_weigh_picked(result, 1)

        assert "乙" in text
        assert "你选的" in text
        # 不评价
        for word in ("好选择", "不错", "正确", "明智"):
            assert word not in text, f"回执出现评价措辞「{word}」"

    def test_confront_output_never_judges(self, env):
        """对峙**只摆事实** —— 不说「你选对了」也不说「你选错了」。"""
        from freeagent.cli import render

        app, clock, role = env
        a, b = _two_contending(app, role)
        result = _weigh(app, clock)
        app.decision.record(result, 0)

        clock.advance(days=7)
        app.tasks.complete(a.id)
        cs = [app.decision.confront(d) for d in app.decision.due(clock.today())]
        text = render.render_decisions(cs, today=clock.today())

        for word in ("选对了", "选错了", "正确", "明智", "后悔", "可惜"):
            assert word not in text, f"对峙出现评价措辞「{word}」"

    def test_no_due_says_so_plainly(self, env):
        """没有到期记录 → 如实说，不降格成「那先做 X」。"""
        from freeagent.cli import render

        text = render.render_decisions([], today=SATURDAY.date())
        assert "没有到期的取舍记录" in text
        for word in ("建议", "先做", "你应该"):
            assert word not in text, f"空态出现建议措辞「{word}」"

    def test_receipt_tells_new_from_repeat(self, env):
        """回执要能说清「刚记的」与「今天已经记过」。

        两者都说成「记下了」就是把事实说错 —— 后者库里并没有多出一行。
        """
        from freeagent.cli import render

        app, clock, role = env
        _two_contending(app, role)
        result = _weigh(app, clock)

        first = app.decision.record(result, 0)
        again = app.decision.record(result, 0)

        new_text = render.render_weigh_picked(result, 0, is_new=first.is_new)
        same_text = render.render_weigh_picked(result, 0, is_new=again.is_new)

        assert "记下了" in new_text
        assert "已经记过" in same_text
        assert "没有多出一行" in same_text
        # 两句话不能混：重复的那句不能说成「刚记了一条」
        assert "记下了" not in same_text, "重复拍板被说成了新记的一条"
        # 且两句都不评价
        for text in (new_text, same_text):
            for word in ("好选择", "不错", "正确", "明智"):
                assert word not in text, f"回执出现评价措辞「{word}」"

    def test_render_never_emits_markdown(self, env):
        """呈现层**不许有 markdown** —— ``textContent`` 会原样印出星号。

        V1.92（飞书 plain_text）与 V1.107（Web 面板）各踩过一次。
        这条守卫把整个呈现层扫一遍，不只是本轮新加的那几条。
        """
        from freeagent.cli import render

        app, clock, role = env
        a, b = _two_contending(app, role)
        result = _weigh(app, clock)
        app.decision.record(result, 0)
        clock.advance(days=7)
        app.tasks.complete(a.id)
        cs = [app.decision.confront(d) for d in app.decision.due(clock.today())]

        texts = [
            render.render_weigh(result),
            render.render_weigh_picked(result, 0),
            render.render_decisions(cs, today=clock.today()),
            render.render_decision_history(app.decision.history()),
        ]
        for text in texts:
            assert "**" not in text, f"呈现层出现 markdown：{text[:80]}"
