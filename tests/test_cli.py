"""CLI 对话循环测试：直接驱动 Repl.handle() 并断言输出。"""

from __future__ import annotations

import io
from datetime import date, datetime

import pytest

from freeagent.app import App
from freeagent.cli.app import Repl


@pytest.fixture()
def repl(app: App, roles):
    buf = io.StringIO()
    return Repl(app, out=buf), buf


def _run(repl, buf, *lines: str) -> str:
    for line in lines:
        repl.handle(line)
    return buf.getvalue()


def test_help_lists_commands(repl):
    r, buf = repl
    r.handle("/help")
    out = buf.getvalue()
    for cmd in ("/today", "/all", "/roles", "/task", "/merge", "/tick"):
        assert cmd in out


def test_unknown_command_is_handled(repl):
    r, buf = repl
    r.handle("/nope")
    assert "未知命令" in buf.getvalue()


def test_quit_returns_false(repl):
    r, _ = repl
    assert r.handle("/quit") is False


def test_empty_role_list_message(app: App):
    from freeagent.cli import render

    assert "还没有角色" in render.render_roles([])


def test_natural_language_creates_task(repl):
    r, buf = repl
    out = _run(r, buf, "下周二要交的销售周报初稿，先理一版")
    assert "已记下" in out
    assert "下周二要交的销售周报初稿" in out
    assert "工作项目A" in out
    assert "已排到 2026-09-29" in out
    tasks = [t for t in r.app.task_repo.list_all()]
    assert len(tasks) == 1
    assert tasks[0].kind.value == "action"


def test_natural_language_detects_reminder(repl):
    r, buf = repl
    out = _run(r, buf, "别忘了提醒我下午三点修窗户螺丝")
    assert "提醒" in out
    task = r.app.task_repo.list_all()[0]
    assert task.kind.value == "reminder"
    assert task.reminder_time == datetime(2026, 9, 26, 15, 0)


def test_natural_language_detects_wait(repl):
    """角色推断不出来时先问一句 —— 回答后才建（设计文档 10.1）。"""
    r, buf = repl
    out = _run(r, buf, "等对方回复合同条款")
    assert "里面吗" in out or "脉络" in out
    assert r.app.task_repo.list_all() == []
    out2 = _run(r, buf, "工作项目A")
    assert "已记下" in out2
    assert "等候" in out2
    task = r.app.task_repo.list_all()[0]
    assert task.kind.value == "wait"
    assert task.state.value == "blocked"
    assert "在等" in out2


def test_ambiguous_input_triggers_followup(repl):
    r, buf = repl
    out = _run(r, buf, "随便弄一下")
    assert "脉络" in out or "里面吗" in out
    assert r.app.task_repo.list_all() == []
    out2 = _run(r, buf, "跑腿杂项")
    assert "已记下" in out2
    assert r.app.task_repo.list_all()[0].role_ids == (
        r.app.roles.get_by_name("跑腿杂项").id,
    )


def test_skip_creates_uncategorized(repl):
    r, buf = repl
    _run(r, buf, "随便弄一下", "/skip")
    assert "未分类" in buf.getvalue()
    assert r.app.task_repo.list_all()[0].role_ids == (
        r.app.roles.get_by_name("未分类").id,
    )


def test_new_command_skips_inference(repl):
    r, buf = repl
    out = _run(r, buf, "/new 家庭 明天要交的材料")
    assert "已记下" in out
    assert "明天要交的材料" in out


def test_today_view_renders_disclaimer(repl):
    r, buf = repl
    _run(r, buf, "/new 工作项目A 今天要交的周报")
    out = _run(r, buf, "/today")
    assert "启发式提示，不是评分" in out
    assert "助手不替你决定先做哪个" in out


def test_today_view_reports_rollover(repl):
    r, buf = repl
    task = r.app.tasks.create("上周的事", [r.app.roles.get_by_name("家庭").id])
    r.app.tasks.schedule(task.id, date(2026, 9, 20))
    out = _run(r, buf, "/today")
    assert "顺延" in out
    assert "⟲顺延" in out


def test_task_command_shows_restore_contract(repl):
    r, buf = repl
    task = r.app.tasks.create(
        "写周报", [r.app.roles.get_by_name("工作项目A").id], intent="先理一版"
    )
    r.app.tasks.start(task.id)
    r.app.artifacts.create_draft(task.id, "初稿", "# 框架")
    out = _run(r, buf, f"/task {task.id}")
    assert "生效完成标准" in out
    assert "version 1" in out
    assert "下一步：" in out


def test_task_accepts_id_prefix(repl):
    r, buf = repl
    task = r.app.tasks.create("写周报", [r.app.roles.get_by_name("工作项目A").id])
    out = _run(r, buf, f"/task {task.id[:8]}")
    assert "写周报" in out


def test_task_not_found_is_reported(repl):
    r, buf = repl
    out = _run(r, buf, "/task deadbeef")
    assert "找不到事务" in out


def test_state_transitions_via_commands(repl):
    r, buf = repl
    task = r.app.tasks.create("写周报", [r.app.roles.get_by_name("工作项目A").id])
    out = _run(r, buf, f"/start {task.id[:8]}", f"/done {task.id[:8]}")
    assert "进行中" in out
    assert "已完成" in out
    assert r.app.task_repo.get(task.id).state.value == "done"


def test_wait_command_records_waiting_on(repl):
    r, buf = repl
    task = r.app.tasks.create("写周报", [r.app.roles.get_by_name("工作项目A").id])
    out = _run(r, buf, f"/wait {task.id[:8]} 客户回复")
    assert "已放一放" in out
    got = r.app.task_repo.get(task.id)
    assert got.state.value == "blocked"
    assert got.waiting_on.who_or_what == "客户回复"


def test_pin_and_unpin(repl):
    r, buf = repl
    task = r.app.tasks.create("写周报", [r.app.roles.get_by_name("工作项目A").id])
    out = _run(r, buf, f"/today-pin {task.id[:8]}")
    assert "2026-09-26" in out
    out2 = _run(r, buf, f"/today-pin {task.id[:8]} off")
    assert "移出排期" in out2
    assert r.app.task_repo.get(task.id).scheduled_for is None


def test_pin_with_explicit_date(repl):
    r, buf = repl
    task = r.app.tasks.create("写周报", [r.app.roles.get_by_name("工作项目A").id])
    out = _run(r, buf, f"/today-pin {task.id[:8]} 2026-10-01")
    assert "2026-10-01" in out


def test_pin_with_bad_date_is_reported(repl):
    r, buf = repl
    task = r.app.tasks.create("写周报", [r.app.roles.get_by_name("工作项目A").id])
    out = _run(r, buf, f"/today-pin {task.id[:8]} 下个月底")
    assert "看不懂的日期" in out


def test_note_command(repl):
    r, buf = repl
    task = r.app.tasks.create("写周报", [r.app.roles.get_by_name("工作项目A").id])
    out = _run(r, buf, f"/note {task.id[:8]} 写到一半了")
    assert "已记" in out
    assert "写到一半了" in r.app.task_repo.get(task.id).progress_note


def test_merge_command(repl):
    r, buf = repl
    task = r.app.tasks.create("跨脉络", [r.app.roles.get_by_name("家庭").id])
    out = _run(r, buf, "/merge 家庭 工作项目A")
    assert "并入" in out
    assert "重指向 1 条" in out
    assert r.app.task_repo.get(task.id).role_ids == (
        r.app.roles.get_by_name("工作项目A").id,
    )


def test_merge_missing_role_is_reported(repl):
    r, buf = repl
    out = _run(r, buf, "/merge 不存在A 工作项目A")
    assert "不存在" in out


def test_role_detail_lists_tasks(repl):
    r, buf = repl
    r.app.tasks.create("家里的事", [r.app.roles.get_by_name("家庭").id])
    out = _run(r, buf, "/role 家庭")
    assert "家里的事" in out


def test_role_detail_by_id(repl):
    r, buf = repl
    role = r.app.roles.get_by_name("家庭")
    out = _run(r, buf, f"/role {role.id[:8]}")
    assert "家庭" in out


def test_role_detail_missing(repl):
    r, buf = repl
    out = _run(r, buf, "/role 没有这个角色")
    assert "找不到角色" in out


def test_roles_command_shows_default_dod(repl):
    r, buf = repl
    out = _run(r, buf, "/roles")
    assert "工作项目A" in out
    assert "先给我能用的就行" in out


def test_all_command(repl):
    r, buf = repl
    r.app.tasks.create("一件事", [r.app.roles.get_by_name("家庭").id])
    out = _run(r, buf, "/all")
    assert "一件事" in out
    assert "启发式提示，不是评分" in out


def test_reminder_digest_printed_on_next_input(repl, clock):
    from freeagent.domain import TaskKind

    r, buf = repl
    r.app.tasks.create(
        "修窗户", [r.app.roles.get_by_name("跑腿杂项").id],
        kind=TaskKind.REMINDER,
        reminder_time=datetime(2026, 9, 26, 15, 0),
    )
    clock.set(datetime(2026, 9, 26, 15, 5))
    out = _run(r, buf, "/tick")
    assert "修窗户" in out
    # 再输入一次不应重复推送
    buf.truncate(0)
    buf.seek(0)
    out2 = _run(r, buf, "/roles")
    assert "修窗户" not in out2


def test_tick_without_reminders(repl):
    r, buf = repl
    out = _run(r, buf, "/tick")
    assert "没有到点的提醒" in out


def test_role_add_creates_with_note(repl):
    r, buf = repl
    out = _run(r, buf, "/role-add 新脉络 关键词 甲 乙")
    assert "已建角色「新脉络」" in out
    assert "甲 乙" in out


def test_role_add_updates_existing_note(repl):
    r, buf = repl
    out = _run(r, buf, "/role-add 家庭 新的关键词")
    assert "已更新" in out
    assert r.app.roles.get_by_name("家庭").note == "新的关键词"


def test_role_dod_sets_default(repl):
    r, buf = repl
    out = _run(r, buf, "/role-dod 家庭 先给我能用的就行")
    assert "先给我能用的就行" in out
    assert r.app.roles.get_by_name("家庭").default_definition_of_done == "先给我能用的就行"


def test_role_dod_off_clears(repl):
    r, buf = repl
    _run(r, buf, "/role-dod 家庭 先给我能用的就行", "/role-dod 家庭 off")
    assert r.app.roles.get_by_name("家庭").default_definition_of_done is None


def test_role_dod_missing_role(repl):
    r, buf = repl
    out = _run(r, buf, "/role-dod 没有这个 标准")
    assert "找不到角色" in out


def test_new_strips_pipe_separator(repl):
    r, buf = repl
    out = _run(r, buf, "/new 家庭 | 孩子的材料")
    assert "已记下：孩子的材料" in out


def test_new_requires_description(repl):
    r, buf = repl
    out = _run(r, buf, "/new 家庭 |")
    assert "描述不能为空" in out


def test_slash_command_cancels_pending_question(repl):
    r, buf = repl
    _run(r, buf, "随便弄一下")
    assert r._pending is not None
    out = _run(r, buf, "/roles")
    assert "已取消这次追问" in out
    assert r.app.roles.list_roles() == list(r.app.roles.list_roles())
    assert r._pending is None


def test_no_roles_auto_creates_default(app: App):
    """没有任何角色时不该追问「是哪个脉络」—— 直接建默认脉络。"""
    buf = io.StringIO()
    r = Repl(app, out=buf)
    assert app.roles.list_roles() == []
    r.handle("写点东西")
    out = buf.getvalue()
    assert "默认脉络" in out
    assert app.task_repo.list_all()[0].role_ids == (
        app.roles.get_by_name("默认脉络").id,
    )


def test_command_without_args_shows_usage(repl):
    r, buf = repl
    for cmd in ("/role", "/new", "/task", "/wait", "/note", "/merge", "/today-pin",
                "/role-add", "/role-dod"):
        r.handle(cmd)
    out = buf.getvalue()
    assert out.count("用法") >= 9
