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
