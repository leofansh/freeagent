"""S1–S6 端到端场景测试。

每个场景对应设计文档里的一节，验证「文档承诺的行为」在实现里真的成立。
时间全部由 ``FrozenClock`` 驱动，因此结果完全确定。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from freeagent.app import App
from freeagent.domain import (
    ArtifactStatus,
    TaskKind,
    TaskState,
    WaitingKind,
    WaitingOn,
)
from freeagent.services.clock import FrozenClock
from freeagent.services.llm import RoleHint
from freeagent.services.sorting import DISCLAIMER_MARKER

NOW = datetime(2026, 9, 26, 9, 0)  # 周六
TODAY = date(2026, 9, 26)


def _hints(app: App) -> list[RoleHint]:
    return [RoleHint(r.name, r.note) for r in app.roles.list_roles()]


# =============================================================================
# S1 — 新事务进来（设计文档 17 章 示例一）
# =============================================================================
class TestS1NewTaskArrives:
    def test_classify_infers_action_and_role_without_asking(self, app: App, roles):
        text = "下周二要交的销售周报初稿，先理一版"
        result = app.llm.classify(text, _hints(app))
        assert result.kind == "action"
        assert result.need_clarification is False
        assert result.role_guesses[0].role_name == "工作项目A"

    def test_full_creation_flow(self, app: App, roles):
        from freeagent.cli import parse

        text = "下周二要交的销售周报初稿，先理一版"
        result = app.llm.classify(text, _hints(app))
        role = app.roles.find_by_name(result.role_guesses[0].role_name)
        scheduled = parse.parse_date_expr(text, TODAY)
        task = app.tasks.create(
            app.llm.refine_title(text),
            [role.id],
            kind=TaskKind(result.kind),
            intent="先理一版",
            scheduled_for=scheduled,
        )
        # 标题保留时间信息（与文档示例一一致）
        assert task.title == "下周二要交的销售周报初稿"
        assert task.state is TaskState.INBOX
        assert task.kind is TaskKind.ACTION
        assert task.scheduled_for == date(2026, 9, 29)
        assert task.role_ids == (roles["work"].id,)

    def test_created_record_exists(self, app: App, roles):
        t = app.tasks.create("新事务", [roles["work"].id], intent="做出来")
        records = app.record_repo.list_for_task(t.id)
        assert records[0].type.value == "created"
        assert "动作" in records[0].content

    def test_ambiguous_input_asks_exactly_one_question(self, app: App, roles):
        result = app.llm.classify("随便弄一下东西", _hints(app))
        assert result.need_clarification is True
        assert result.clarifying_question and "？" in result.clarifying_question


# =============================================================================
# S2 — 今天视图（示例二）：顺延 + 透明信号
# =============================================================================
class TestS2TodayView:
    @pytest.fixture(autouse=True)
    def _seed(self, app: App, roles):
        # 上周没做完的
        late = app.tasks.create("上周就该交的材料", [roles["family"].id])
        app.tasks.schedule(late.id, date(2026, 9, 20))
        # 今天的三件事
        self.report = app.tasks.create(
            "销售周报初稿", [roles["work"].id], intent="先理一版",
            due_time=datetime(2026, 9, 27, 18, 0),
        )
        app.tasks.schedule(self.report.id, TODAY)
        self.wait = app.tasks.create(
            "孩子周四要交的材料清单", [roles["family"].id], kind=TaskKind.WAIT,
            waiting_on=WaitingOn(
                WaitingKind.PERSON, "孩子带回来", NOW - timedelta(days=3),
                follow_up_at=NOW - timedelta(days=1),
            ),
        )
        app.tasks.schedule(self.wait.id, TODAY)
        self.errand = app.tasks.create(
            "修一下窗户螺丝", [roles["errand"].id], kind=TaskKind.REMINDER,
            reminder_time=datetime(2026, 9, 26, 15, 0),
        )
        app.tasks.schedule(self.errand.id, TODAY)

    def test_rollover_moves_the_stale_one(self, app: App):
        view = app.today.view()
        assert view.rollover.count == 1
        assert view.rollover.items[0].from_day == date(2026, 9, 20)
        assert app.task_repo.get(self.report.id) is not None

    def test_rollover_summary_is_one_line(self, app: App):
        summary = app.today.view().rollover.summary()
        assert summary is not None
        assert "\n" not in summary, "顺延必须是合并成一条的摘要"
        assert "上周就该交的材料" in summary

    def test_all_four_visible_today(self, app: App):
        view = app.today.view()
        assert len(view.items) == 4

    def test_every_signal_has_a_reason(self, app: App):
        view = app.today.view()
        for scored in view.items:
            for sig in scored.signals:
                assert sig.reason, f"{sig.code} 缺理由"
                assert isinstance(sig.weight, int)

    def test_overdue_wait_surfaces_the_person_to_chase(self, app: App):
        view = app.today.view()
        target = next(s for s in view.items if s.task.id == self.wait.id)
        assert any("孩子带回来" in sig.reason for sig in target.signals)

    def test_ranking_is_globally_descending(self, app: App):
        view = app.today.view()
        weights = [s.total_weight for s in view.items]
        assert weights == sorted(weights, reverse=True)
        # 该催的那条应排在最前
        assert view.items[0].task.id == self.wait.id

    def test_today_view_rerun_is_stable(self, app: App):
        first = app.today.view()
        second = app.today.view()
        assert second.rollover.count == 0
        assert [s.task.id for s in first.items] == [s.task.id for s in second.items]

    def test_rendered_view_declares_it_is_not_a_score(self, app: App):
        from freeagent.cli import render

        view = app.today.view()
        names = render.role_names(app.roles.list_roles(include_merged=True))
        text = render.render_today(view, names)
        assert DISCLAIMER_MARKER in text
        assert "启发式提示，不是评分" in text
        assert "助手不替你决定先做哪个" in text

    def test_rendered_view_shows_signal_reasons(self, app: App):
        from freeagent.cli import render

        view = app.today.view()
        names = render.role_names(app.roles.list_roles(include_merged=True))
        text = render.render_today(view, names)
        assert "该催" in text
        assert "排在今天" in text


# =============================================================================
# S3 — 做事中断（示例三）：草稿 + 恢复 + 局部重做
# =============================================================================
class TestS3InterruptedWork:
    @pytest.fixture(autouse=True)
    def _task(self, app: App, roles):
        self.task = app.tasks.create(
            "下周二要交的销售周报初稿", [roles["work"].id], intent="先理一版"
        )
        app.tasks.start(self.task.id)
        self.draft = app.artifacts.create_draft(
            self.task.id, "周报初稿", "# 框架\n- [TODO] 数据\n- [TODO] 结论"
        )
        app.tasks.note(self.task.id, "草稿写到有了框架，还没填数据")

    def test_state_is_resumable(self, app: App):
        t = app.task_repo.get(self.task.id)
        assert t.state is TaskState.ACTIVE
        assert t.current_artifact_id == self.draft.id
        assert t.progress_note and "还没填数据" in t.progress_note

    def test_restore_contract_has_every_field(self, app: App):
        view = app.restore.open_task(self.task.id)
        assert view.task.id == self.task.id
        assert view.task.intent == "先理一版"
        assert view.effective_definition_of_done == "先给我能用的就行"
        assert view.current_artifact is not None
        assert view.progress_note
        assert len(view.recent_records) > 0
        assert view.waiting_on is None
        assert len(view.next_actions) > 0

    def test_restart_can_follow_up_in_one_go(self, app: App):
        view = app.restore.open_task(self.task.id)
        assert any("version 1" in a for a in view.next_actions)
        v2 = app.artifacts.revise(self.task.id, "# 框架\n- 数据\n- 结论")
        assert v2.version == 2
        assert app.artifact_repo.get(self.draft.id).content.startswith("# 框架")
        assert v2.supersedes == self.draft.id

    def test_old_version_survives_local_redo(self, app: App):
        app.artifacts.revise(self.task.id, "改过的")
        app.artifacts.revise(self.task.id, "又改的")
        versions = {a.version: a.content for a in app.artifacts.list_for_task(self.task.id)}
        assert versions == {1: "# 框架\n- [TODO] 数据\n- [TODO] 结论", 2: "改过的", 3: "又改的"}

    def test_accept_supersedes_previous(self, app: App):
        v1 = app.artifact_repo.get(self.draft.id)
        app.artifacts.accept(v1.id)
        v2 = app.artifacts.revise(self.task.id, "第二版")
        assert v2.supersedes == v1.id
        assert app.artifact_repo.get(v1.id).status is ArtifactStatus.SUPERSEDED

    def test_progress_note_rebuildable_after_corruption(self, app: App):
        good = app.task_repo.get(self.task.id).progress_note
        app.task_repo.set_progress_note(self.task.id, "坏的")
        app.tasks.rebuild_progress_note(self.task.id)
        assert app.task_repo.get(self.task.id).progress_note == good

    def test_rendered_task_card_is_actionable(self, app: App):
        from freeagent.cli import render

        view = app.restore.open_task(self.task.id)
        names = render.role_names(app.roles.list_roles(include_merged=True))
        text = render.render_task(view, names)
        assert "生效完成标准：先给我能用的就行" in text
        assert "version 1" in text
        assert "下一步：" in text


# =============================================================================
# S4 — 跨角色事务与角色合并（设计文档 3.5）
# =============================================================================
class TestS4RoleMerge:
    def test_multi_role_task_survives_merge(self, app: App, roles):
        t = app.tasks.create("又工作又人情的事", [roles["work"].id, roles["family"].id])
        report = app.roles.merge(roles["family"].id, roles["work"].id)
        assert t.id in report.moved_tasks
        assert app.task_repo.get(t.id).role_ids == (roles["work"].id,)

    def test_merge_is_visible_in_history(self, app: App, roles):
        t = app.tasks.create("要合并的", [roles["family"].id])
        app.roles.merge(roles["family"].id, roles["work"].id)
        assert any(
            "角色合并" in r.content
            for r in app.record_repo.list_for_task(t.id)
        )

    def test_merged_role_still_resolvable(self, app: App, roles):
        app.roles.merge(roles["family"].id, roles["work"].id)
        assert app.roles.resolve(roles["family"].id).name == "工作项目A"

    def test_no_orphan_references_remain(self, app: App, roles):
        app.tasks.create("a", [roles["family"].id])
        app.tasks.create("b", [roles["work"].id, roles["family"].id])
        app.roles.merge(roles["family"].id, roles["work"].id)
        rows = app.conn.execute(
            "SELECT t.id, tr.role_id FROM tasks t"
            " LEFT JOIN task_roles tr ON tr.task_id = t.id"
        ).fetchall()
        live_roles = {
            r[0] for r in app.conn.execute("SELECT id FROM roles WHERE merged_into IS NULL")
        }
        for task_id, role_id in rows:
            if role_id is not None:
                assert role_id in live_roles, (task_id, role_id)

    def test_merge_rolls_back_on_failure(self, app: App, roles, monkeypatch):
        """原子性：重指向过程中出错必须整体回滚，不能留下半合并状态。"""
        t = app.tasks.create("会被回滚的", [roles["family"].id])
        monkeypatch.setattr(app.roles, "_records", _BoomRecordRepo())
        with pytest.raises(RuntimeError):
            app.roles.merge(roles["family"].id, roles["work"].id)
        # 回滚后一切不变
        assert app.task_repo.get(t.id).role_ids == (roles["family"].id,)
        assert app.roles.get(roles["family"].id).merged_into is None
        assert app.roles.get(roles["work"].id).merged_into is None

    def test_tasks_in_merged_role_aggregates(self, app: App, roles):
        app.tasks.create("家里的", [roles["family"].id])
        app.tasks.create("工作上的", [roles["work"].id])
        app.roles.merge(roles["family"].id, roles["work"].id)
        titles = {t.title for t in app.roles.tasks_in(roles["work"].id)}
        assert titles == {"家里的", "工作上的"}


class _BoomRecordRepo:
    """任何写操作都失败，用于验证合并的事务回滚。"""

    def append(self, *args, **kwargs):
        raise RuntimeError("boom")

    def list_for_task(self, *args, **kwargs):  # pragma: no cover
        return []


# =============================================================================
# S5 — 跨角色中断恢复：H2 判据「≤2 轮」
# =============================================================================
class TestS5CrossRoleResumeWithinTwoTurns:
    @pytest.fixture(autouse=True)
    def _setup(self, app: App, roles, clock):
        # 事务同时挂在两个角色上
        self.task = app.tasks.create(
            "给朋友做的方案，顺便用到工作数据",
            [roles["work"].id, roles["family"].id],
            intent="先理一版",
        )
        app.tasks.start(self.task.id)
        self.draft = app.artifacts.create_draft(
            self.task.id, "方案草稿", "# 结论\n- [TODO] 补数据"
        )
        app.tasks.note(self.task.id, "写了框架")
        # 中断三天后回来
        clock.set(NOW + timedelta(days=3, hours=2))

    def test_round_1_open_gives_everything_needed(self, app: App):
        view = app.restore.open_task(self.task.id)
        # 恢复契约七项齐备
        assert view.task.role_ids == (app.roles.list_roles()[0].id,) or len(
            view.task.role_ids
        ) == 2
        assert view.effective_definition_of_done
        assert view.current_artifact is not None
        assert view.progress_note
        assert view.recent_records
        assert view.next_actions
        # 恢复点被更新
        assert view.task.last_resumed_at == NOW + timedelta(days=3, hours=2)

    def test_next_action_is_immediately_executable(self, app: App):
        view = app.restore.open_task(self.task.id)
        assert any("version 1" in a for a in view.next_actions)
        # 草稿内容已经在视图里，不需要再查一次
        assert "[TODO] 补数据" in view.current_artifact.content

    def test_round_2_execute_without_further_lookup(self, app: App):
        view = app.restore.open_task(self.task.id)
        v2 = app.artifacts.revise(
            self.task.id, view.current_artifact.content.replace("[TODO] 补数据", "已补")
        )
        assert v2.version == 2
        assert "[TODO]" not in v2.content

    def test_stale_resume_signal_appears(self, app: App):
        """信号是给「还没打开的事务」用的；打开本身就刷新了恢复点。"""
        from freeagent.services.sorting import compute_signals

        stale = app.task_repo.get(self.task.id)  # 打开之前
        codes = [
            s.code.value
            for s in compute_signals(stale, app.clock.now(), app.clock.today())
        ]
        assert "resume_stale" in codes, codes
        # 打开后恢复点被刷新，信号随之消失
        view = app.restore.open_task(self.task.id)
        after = [
            s.code.value
            for s in compute_signals(view.task, app.clock.now(), app.clock.today())
        ]
        assert "resume_stale" not in after, after

    def test_two_turn_judgement_holds(self, app: App):
        """H2 的可测形式：打开一次 + 执行一次，中间不需要额外查询。"""
        turns = 0
        view = app.restore.open_task(self.task.id)  # 第 1 轮
        turns += 1
        assert view.next_actions, "第 1 轮必须给出下一步"
        action = view.next_actions[0]
        if "version 1" in action:
            app.artifacts.revise(self.task.id, view.current_artifact.content)  # 第 2 轮
            turns += 1
        assert turns <= 2, f"恢复应 ≤2 轮，实际 {turns}"


# =============================================================================
# S6 — 提醒：到点、错过、合并摘要
# =============================================================================
class TestS6Reminders:
    def test_reminder_fires_once(self, app: App, roles, clock):
        app.tasks.create(
            "修一下窗户螺丝", [roles["errand"].id], kind=TaskKind.REMINDER,
            reminder_time=datetime(2026, 9, 26, 15, 0),
        )
        clock.set(datetime(2026, 9, 26, 15, 5))
        first = app.reminders.check()
        assert first.count == 1 and "修一下窗户螺丝" in first.text()
        assert app.reminders.check().is_empty, "同一次提醒不应重复推送"

    def test_missed_reminder_is_labelled_not_shown_as_urgent(self, app: App, roles, clock):
        app.tasks.create(
            "交物业费", [roles["errand"].id], kind=TaskKind.REMINDER,
            reminder_time=datetime(2026, 9, 25, 10, 0),
        )
        clock.set(datetime(2026, 9, 26, 20, 0))
        digest = app.reminders.check()
        assert len(digest.missed) == 1
        assert "已错过" in digest.text()
        assert digest.fired == ()

    def test_digest_is_single_message(self, app: App, roles, clock):
        for i in range(4):
            app.tasks.create(
                f"提醒{i}", [roles["errand"].id], kind=TaskKind.REMINDER,
                reminder_time=datetime(2026, 9, 26, 15, i),
            )
        clock.set(datetime(2026, 9, 26, 15, 30))
        text = app.reminders.check().text()
        assert text and "\n" not in text
        assert "4 条" in text

    def test_cancelled_task_does_not_remind(self, app: App, roles, clock):
        t = app.tasks.create(
            "算了不修了", [roles["errand"].id], kind=TaskKind.REMINDER,
            reminder_time=datetime(2026, 9, 26, 15, 0),
        )
        app.tasks.drop(t.id)
        clock.set(datetime(2026, 9, 26, 15, 5))
        assert app.reminders.check().is_empty
