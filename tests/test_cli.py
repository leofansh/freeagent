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


def test_create_hints_registered_relation(repl):
    """提到**已登记**的关系对象，建事务回复顺势提一句可挂账（V1.84）。

    只提示不静默挂（守 11.19）—— 用户知道能挂，但挂不挂由他拍板。
    """
    r, buf = repl
    r.app.promise.contact("张总")
    out = _run(r, buf, "/new 工作项目A 和张总确认合同")
    assert "可挂关系账" in out
    assert "张总" in out


def test_create_skips_unregistered_relation(repl):
    """张总还没登记成关系对象时，**不**提示 —— 我们不猜谁是关系人。"""
    r, buf = repl
    out = _run(r, buf, "/new 工作项目A 和张总确认合同")
    assert "可挂关系账" not in out


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


# -- /accept 的约定提示（V1.95 / 12.10.10）----------------------------------- #

class TestAcceptConventionHint:
    """验收是「审核 → 积累」的入口：只提示，不代记（EXPLICIT_ONLY）。

    静默替人记一条约定，和静默挂关系账（V1.84 摩擦点 3）是同一种越权 ——
    所以这条提示**只在采纳执行器产出时**出现：用户自己写的草稿没有
    「审核机器的工作」这回事。
    """

    def test_executor_output_gets_hint(self, app, repl):
        r, buf = repl
        role = app.roles.create("工作")
        t = app.tasks.create("改登录", [role.id])
        app.artifacts.create_draft(
            t.id, "opencode 产出", "正文", model="opencode/big-pickle"
        )
        out = _run(r, buf, f"/accept {t.id}")
        assert "已采纳 version 1" in out
        assert "/memory add 约定" in out, "验收后该提示可沉淀的出口"

    def test_user_draft_gets_no_hint(self, app, repl):
        r, buf = repl
        role = app.roles.create("工作")
        t = app.tasks.create("改登录", [role.id])
        app.artifacts.create_draft(t.id, "我的方案", "手写正文")
        out = _run(r, buf, f"/accept {t.id}")
        assert "已采纳 version 1" in out
        assert "/memory add 约定" not in out, "没有机器工作被审核，就不该提示"


# -- /ask 带 core 记忆（V1.97 / 3.7.6 注入点①）------------------------------- #

class TestAskCarriesMemory:
    """`/ask` 问执行器前拼上用户记下的 core 记忆。

    治的是「文档说有、代码没有」：3.7.6 登记了注入点①，而 `core_block`
    此前只有测试在调，`/ask` 把原题直接发给执行器。现在接上，三条纪律：

    - 块自带「可能过期，以当前为准」定性（CLI 不另拼也不去掉）；
    - 向用户**明说**带了记忆、超预算几条没带上 —— 不静默；
    - 空记忆不注入也不打印（问的是问题，不是记忆状态）。
    """

    def test_memory_reaches_the_executor(self, repl, monkeypatch):
        from freeagent.services import ask as ask_mod
        r, buf = repl
        r.app.memory.add("回复先给结论", kind="约定")
        seen: dict = {}

        def _fake(question, *, context="", **kw):
            seen["question"] = question
            seen["context"] = context
            return ask_mod.AskOutcome(ok=True, text="答")

        monkeypatch.setattr(ask_mod, "ask_executor", _fake)
        _run(r, buf, "/ask 这个项目是干什么的？")
        assert "回复先给结论" in seen["context"], "约定没带到执行器"
        assert "可能过期" in seen["context"], "定性没了 —— 拿旧约定冒充当前规则"
        assert seen["question"] == "这个项目是干什么的？"

    def test_user_is_told_what_was_carried(self, repl, monkeypatch):
        from freeagent.services import ask as ask_mod
        r, buf = repl
        r.app.memory.add("回复先给结论", kind="约定")
        monkeypatch.setattr(
            ask_mod, "ask_executor",
            lambda q, *, context="", **kw: ask_mod.AskOutcome(ok=True, text="答"),
        )
        out = _run(r, buf, "/ask 你好")
        assert "带上了你记下的记忆" in out, "带了却不说 —— 用户无法核对带了什么"

    def test_over_budget_is_reported_not_silently_truncated(self, repl, monkeypatch):
        from freeagent.services import ask as ask_mod
        r, buf = repl
        for i in range(14):
            r.app.memory.add(f"第 {i} 条", kind="约定")
        seen: dict = {}

        def _fake(question, *, context="", **kw):
            seen["context"] = context
            return ask_mod.AskOutcome(ok=True, text="答")

        monkeypatch.setattr(ask_mod, "ask_executor", _fake)
        out = _run(r, buf, "/ask 你好")
        assert "还有 2 条未带上" in out, f"超预算没说清：{out!r}"
        assert "/memory list" in out, "没说出去路"

    def test_no_memory_means_no_injection_and_no_noise(self, repl, monkeypatch):
        from freeagent.services import ask as ask_mod
        r, buf = repl
        seen: dict = {}

        def _fake(question, *, context="", **kw):
            seen["context"] = context
            return ask_mod.AskOutcome(ok=True, text="答")

        monkeypatch.setattr(ask_mod, "ask_executor", _fake)
        out = _run(r, buf, "/ask 你好")
        assert seen["context"] == "", "一条都没记却注入了东西"
        assert "带上了你记下的记忆" not in out, "空记忆还打印 —— 噪音"


# -- /memory stats：使用留痕（V1.98 / 3.7）---------------------------------- #

class TestMemoryStats:
    """``/memory stats [天数]`` —— 「哪条记忆好久没用了」。

    三条纪律：只摆事实不建议删（3.7.5 的不自动 consolidation）；
    用过但超窗口的说「上次哪天」，**不装作从未用过**；
    留痕本身坏掉时统计显示 0，而不是假装检索没发生过。
    """

    def test_counts_and_lists_usage(self, repl):
        r, buf = repl
        r.app.memory.add("回复先给结论", kind="约定")
        r.app.memory.retrieve("结论", consumer="/memory search")
        r.app.memory.core_block(consumer="/ask")
        out = _run(r, buf, "/memory stats")
        assert "检索 1 次" in out and "注入 1 次" in out
        assert "回复先给结论" in out
        assert "检索 1 · 注入 1" in out

    def test_stale_entry_says_never_used(self, repl):
        r, buf = repl
        r.app.memory.add("我不喜欢早上开会", kind="偏好")
        out = _run(r, buf, "/memory stats")
        assert "没用过" in out
        assert "从未被检索或注入" in out
        assert "删" not in out, "只摆事实，不替人决定删不删"

    def test_used_outside_window_is_not_claimed_never_used(self, repl):
        """三个月前用过 ≠ 从未用过 —— 窗口只管计数，时间不设窗。"""
        r, buf = repl
        r.app.memory.add("回复先给结论", kind="约定")
        r.app.memory.retrieve("结论")
        r.app.clock.advance(days=91)
        out = _run(r, buf, "/memory stats 90")
        assert "没用过" in out
        assert "从未被检索或注入" not in out, "超窗口被说成从未用过 = 撒谎"
        assert "上次" in out

    def test_window_is_configurable(self, repl):
        r, buf = repl
        r.app.memory.add("回复先给结论", kind="约定")
        r.app.memory.retrieve("结论")
        r.app.clock.advance(days=31)
        out = _run(r, buf, "/memory stats 30")
        assert "近 30 天" in out

    def test_bad_days_rejected(self, repl):
        r, buf = repl
        r.app.memory.add("回复先给结论", kind="约定")
        out = _run(r, buf, "/memory stats 九十")
        assert "正整数" in out

    def test_no_memory_says_so(self, repl):
        r, buf = repl
        out = _run(r, buf, "/memory stats")
        assert "还没记过任何记忆" in out

    def test_log_failure_does_not_break_retrieval(self, repl):
        """留痕写不进去时：检索照常，统计显示 0 —— 可被发现，不装没发生。"""
        r, buf = repl
        r.app.memory.add("回复先给结论", kind="约定")
        conn = r.app.memory._conn
        conn.execute("ALTER TABLE retrieval_log RENAME TO retrieval_log_bak")
        conn.commit()
        try:
            out = _run(r, buf, "/memory search 结论")
            assert "关键词命中 1 条" in out, "留痕失败不该让检索失败"
            out2 = _run(r, buf, "/memory stats")
            assert "检索 0 次" in out2
            assert "不可信" in out2, "读不到必须自报，不能装作「没有」"
        finally:
            conn.execute("DROP TABLE IF EXISTS retrieval_log_bak")
            conn.commit()


# -- /ask 的相关度注入（V1.99 / 3.7.6）--------------------------------------- #

class TestAskRelevanceInjection:
    """``/ask`` 把问题当查询词：注入集按相关度选，不一律取最新。

    V1.97 接上注入时一律按时间倒序取最新 N 条。记到三十条之后，与问题
    相关的那条可能早在预算之外。三种 leg 三种说法（``CoreBlock.leg``）：

    - 相关度命中：「与这个问题相关的 N 条」；
    - 落空：「最近的 N 条——相关度没命中」；
    - 相关度那条腿本身坏了：明说「相关度检索不可用」，不装作按相关度选的。
    """

    def _fake_ask(self, monkeypatch):
        from freeagent.services import ask as ask_mod

        seen: dict = {}

        def _fake(question, *, context="", **kw):
            seen["question"] = question
            seen["context"] = context
            return ask_mod.AskOutcome(ok=True, text="答")

        monkeypatch.setattr(ask_mod, "ask_executor", _fake)
        return seen

    def test_question_drives_relevance(self, repl, monkeypatch):
        from freeagent.services.memory import CORE_BUDGET

        r, buf = repl
        r.app.memory.add("本项目禁用 requests", kind="约定")
        for i in range(CORE_BUDGET + 3):
            r.app.memory.add(f"第 {i} 条无关内容", kind="偏好")
        seen = self._fake_ask(monkeypatch)
        out = _run(r, buf, "/ask requests 能用吗")
        assert "本项目禁用 requests" in seen["context"]
        assert "第 0 条无关内容" not in seen["context"], (
            "相关的那条赢了，就不该再塞一打最新的进去"
        )
        assert "与这个问题相关的 1 条" in out

    def test_miss_says_it_fell_back_to_recent(self, repl, monkeypatch):
        r, buf = repl
        r.app.memory.add("回复先给结论", kind="约定")
        seen = self._fake_ask(monkeypatch)
        out = _run(r, buf, "/ask 你好")
        assert "回复先给结论" in seen["context"], "落空不该让注入消失"
        assert "相关度没命中" in out
        assert "最近的 1 条" in out

    def test_degraded_relevance_is_reported(self, repl, monkeypatch):
        import sqlite3

        from freeagent.services.memory import MemoryService

        r, buf = repl
        r.app.memory.add("回复先给结论", kind="约定")

        def _boom(self, *a, **kw):
            raise sqlite3.OperationalError("no such table: memory_fts")

        monkeypatch.setattr(MemoryService, "_retrieve", _boom)
        seen = self._fake_ask(monkeypatch)
        out = _run(r, buf, "/ask 结论")
        assert "回复先给结论" in seen["context"], "相关度坏了不该让注入消失"
        assert "相关度检索不可用" in out, "静默回落 = 把「按相关度选的」说成真的"

    def test_question_is_logged_for_later_review(self, repl, monkeypatch):
        """日志里要能复核「这批是怎么选出来的」—— 不只在一句问候里。"""
        r, buf = repl
        r.app.memory.add("本项目禁用 requests", kind="约定")
        self._fake_ask(monkeypatch)
        _run(r, buf, "/ask requests 能用吗")
        row = r.app.memory._conn.execute(
            "SELECT legs, query FROM retrieval_log"
            " WHERE event = 'inject' ORDER BY created_at DESC, id DESC LIMIT 1"
        ).fetchone()
        assert row[0] == "bm25"
        assert row[1] == "requests 能用吗"


# -- /memory review（V1.100 / 3.7.8）----------------------------------------- #

class TestMemoryReview:
    """``/memory review``：候选怎么摆、确认怎么记。

    纪律：只摆不动（不自动删、不自动 consolidation）；候选带「最后一次
    动静」的日期（用过又超窗口 ≠ 从未有过）；读不出来时不摆候选、也不说
    「没有候选」；相近只说相近不说冲突。
    """

    def test_stale_candidate_listed_with_facts_and_way_out(self, repl):
        r, buf = repl
        r.app.memory.add("我不喜欢早上开会", kind="偏好")
        r.app.clock.advance(days=91)
        out = _run(r, buf, "/memory review")
        assert "好久没动静" in out
        assert "我不喜欢早上开会" in out
        assert "最后一次动静" in out
        assert "/memory review confirm" in out and "/memory del" in out
        assert "绝不自动删" in out

    def test_no_candidates_says_so(self, repl):
        r, buf = repl
        r.app.memory.add("我不喜欢早上开会", kind="偏好")
        out = _run(r, buf, "/memory review")
        assert "没有待整理的候选" in out
        assert "都有过动静" in out

    def test_empty_memory_says_so(self, repl):
        r, buf = repl
        out = _run(r, buf, "/memory review")
        assert "还没记过任何记忆" in out

    def test_confirm_says_what_it_did(self, repl):
        r, buf = repl
        e = r.app.memory.add("我不喜欢早上开会", kind="偏好")
        r.app.clock.advance(days=91)
        out = _run(r, buf, "/memory review", f"/memory review confirm {e.id}")
        assert "好久没动静" in out
        assert "确认留着：我不喜欢早上开会" in out
        assert "退出本轮候选" in out

    def test_confirmed_entry_leaves_the_queue(self, repl):
        r, buf = repl
        e = r.app.memory.add("我不喜欢早上开会", kind="偏好")
        r.app.clock.advance(days=91)
        _run(r, buf, f"/memory review confirm {e.id}")
        out = _run(r, buf, "/memory review")
        assert "没有待整理的候选" in out
        assert "好久没动静" not in out

    def test_confirm_multiple_ids(self, repl):
        r, buf = repl
        a = r.app.memory.add("甲条内容", kind="偏好")
        b = r.app.memory.add("乙条内容", kind="偏好")
        out = _run(r, buf, f"/memory review confirm {a.id} {b.id}")
        assert out.count("确认留着") == 2

    def test_confirm_unknown_id_errors(self, repl):
        r, buf = repl
        out = _run(r, buf, "/memory review confirm no-such-id")
        assert "没找到这条记忆" in out

    def test_confirm_without_id_shows_usage(self, repl):
        r, buf = repl
        out = _run(r, buf, "/memory review confirm")
        assert "用法" in out

    def test_near_duplicate_group_shown_without_claiming_conflict(self, repl):
        r, buf = repl
        r.app.memory.add("回复先给结论", kind="约定")
        r.app.memory.add("回复先给结论，再给细节", kind="约定")
        out = _run(r, buf, "/memory review")
        assert "内容相近" in out
        assert "不一定是冲突" in out
        assert "相近度 62%" in out, "度量要摆出来，可反驳"
        assert "留哪条、怎么改，你定" in out

    def test_bad_days_rejected(self, repl):
        r, buf = repl
        out = _run(r, buf, "/memory review 九十")
        assert "正整数" in out

    def test_window_configurable(self, repl):
        r, buf = repl
        r.app.memory.add("我不喜欢早上开会", kind="偏好")
        r.app.clock.advance(days=31)
        assert "好久没动静" in _run(r, buf, "/memory review 30")
        assert "没有待整理的候选" in _run(r, buf, "/memory review 60")

    def test_unreadable_log_does_not_claim_clean(self, repl):
        """留痕读不出来时不摆候选，也不说「没有候选」。"""
        r, buf = repl
        r.app.memory.add("我不喜欢早上开会", kind="偏好")
        r.app.clock.advance(days=91)
        conn = r.app.memory._conn
        conn.execute("ALTER TABLE retrieval_log RENAME TO retrieval_log_bak")
        conn.commit()
        try:
            out = _run(r, buf, "/memory review")
            assert "读不出来" in out
            assert "没有待整理的候选" not in out
        finally:
            conn.execute("DROP TABLE IF EXISTS retrieval_log_bak")
            conn.commit()


class TestMemoryMinimalPairs:
    """``/memory review`` 的第三组：只差一处（V1.102）。

    呈现的三件事：**差在哪**（不是「相似」这种形容词）、**多少重合**
    （可复算）、**出口在哪**（哪条算数由人定，系统不改不删）。
    """

    def test_group_states_the_diff_and_share(self, repl):
        r, buf = repl
        r.app.memory.add("回复先给结论", kind="约定")
        r.app.memory.add("回复先给细节", kind="约定")
        out = _run(r, buf, "/memory review")
        assert "只差一处的（1 对）" in out
        assert "同一个框架、不同的取值" in out
        # 左右按库里的条目顺序（先摆左边、再摆右边），断「差在哪」时不锁顺序 ——
        # 锁了等于要求「谁先被记下」这件事影响输出，那不是这一组要管的事。
        assert "只差「结论」↔「细节」" in out or "只差「细节」↔「结论」" in out
        assert "首尾重合 67%" in out
        assert "哪条算数、要不要合，你定" in out

    def test_summary_counts_the_third_group(self, repl):
        r, buf = repl
        r.app.memory.add("回复先给结论", kind="约定")
        r.app.memory.add("回复先给细节", kind="约定")
        out = _run(r, buf, "/memory review")
        assert "1 对只差一处" in out

    def test_pairs_alone_are_not_reported_as_clean(self, repl):
        """两条刚记下、字面只差一处 —— 没有「好久没动静」，但不是没事可看。"""
        r, buf = repl
        r.app.memory.add("回复先给结论", kind="约定")
        r.app.memory.add("回复先给细节", kind="约定")
        out = _run(r, buf, "/memory review")
        assert "没有待整理的候选" not in out
        assert "只差一处的（1 对）" in out

    def test_one_side_extension_is_written_as_extension(self):
        """一条是另一条的延长时，写「右边多出」而不是「↔」—— 后者读成换说法。"""
        from types import SimpleNamespace

        pair = SimpleNamespace(left_diff="", right_diff="，再给细节")
        assert Repl._minimal_diff_label(pair) == "右边多出「，再给细节」"
        pair = SimpleNamespace(left_diff="不要", right_diff="")
        assert Repl._minimal_diff_label(pair) == "左边多出「不要」"

    def test_degraded_log_still_shows_pairs(self, repl):
        """留痕坏了：说清哪一组判断不了，两组字面关系照摆（V1.102）。"""
        r, buf = repl
        r.app.memory.add("回复先给结论", kind="约定")
        r.app.memory.add("回复先给细节", kind="约定")
        conn = r.app.memory._conn
        conn.execute("ALTER TABLE retrieval_log RENAME TO retrieval_log_bak")
        conn.commit()
        try:
            out = _run(r, buf, "/memory review")
            assert "判断不了哪些好久没动静" in out
            assert "「内容相近」「只差一处」两组只看正文" in out
            assert "只差一处的（1 对）" in out
        finally:
            conn.execute("DROP TABLE IF EXISTS retrieval_log_bak")
            conn.commit()


# =============================================================================
# /brief —— 简报预览与协作象限（12.10.12 / V1.103）
# =============================================================================
class TestBriefPreview:
    """人第一次能看见「执行器到底收到了什么」。

    这一刀的价值全在**预览与实发逐字相同**上：预览说「没给方案」而实发
    带了方案，比没有预览更坏。所以渲染只做排版，组装走 ``assemble_brief``。
    """

    def _make(self, r):
        return r.app.tasks.create(
            "加个邮箱登录", [r.app.roles.list_roles()[0].id],
            intent="能登录就行",
            project_path="D:/proj/app",
        ).id

    def test_shows_quadrant_and_brief_body(self, repl):
        r, buf = repl
        tid = self._make(r)
        out = _run(r, buf, f"/brief {tid[:8]}")
        assert "协作象限：双盲" in out
        assert "本次没有已采纳方案、约定和历史执行记录" in out
        assert "在当前项目目录里完成：加个邮箱登录" in out
        # 必须先说清「实发是单行」，否则人会以为执行器收到的是这份带缩进的东西。
        assert "单行" in out

    def test_says_how_much_and_what_to_add(self, repl):
        r, buf = repl
        tid = self._make(r)
        out = _run(r, buf, f"/brief {tid[:8]}")
        assert "该说多少" in out
        assert "可补" in out
        # 建议里出现的命令必须真实存在，且 id 已被替换成可复制的短 id。
        assert f"/draft {tid[:8]}" in out
        assert "<id>" not in out

    def test_teach_quadrant_after_accepting_a_plan(self, repl):
        """采纳过方案 → 讲授象限，且预览里看得到方案正文。"""
        r, buf = repl
        tid = self._make(r)
        draft = r.app.artifacts.create_draft(tid, "方案", "先做接口再做界面")
        r.app.artifacts.accept(draft.id)
        out = _run(r, buf, f"/brief {tid[:8]}")
        assert "协作象限：讲授" in out
        assert "先做接口再做界面" in out
        assert "本次没有" not in out

    def test_no_args_shows_usage(self, repl):
        r, buf = repl
        out = _run(r, buf, "/brief")
        assert "/brief <id>" in out

    def test_unknown_id_is_explained(self, repl):
        r, buf = repl
        out = _run(r, buf, "/brief 不存在的id")
        assert "找不到事务" in out

    def test_suggested_commands_are_real(self):
        """可补里点名的命令必须都在命令表里（防写一条不存在的下一步）。"""
        from freeagent.cli.app import _USAGE
        from freeagent.services.delegate import brief_quadrant

        for q in (brief_quadrant(), brief_quadrant(records=())):
            if not q.what_to_add:
                continue
            for cmd in ("/draft", "/memory"):
                if cmd in q.what_to_add:
                    assert cmd in _USAGE


# =============================================================================
# /ask 的凭据档（V1.104 / 11.10.9）
# =============================================================================
class TestAskKnowledgeBoundary:
    """答复之后给档位，说的是「查没查」，不是「对不对」。

    少了那句定性，「确定」会被读成「这句话是对的」—— 那正好是这套标注
    最坏的误用，比不标注更坏。
    """

    def _fake(self, monkeypatch, *, evidence=(), text="答", ok=True):
        from freeagent.services import ask as ask_mod

        def _fake(question, *, context="", **kw):
            if not ok:
                return ask_mod.AskOutcome(
                    ok=False, reason="用尽预算", partial="半截", evidence=evidence
                )
            return ask_mod.AskOutcome(ok=True, text=text, evidence=evidence)

        monkeypatch.setattr(ask_mod, "ask_executor", _fake)

    def test_no_tool_calls_reads_as_guessed(self, repl, monkeypatch):
        self._fake(monkeypatch)
        r, buf = repl
        out = _run(r, buf, "/ask 这个项目是干什么的")
        assert "猜想" in out
        assert "不是本机查证" in out

    def test_checked_file_reads_as_verified_with_path(self, repl, monkeypatch):
        from freeagent.services.tool_evidence import ToolEvidence

        self._fake(monkeypatch, evidence=(ToolEvidence("read", "README.md"),))
        r, buf = repl
        out = _run(r, buf, "/ask 这个项目是干什么的")
        assert "确定" in out
        assert "README.md" in out

    def test_the_line_says_it_is_not_a_verdict(self, repl, monkeypatch):
        """每个档位都必须带这句 —— 它是这条标注的边界，不是装饰。"""
        self._fake(monkeypatch)
        r, buf = repl
        out = _run(r, buf, "/ask 随便问问")
        assert "不是「对不对」" in out

    def test_failed_ask_still_shows_the_grade(self, repl, monkeypatch):
        """答没答完与查没查是两件事 —— 半截答复照样标档。"""
        from freeagent.services.tool_evidence import ToolEvidence

        self._fake(
            monkeypatch, ok=False,
            evidence=(ToolEvidence("read", "README.md"),),
        )
        r, buf = repl
        out = _run(r, buf, "/ask 这个项目是干什么的")
        assert "没答成：用尽预算" in out
        assert "确定" in out


# =============================================================================
# 在线检索可选腿（V1.105 / 3.7.9）
# =============================================================================
class TestOnlineLegCli:
    """本地关键词空手时，才问一句外面；问出去就要说清楚发往哪。

    这组的网络全部被替换（monkeypatch 模块里的 ``urllib_transport``），
    所以断言里出现的「已发往 …」是**我们让它发往**的，不是真的。
    """

    URL_ENV = "FREEAGENT_ONLINE_SEARCH_URL"
    URL = "https://searx.example.org/search?q={query}&format=json"
    BODY = (
        '{"results": [{"title": "怎么做销售周报", "url": "https://zhihu.com/q/1",'
        ' "content": "先列数字，再写结论。"}]}'
    )

    def _enable(self, monkeypatch, body=None, error=None):
        from freeagent.services import online_leg

        monkeypatch.setenv(self.URL_ENV, self.URL)

        def fake(url: str, timeout: float) -> str:
            if error is not None:
                raise OSError(error)
            return self.BODY if body is None else body

        monkeypatch.setattr(online_leg, "urllib_transport", fake)

    def test_off_by_default_says_nothing_in_search(self, repl):
        """没开启时检索输出里**不该**出现「在线检索」—— 那是噪声。"""
        r, buf = repl
        r.app.memory.add("回复先给结论", kind="约定")
        out = _run(r, buf, "/memory search 销售周报")
        assert "检索：" in out
        assert "在线检索" not in out

    def test_enabled_shows_external_block(self, repl, monkeypatch):
        self._enable(monkeypatch)
        r, buf = repl
        r.app.memory.add("回复先给结论", kind="约定")
        out = _run(r, buf, "/memory search 销售周报")
        assert "在线检索（外部材料，未核实）" in out
        assert "已发往 searx.example.org（查询原文离开了本机）" in out
        assert "怎么做销售周报" in out

    def test_external_results_are_not_mixed_with_local(self, repl, monkeypatch):
        """本地条目与外部材料必须**分块**摆 —— 可信度不同的两类证据。"""
        self._enable(monkeypatch)
        r, buf = repl
        r.app.memory.add("回复先给结论", kind="约定")
        out = _run(r, buf, "/memory search 销售周报")
        assert out.index("回复先给结论") < out.index("在线检索（外部材料")

    def test_local_hit_does_not_go_outside(self, repl, monkeypatch):
        self._enable(monkeypatch)
        r, buf = repl
        r.app.memory.add("回复先给结论", kind="约定")
        out = _run(r, buf, "/memory search 结论")
        assert "关键词命中" in out
        assert "在线检索" not in out

    def test_failure_reason_is_shown_verbatim(self, repl, monkeypatch):
        self._enable(monkeypatch, error="timed out")
        r, buf = repl
        r.app.memory.add("回复先给结论", kind="约定")
        out = _run(r, buf, "/memory search 销售周报")
        assert "没取到：" in out
        assert "timed out" in out

    def test_stats_says_whether_the_leg_is_on(self, repl):
        r, buf = repl
        r.app.memory.add("回复先给结论", kind="约定")
        out = _run(r, buf, "/memory stats 30")
        assert "在线检索：未启用" in out

    def test_stats_names_the_endpoint_when_on(self, repl, monkeypatch):
        self._enable(monkeypatch)
        r, buf = repl
        r.app.memory.add("回复先给结论", kind="约定")
        out = _run(r, buf, "/memory stats 30")
        assert "已启用" in out
        assert "searx.example.org" in out
        assert "离开本机" in out

    def test_stats_reports_a_broken_template(self, repl, monkeypatch):
        monkeypatch.setenv(self.URL_ENV, "https://searx.example.org/search")
        r, buf = repl
        r.app.memory.add("回复先给结论", kind="约定")
        out = _run(r, buf, "/memory stats 30")
        assert "配置有问题" in out
