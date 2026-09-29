"""补齐 V1 功能清单里此前未接线的 CLI 命令。

对应文档 13.1 的第 2、3、4、6、8 项，以及 3.4「快速移到另一个角色视图」、
9.5「跟进可更新 follow_up_at」、10.4「拆步骤 / 整理成可用文本」。
"""

from __future__ import annotations

import io
from datetime import date, datetime

import pytest

from freeagent.app import App
from freeagent.cli.app import KIND_ALIASES, Repl
from freeagent.domain import ArtifactStatus, TaskKind, TaskState, WaitingKind
from freeagent.services.sorting import DISCLAIMER_MARKER


@pytest.fixture()
def repl(app: App, roles):
    """只返回 Repl；输出缓冲从 ``repl.out`` 取。"""
    return Repl(app, out=io.StringIO())


def _run(repl: Repl, *lines: str) -> str:
    """驱动 Repl 并返回**本次**的输出（先清空缓冲，避免跨次累积）。"""
    buf = repl.out
    buf.truncate(0)
    buf.seek(0)
    for line in lines:
        repl.handle(line)
    return buf.getvalue()


def _task(app: App, roles, title="一件事", **kw) -> str:
    return app.tasks.create(title, [roles["work"].id], **kw).id


# =============================================================================
# 13.1-4 状态与调度：暂停 / 改形状（此前 CLI 完全缺失）
# =============================================================================
class TestPauseAndKind:
    def test_pause_moves_active_to_inbox(self, app: App, roles, repl):
        tid = _task(app, roles)
        repl.handle(f"/start {tid[:8]}")
        out = _run(repl, f"/pause {tid[:8]}")
        assert "待办" in out
        assert app.task_repo.get(tid).state is TaskState.INBOX

    def test_pause_requires_id(self, repl):
        repl.handle("/pause")
        assert "用法" in repl.out.getvalue()

    @pytest.mark.parametrize(
        "key,expected",
        [("action", TaskKind.ACTION), ("动作", TaskKind.ACTION),
         ("reminder", TaskKind.REMINDER), ("提醒", TaskKind.REMINDER)],
    )
    def test_kind_change_to_non_wait(self, app: App, roles, repl, key, expected):
        tid = _task(app, roles)
        out = _run(repl, f"/kind {tid[:8]} {key}")
        assert expected.label in out
        assert app.task_repo.get(tid).kind is expected

    def test_kind_change_to_wait_creates_blocked(self, app: App, roles, repl):
        tid = _task(app, roles)
        out = _run(repl, f"/kind {tid[:8]} wait 客户回复")
        got = app.task_repo.get(tid)
        assert got.kind is TaskKind.WAIT
        assert got.state is TaskState.BLOCKED
        assert got.waiting_on.who_or_what == "客户回复"
        assert "等候" in out

    def test_kind_wait_without_target_is_rejected(self, app: App, roles, repl):
        tid = _task(app, roles)
        out = _run(repl, f"/kind {tid[:8]} wait")
        assert "在等什么" in out
        assert app.task_repo.get(tid).kind is TaskKind.ACTION

    def test_kind_change_to_action_clears_waiting(self, app: App, roles, repl):
        """改成动作类就不再是「在等」，waiting_on 必须清空（否则撞不变式 1）。"""
        from freeagent.domain import WaitingOn

        tid = _task(app, roles)
        app.tasks.block(
            tid, WaitingOn(WaitingKind.PERSON, "已有对象", app.clock.now())
        )
        out = _run(repl, f"/kind {tid[:8]} action")
        got = app.task_repo.get(tid)
        assert got.kind is TaskKind.ACTION
        assert got.waiting_on is None
        assert got.state is TaskState.INBOX
        assert "动作" in out
        # 清空后想再改回等候类，必须重新说明在等什么
        out2 = _run(repl, f"/kind {tid[:8]} wait")
        assert "在等什么" in out2

    def test_kind_wait_reuses_existing_waiting_on(self, app: App, roles, repl):
        """BLOCKED 事务上直接 /kind wait：沿用已有 waiting_on，不追问。"""
        from freeagent.domain import WaitingOn

        tid = _task(app, roles, kind=TaskKind.WAIT,
                    waiting_on=WaitingOn(WaitingKind.PERSON, "客户", app.clock.now()))
        out = _run(repl, f"/kind {tid[:8]} wait")
        assert "在等什么" not in out
        assert app.task_repo.get(tid).waiting_on.who_or_what == "客户"

    def test_kind_bad_value_is_rejected(self, app: App, roles, repl):
        tid = _task(app, roles)
        out = _run(repl, f"/kind {tid[:8]} nonsense")
        assert "形状只能是" in out

    def test_kind_alias_table_is_complete(self):
        assert set(KIND_ALIASES) == {
            "action", "动作", "wait", "等候", "reminder", "提醒"
        }
        assert set(KIND_ALIASES.values()) == set(TaskKind)


# =============================================================================
# 13.1-2 角色管理：改名 / 静置 / 删除（此前 CLI 完全缺失）
# =============================================================================
class TestRoleManagementCommands:
    def test_rename(self, app: App, roles, repl):
        out = _run(repl, "/role-rename 家庭 家庭与孩子")
        assert "已改名" in out
        assert app.roles.get_by_name("家庭与孩子").id == roles["family"].id
        assert app.roles.find_by_name("家庭") is None

    def test_rename_requires_new_name(self, repl):
        repl.handle("/role-rename 家庭")
        assert "用法" in repl.out.getvalue()

    def test_rename_missing_role(self, repl):
        out = _run(repl, "/role-rename 没有这个 别的")
        assert "找不到角色" in out

    def test_silence_defaults_on(self, app: App, roles, repl):
        out = _run(repl, "/role-silence 跑腿杂项")
        assert "静置" in out
        assert app.roles.get_by_name("跑腿杂项").active is False

    def test_silenced_role_disappears_from_default_listing(self, app: App, roles, repl):
        repl.handle("/role-silence 跑腿杂项")
        out = _run(repl, "/roles")
        assert "跑腿杂项" not in out

    def test_silenced_role_data_survives(self, app: App, roles, repl):
        tid = app.tasks.create("家里的", [roles["family"].id]).id
        repl.handle("/role-silence 家庭")
        assert app.task_repo.get(tid).title == "家里的"
        assert app.roles.tasks_in(roles["family"].id)

    def test_unsilence(self, app: App, roles, repl):
        _run(repl, "/role-silence 家庭", "/role-silence 家庭 on")
        assert app.roles.get_by_name("家庭").active is True

    def test_silence_bad_flag(self, app: App, roles, repl):
        out = _run(repl, "/role-silence 家庭 maybe")
        assert "用法" in out

    def test_delete_empty_role(self, app: App, roles, repl):
        out = _run(repl, "/role-del 跑腿杂项")
        assert "已删除" in out
        assert app.roles.find_by_name("跑腿杂项") is None

    def test_delete_role_with_tasks_is_blocked_with_guidance(self, app: App, roles, repl):
        app.tasks.create("挂着的事", [roles["family"].id])
        out = _run(repl, "/role-del 家庭")
        assert "不能删" in out
        assert "/merge" in out and "/move" in out
        assert app.roles.find_by_name("家庭") is not None

    def test_delete_merged_role_conflicts(self, app: App, roles, repl):
        app.roles.merge(roles["family"].id, roles["work"].id)
        out = _run(repl, "/role-del 家庭")
        assert "找不到角色" in out or "不能删" in out or "冲突" in out

    def test_delete_requires_arg(self, repl):
        repl.handle("/role-del")
        assert "用法" in repl.out.getvalue()


# =============================================================================
# 3.4 角色归属：跨脉络移动 / 摘标签（此前 CLI 完全缺失）
# =============================================================================
class TestRoleAssignment:
    def test_move_to_single_role(self, app: App, roles, repl):
        tid = app.tasks.create("跨脉络", [roles["work"].id, roles["family"].id]).id
        out = _run(repl, f"/move {tid[:8]} 跑腿杂项")
        assert "只属于" in out
        assert app.task_repo.get(tid).role_ids == (roles["errand"].id,)

    def test_unrole_removes_tag(self, app: App, roles, repl):
        tid = app.tasks.create("跨脉络", [roles["work"].id, roles["family"].id]).id
        out = _run(repl, f"/unrole {tid[:8]} 家庭")
        assert app.task_repo.get(tid).role_ids == (roles["work"].id,)
        assert "工作项目A" in out

    def test_unrole_last_role_is_rejected(self, app: App, roles, repl):
        tid = _task(app, roles)
        out = _run(repl, f"/unrole {tid[:8]} 工作项目A")
        assert "至少属于一个角色" in out
        assert app.task_repo.get(tid).role_ids == (roles["work"].id,)

    def test_move_missing_role(self, app: App, roles, repl):
        tid = _task(app, roles)
        out = _run(repl, f"/move {tid[:8]} 不存在")
        assert "找不到角色" in out


# =============================================================================
# 13.1-6 等候跟进：follow_up_at 可更新（文档 9.5 要求）
# =============================================================================
class TestFollowup:
    def _blocked(self, app: App, roles) -> str:
        from freeagent.domain import WaitingOn

        return app.tasks.create(
            "等客户", [roles["work"].id], kind=TaskKind.WAIT,
            waiting_on=WaitingOn(WaitingKind.PERSON, "客户", app.clock.now()),
        ).id

    def test_followup_sets_datetime(self, app: App, roles, repl):
        tid = self._blocked(app, roles)
        out = _run(repl, f"/followup {tid[:8]} 明天上午十点")
        assert "该跟进" in out
        got = app.task_repo.get(tid)
        assert got.waiting_on.follow_up_at is not None
        assert got.waiting_on.follow_up_at.date() == date(2026, 9, 27)
        assert got.waiting_on.follow_up_at.hour == 10

    def test_followup_off_clears(self, app: App, roles, repl):
        tid = self._blocked(app, roles)
        _run(repl, f"/followup {tid[:8]} 明天")
        out = _run(repl, f"/followup {tid[:8]} off")
        assert "已取消" in out
        assert app.task_repo.get(tid).waiting_on.follow_up_at is None

    def test_followup_date_only_defaults_to_nine(self, app: App, roles, repl):
        tid = self._blocked(app, roles)
        _run(repl, f"/followup {tid[:8]} 3天后")
        got = app.task_repo.get(tid)
        assert got.waiting_on.follow_up_at == datetime(2026, 9, 29, 9, 0)

    def test_followup_on_non_waiting_task_is_rejected(self, app: App, roles, repl):
        tid = _task(app, roles)
        out = _run(repl, f"/followup {tid[:8]} 明天")
        assert "没有在等什么" in out
        assert "/wait" in out

    def test_followup_appends_record(self, app: App, roles, repl):
        tid = self._blocked(app, roles)
        _run(repl, f"/followup {tid[:8]} 明天")
        assert any("跟进 客户" in rec.content for rec in app.record_repo.list_for_task(tid))

    def test_followup_drives_overdue_wait_signal(self, app: App, roles, repl, clock):
        from freeagent.services.sorting import compute_signals

        tid = self._blocked(app, roles)
        repl.handle(f"/followup {tid[:8]} 明天")
        clock.advance(days=2)
        codes = [
            s.code.value
            for s in compute_signals(app.task_repo.get(tid), clock.now(), clock.today())
        ]
        assert "overdue_wait" in codes

    def test_followup_bad_text(self, app: App, roles, repl):
        tid = self._blocked(app, roles)
        out = _run(repl, f"/followup {tid[:8]} 下个月底")
        assert "看不懂的时间" in out


# =============================================================================
# 13.1-8 草稿辅助：draft / revise / artifact / accept（此前一条命令都没有）
# =============================================================================
class TestDraftCommands:
    def test_draft_creates_version_one(self, app: App, roles, repl):
        tid = _task(app, roles, "写周报", intent="先理一版")
        out = _run(repl, f"/draft {tid[:8]}")
        assert "已出草稿 version 1" in out
        assert "[TODO]" in out
        art = app.artifacts.current(tid)
        assert art.version == 1 and art.status is ArtifactStatus.DRAFT

    def test_draft_accepts_instruction(self, app: App, roles, repl):
        tid = _task(app, roles, "写周报")
        out = _run(repl, f"/draft {tid[:8]} 重点讲增长")
        assert "重点讲增长" in out
        assert "重点讲增长" in app.artifacts.current(tid).content

    def test_draft_moves_task_to_active(self, app: App, roles, repl):
        tid = _task(app, roles)
        repl.handle(f"/draft {tid[:8]}")
        assert app.task_repo.get(tid).state is TaskState.INBOX, "起草不等于开始做"

    def test_revise_creates_new_version_and_keeps_old(self, app: App, roles, repl):
        tid = _task(app, roles)
        _run(repl, f"/draft {tid[:8]}")
        out = _run(repl, f"/revise {tid[:8]} 改过的内容")
        assert "version 2" in out
        assert "旧版保留" in out
        versions = app.artifacts.list_for_task(tid)
        assert [a.version for a in versions] == [1, 2]
        assert versions[0].content != versions[1].content

    def test_revise_without_draft_is_rejected(self, app: App, roles, repl):
        tid = _task(app, roles)
        out = _run(repl, f"/revise {tid[:8]} 内容")
        assert "还没有草稿" in out
        assert "/draft" in out

    def test_artifact_list_shows_chain(self, app: App, roles, repl):
        tid = _task(app, roles)
        _run(repl, f"/draft {tid[:8]}", f"/revise {tid[:8]} v2", f"/revise {tid[:8]} v3")
        out = _run(repl, f"/artifact {tid[:8]}")
        assert "v1" in out and "v2" in out and "v3" in out
        assert "旧版全部保留" in out

    def test_artifact_single_version(self, app: App, roles, repl):
        tid = _task(app, roles)
        _run(repl, f"/draft {tid[:8]}")
        out = _run(repl, f"/artifact {tid[:8]} 1")
        assert "version 1" in out

    def test_artifact_missing_version(self, app: App, roles, repl):
        tid = _task(app, roles)
        _run(repl, f"/draft {tid[:8]}")
        out = _run(repl, f"/artifact {tid[:8]} 9")
        assert "没有 version 9" in out

    def test_artifact_bad_version_number(self, app: App, roles, repl):
        tid = _task(app, roles)
        _run(repl, f"/draft {tid[:8]}")
        out = _run(repl, f"/artifact {tid[:8]} abc")
        assert "版本号必须是整数" in out

    def test_artifact_when_empty(self, app: App, roles, repl):
        tid = _task(app, roles)
        out = _run(repl, f"/artifact {tid[:8]}")
        assert "还没有草稿" in out

    def test_accept_current(self, app: App, roles, repl):
        tid = _task(app, roles)
        _run(repl, f"/draft {tid[:8]}")
        out = _run(repl, f"/accept {tid[:8]}")
        assert "已采纳 version 1" in out
        assert app.artifacts.accepted(tid).version == 1

    def test_accept_specific_version_supersedes_previous(self, app: App, roles, repl):
        tid = _task(app, roles)
        _run(repl, f"/draft {tid[:8]}")
        _run(repl, f"/accept {tid[:8]} 1")
        _run(repl, f"/revise {tid[:8]} 第二版")
        out = _run(repl, f"/accept {tid[:8]} 2")
        assert "已采纳 version 2" in out
        assert app.artifacts.accepted(tid).version == 2
        assert app.artifact_repo.get_by_version(tid, 1).status is ArtifactStatus.SUPERSEDED

    def test_accept_without_draft_is_rejected(self, app: App, roles, repl):
        tid = _task(app, roles)
        out = _run(repl, f"/accept {tid[:8]}")
        assert "还没有草稿" in out

    def test_accept_missing_version(self, app: App, roles, repl):
        tid = _task(app, roles)
        _run(repl, f"/draft {tid[:8]}")
        out = _run(repl, f"/accept {tid[:8]} 7")
        assert "没有 version 7" in out

    def test_draft_workflow_restores_cleanly(self, app: App, roles, repl):
        """文档示例三：起草 → 中断 → 恢复 → 局部重做 → 采纳。"""
        tid = _task(app, roles, "下周二要交的销售周报初稿", intent="先理一版")
        _run(repl, f"/draft {tid[:8]}")
        repl.handle(f"/start {tid[:8]}")
        repl.handle(f"/note {tid[:8]} 草稿写到有了框架，还没填数据")
        view = app.restore.open_task(tid)
        assert view.current_artifact.version == 1
        assert any("version 1" in a for a in view.next_actions)
        _run(repl, f"/revise {tid[:8]} 补上数据", f"/accept {tid[:8]}")
        assert app.artifacts.accepted(tid).version == 2


# =============================================================================
# 10.4 助手产出：拆步骤 / 排期参考（此前完全没接线）
# =============================================================================
class TestAssistantOutput:
    def test_steps_splits(self, app: App, roles, repl):
        tid = _task(
            app, roles, "写季度总结",
            intent="先收集数据，然后写初稿，接着找人review，最后定稿",
        )
        out = _run(repl, f"/steps {tid[:8]}")
        assert "拆成" in out
        assert "只拆，不排序" in out
        assert "1." in out and "2." in out

    def test_plan_without_signals(self, app: App, roles, repl):
        tid = app.tasks.create("安静的事", [roles["family"].id])
        app.tasks.complete(tid.id)  # 终态无信号
        out = _run(repl, f"/plan {tid.id[:8]}")
        assert "没有命中任何提示信号" in out

    def test_plan_lists_signal_reasons(self, app: App, roles, repl):
        tid = _task(app, roles)
        app.tasks.schedule(tid, date(2026, 9, 26))
        out = _run(repl, f"/plan {tid[:8]}")
        assert "排在今天" in out
        assert "权重" in out

    def test_plan_declares_disclaimer(self, app: App, roles, repl):
        tid = _task(app, roles)
        app.tasks.schedule(tid, date(2026, 9, 26))
        out = _run(repl, f"/plan {tid[:8]}")
        assert DISCLAIMER_MARKER in out

    def test_plan_does_not_issue_verdict(self, app: App, roles, repl):
        tid = _task(app, roles)
        app.tasks.schedule(tid, date(2026, 9, 26))
        out = _run(repl, f"/plan {tid[:8]}")
        assert "由你决定" in out


# =============================================================================
# 13.1-3 视图：所有事务视图要能看到已结束的
# =============================================================================
class TestAllScopes:
    @pytest.fixture(autouse=True)
    def _seed(self, app: App, roles):
        self.open_task = _task(app, roles, "还开着")
        self.closed = _task(app, roles, "已完成")
        app.tasks.complete(self.closed)

    def test_default_is_open_only(self, app: App, repl):
        out = _run(repl, "/all")
        assert "全部未结束事务" in out
        assert "还开着" in out
        assert "已完成" not in out

    def test_closed_scope(self, app: App, repl):
        out = _run(repl, "/all closed")
        assert "已结束事务" in out
        assert "已完成" in out
        assert "还开着" not in out

    def test_all_scope(self, app: App, repl):
        out = _run(repl, "/all all")
        assert "全部事务" in out
        assert "还开着" in out and "已完成" in out

    def test_bad_scope(self, app: App, repl):
        out = _run(repl, "/all nope")
        assert "用法" in out

    def test_closed_scope_keeps_disclaimer(self, app: App, repl):
        assert DISCLAIMER_MARKER in _run(repl, "/all closed")


# =============================================================================
# 事务级完成标准（原则 4 的另一半）
# =============================================================================
class TestTaskLevelDod:
    def test_dod_sets(self, app: App, roles, repl):
        tid = _task(app, roles)
        out = _run(repl, f"/dod {tid[:8]} 能给领导看")
        assert "能给领导看" in out
        assert app.task_repo.get(tid).definition_of_done == "能给领导看"

    def test_dod_overrides_role_default_in_restore_view(self, app: App, roles, repl):
        tid = _task(app, roles)
        repl.handle(f"/dod {tid[:8]} 能给领导看")
        view = app.restore.open_task(tid)
        assert view.effective_definition_of_done == "能给领导看"

    def test_dod_off_reverts_to_role_default(self, app: App, roles, repl):
        tid = _task(app, roles)
        _run(repl, f"/dod {tid[:8]} 能给领导看", f"/dod {tid[:8]} off")
        assert app.restore.open_task(tid).effective_definition_of_done == "先给我能用的就行"

    def test_dod_clears_missing_completion_hint(self, app: App, roles, repl):
        task = app.tasks.create("x", [roles["family"].id])  # 家庭无默认 DoD
        repl.handle(f"/start {task.id[:8]}")
        assert any("做到什么程度算完" in a for a in app.restore.open_task(task.id).next_actions)
        repl.handle(f"/dod {task.id[:8]} 能交差")
        assert not any(
            "做到什么程度算完" in a
            for a in app.restore.open_task(task.id).next_actions
        )


# =============================================================================
# 帮助与用法提示
# =============================================================================
class TestHelpSurface:
    def test_help_lists_every_command(self, repl):
        from freeagent.cli.app import _USAGE

        out = _run(repl, "/help")
        for cmd in _USAGE:
            assert cmd in out, f"{cmd} 未出现在 /help"

    def test_every_handler_has_usage_text(self, app: App):
        from freeagent.cli.app import _USAGE

        instance = Repl(app, out=io.StringIO())
        for cmd in instance._handlers():
            assert cmd in _USAGE, f"{cmd} 缺少用法文本"

    def test_usage_shown_for_missing_args(self, app: App, roles):
        for cmd in ("/role", "/new", "/task", "/start", "/pause", "/done", "/drop",
                    "/kind", "/wait", "/followup", "/move", "/unrole", "/dod",
                    "/today-pin", "/note", "/draft", "/revise", "/artifact",
                    "/accept", "/steps", "/plan", "/role-add", "/role-rename",
                    "/role-silence", "/role-dod", "/role-del", "/merge"):
            instance = Repl(app, out=io.StringIO())
            instance.handle(cmd)
            assert "用法" in instance.out.getvalue(), f"{cmd} 未给用法提示"
