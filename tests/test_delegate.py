"""委派：把一件事交给 opencode 在指定项目里完成。

这个模块最该被测的不是「能不能跑通」，而是**四道闸门能不能绕过**：

1. 项目白名单 —— 相对路径、目录外路径、白名单前缀相似项
2. 人类闸门 —— ``inbox`` 的绝不派
3. **不传 ``--auto``** —— opencode 自己标了 dangerous
4. 幂等 —— 派过一次不重派

执行全程离线：``runner`` 注入，不启动 opencode、不发网络请求
（和 ``DeepSeekProvider`` 用 ``transport`` 注入同一个手法）。
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pytest

from freeagent.app import build_app
from freeagent.delegate import run_once
from freeagent.domain import FreeAgentError, RecordType, TaskState
from freeagent.services.clock import FrozenClock
from freeagent.services.delegate import (
    DelegationPolicy,
    build_brief,
    check_project_allowed,
    eligible_tasks,
    parse_opencode_output,
)
from freeagent.cli.app import parse_delegate_args

class _AllowAllGate:
    """放行闸门，**只给测试用**。

    刻意做成一个最小的具名类而不是 ``lambda``: 出错时要能一眼看出
    「这里本该有闸门」而不是「某个 lambda 返回了 None」。
    """

    def __init__(self, reason: str = "测试放行"):
        self.reason = reason
        self.calls: list[tuple] = []

    def check(self, *, task_id, project, brief):
        self.calls.append((task_id, project, brief))
        return None   # None = 放行


def _gate() -> _AllowAllGate:
    return _AllowAllGate()




class TestParseDelegateArgs:
    """**回归**：`/delegate` 照文档敲必然解析错。

    原来按位置取 ``args[1]`` 当角色，可文档写的分隔符 ``|`` 会被切成独立
    的一词，于是角色取到字面量 ``"|"``。文档里那句「例：/delegate D:/proj/myapp
    | 工作项目A | 加个邮箱登录」直接抄进去就是错的 —— 而 800 多个测试全绿，
    因为没有一个测过命令行的解析。
    """

    def test_documented_form_with_pipes(self):
        got = parse_delegate_args(
            ["D:/proj/myapp", "|", "工作项目A", "|", "加个邮箱登录"]
        )
        assert got == ("D:/proj/myapp", "工作项目A", "加个邮箱登录")

    def test_documented_form_pipes_attached(self):
        """`|工作` 这种贴着的写法也得认。"""
        got = parse_delegate_args(
            ["D:/proj/myapp", "|工作项目A", "|加个邮箱登录"]
        )
        assert got == ("D:/proj/myapp", "工作项目A", "加个邮箱登录")

    def test_space_separated_form(self):
        got = parse_delegate_args(["D:/proj/myapp", "工作项目A", "加个邮箱登录"])
        assert got == ("D:/proj/myapp", "工作项目A", "加个邮箱登录")

    def test_requirement_keeps_its_spaces(self):
        got = parse_delegate_args(
            ["D:/p", "|", "角色", "|", "加个", "邮箱", "登录"]
        )
        assert got[2] == "加个 邮箱 登录"

    def test_requirement_may_contain_pipes(self):
        """需求里自带 ``|``（比如表格）不该被当成第三段分隔符。"""
        got = parse_delegate_args(
            ["D:/p", "|", "角色", "|", "改", "a|b", "布局"]
        )
        assert got[0] == "D:/p"
        assert got[1] == "角色"
        assert "a|b" in got[2]

    def test_missing_segments_are_empty_not_guessed(self):
        """缺的段返回空串，让调用方明确报错 —— 不能猜。"""
        assert parse_delegate_args([]) == ("", "", "")
        assert parse_delegate_args(["D:/p"]) == ("D:/p", "", "")
        assert parse_delegate_args(["D:/p", "|", "角色"]) == ("D:/p", "角色", "")

    def test_repeated_pipes_do_not_create_empty_segments(self):
        got = parse_delegate_args(["D:/p", "|", "|", "角色", "|", "需求"])
        assert got == ("D:/p", "角色", "需求")


@pytest.fixture
def db_path(tmp_path) -> Path:
    """执行器是**独立进程**，它自己开库 —— 所以测试必须把同一个路径给它。"""
    return tmp_path / "a.db"


@pytest.fixture
def app(db_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    a = build_app(db_path, clock=FrozenClock(datetime(2026, 9, 26, 14, 0)))
    yield a
    a.close()


def arm_all(app) -> None:
    """把还没开始的事务都 ``/start`` —— 也就是「人确认过了」。"""
    for task in app.task_repo.list_all():
        if task.state is TaskState.INBOX:
            app.tasks.start(task.id)


@pytest.fixture
def work(app):
    return app.roles.create("工作项目A", note="开发 项目")


def make_delegation(app, work, project: str) -> str:
    return app.tasks.create(
        "加个邮箱登录", [work.id],
        intent=f"在 {project} 里完成",
        project_path=project,
    ).id


# =============================================================================
# 闸门一：项目白名单
# =============================================================================
class TestProjectAllowlist:
    POLICY = DelegationPolicy(projects=("D:/proj/app", "D:/proj/lib"))

    def test_disabled_by_default(self):
        with pytest.raises(FreeAgentError) as ei:
            check_project_allowed(DelegationPolicy(), "D:/proj/app")
        assert "未启用" in str(ei.value)

    def test_exact_match_allowed(self, tmp_path):
        project = tmp_path / "app"
        project.mkdir()
        policy = DelegationPolicy(projects=(str(project),))
        assert check_project_allowed(policy, str(project)) == project.resolve()

    def test_subdirectory_allowed(self, tmp_path):
        root = tmp_path / "app"
        sub = root / "packages" / "web"
        sub.mkdir(parents=True)
        policy = DelegationPolicy(projects=(str(root),))
        got = check_project_allowed(policy, str(sub))
        assert got == sub.resolve()

    def test_relative_path_rejected(self):
        """相对路径的基准随工作目录变 —— 绕过白名单的经典手法。"""
        with pytest.raises(FreeAgentError) as ei:
            check_project_allowed(self.POLICY, "../../etc")
        assert "绝对路径" in str(ei.value)

    def test_outside_allowlist_rejected(self):
        with pytest.raises(FreeAgentError) as ei:
            check_project_allowed(self.POLICY, "D:/other/secret")
        assert "白名单" in str(ei.value)

    def test_similar_prefix_not_allowed(self, tmp_path):
        """``D:/proj/app`` 不得匹配 ``D:/proj/app-secrets``。

        用字符串前缀判断就会漏 —— 这是最容易被写错的一处。
        """
        root = tmp_path / "app"
        sibling = tmp_path / "app-secrets"
        root.mkdir()
        sibling.mkdir()
        policy = DelegationPolicy(projects=(str(root),))
        with pytest.raises(FreeAgentError):
            check_project_allowed(policy, str(sibling))

    def test_quotes_stripped(self, tmp_path):
        project = tmp_path / "app"
        project.mkdir()
        policy = DelegationPolicy(projects=(str(project),))
        assert check_project_allowed(policy, f'"{project}"') == project.resolve()

    def test_empty_rejected(self):
        with pytest.raises(FreeAgentError):
            check_project_allowed(self.POLICY, "   ")

    def test_allowlist_reported_when_rejected(self, tmp_path):
        allowed = tmp_path / "ok"
        allowed.mkdir()
        policy = DelegationPolicy(projects=(str(allowed),))
        with pytest.raises(FreeAgentError) as ei:
            check_project_allowed(policy, str(tmp_path / "nope"))
        assert str(allowed) in str(ei.value), "报错要告诉用户白名单是啥"

    def test_relative_in_allowlist_rejected_at_config_load(self, tmp_path, monkeypatch):
        """配置里就不该能写相对路径。"""
        import json as _json

        from freeagent.config import load_config
        from freeagent.domain import ValidationError

        (tmp_path / "config.json").write_text(
            _json.dumps({"delegate": {"projects": ["./app"]}}),
            encoding="utf-8",
        )
        with pytest.raises(ValidationError) as ei:
            load_config(tmp_path)
        assert "绝对路径" in str(ei.value)

    def test_relative_not_normalized_into_absolute(self, tmp_path, monkeypatch):
        """白名单里的相对路径**不会被**偷偷变成绝对路径。"""
        import json as _json

        from freeagent.config import load_config
        from freeagent.domain import ValidationError

        (tmp_path / "config.json").write_text(
            _json.dumps({"delegate": {"projects": ["app", "../x"]}}),
            encoding="utf-8",
        )
        with pytest.raises(ValidationError):
            load_config(tmp_path)


# =============================================================================
# 闸门二：人类确认
# =============================================================================
class TestHumanGate:
    def test_inbox_task_is_never_eligible(self, app, work, tmp_path):
        """**核心闸门**：刚建的委派事务绝不能被派出去。"""
        make_delegation(app, work, str(tmp_path))
        assert eligible_tasks(app.task_repo, app.record_repo) == []

    def test_started_task_becomes_eligible(self, app, work, tmp_path):
        tid = make_delegation(app, work, str(tmp_path))
        app.tasks.start(tid)
        got = eligible_tasks(app.task_repo, app.record_repo)
        assert [t.id for t in got] == [tid]

    def test_normal_task_never_eligible(self, app, work, tmp_path):
        plain = app.tasks.create("普通事务", [work.id])
        app.tasks.start(plain.id)
        assert eligible_tasks(app.task_repo, app.record_repo) == []

    def test_done_task_not_eligible(self, app, work, tmp_path):
        tid = make_delegation(app, work, str(tmp_path))
        app.tasks.start(tid)
        app.tasks.complete(tid)
        assert eligible_tasks(app.task_repo, app.record_repo) == []

    def test_delegation_starts_in_inbox(self, app, work, tmp_path):
        task = app.tasks.get(make_delegation(app, work, str(tmp_path)))
        assert task.state is TaskState.INBOX
        assert task.project_path == str(tmp_path)


# =============================================================================
# 幂等：派过一次不重派
# =============================================================================
class TestIdempotence:
    def test_dispatched_task_not_eligible_again(self, app, work, tmp_path):
        tid = make_delegation(app, work, str(tmp_path))
        app.tasks.start(tid)
        app.record_repo.append(
            tid, RecordType.DELEGATION_DISPATCHED, "派过了", app.clock.now()
        )
        assert eligible_tasks(app.task_repo, app.record_repo) == []

    def test_other_records_do_not_block(self, app, work, tmp_path):
        tid = make_delegation(app, work, str(tmp_path))
        app.tasks.start(tid)
        app.tasks.note(tid, "随便记一笔")
        assert len(eligible_tasks(app.task_repo, app.record_repo)) == 1


# =============================================================================
# 简报：不给执行器内部术语
# =============================================================================
class TestBrief:
    def test_contains_title_intent_dod(self, app, work, tmp_path):
        tid = make_delegation(app, work, str(tmp_path))
        task = app.tasks.get(tid)
        brief = build_brief(task, task.intent, "能登录就行")
        assert "加个邮箱登录" in brief
        assert "能登录就行" in brief

    def test_does_not_leak_internal_vocabulary(self, app, work, tmp_path):
        """执行器不知道恢复契约、角色脉络、排序信号 —— 说了只会让它误解。"""
        tid = make_delegation(app, work, str(tmp_path))
        task = app.tasks.get(tid)
        brief = build_brief(task, task.intent, None)
        for leak in ("恢复契约", "角色脉络", "排序信号", "启发式", "顺延", "WaitFor"):
            assert leak not in brief, f"简报里不该出现内部术语 {leak}"

    def test_states_scope_constraint(self, app, work, tmp_path):
        """必须告诉它别动目录外的文件。"""
        tid = make_delegation(app, work, str(tmp_path))
        brief = build_brief(app.tasks.get(tid), None, None)
        assert "目录" in brief

    def test_brief_is_always_single_line(self, app, work, tmp_path):
        """**回归**：简报里有换行 = 委派必然失败。

        实测（opencode 1.18.31）：提示中只要有一个 ``\\n``，opencode 就判定
        为「复杂任务」，升级到主 agent 的强模型并**无视 ``--model``**，
        余额不足直接 402。原来简报是带 ``#``/``##``/``-`` 的多行 markdown，
        正好踩中 —— 每一单委派都在付费模型上失败。
        """
        tid = make_delegation(app, work, str(tmp_path))
        task = app.tasks.get(tid)
        assert "\n" not in build_brief(task, task.intent, task.definition_of_done)

    def test_newlines_in_user_input_are_flattened(self, app, work, tmp_path):
        """用户自己写的意图/完成标准可能带换行 —— 必须被压掉。"""
        tid = make_delegation(app, work, str(tmp_path))
        task = app.tasks.get(tid)
        brief = build_brief(
            task,
            "第一行\n第二行\n第三行",
            "标准一\n标准二",
        )
        assert "\n" not in brief, "用户输入里的换行也会触发升级"
        assert "第二行" in brief and "标准二" in brief, "内容不能被弄丢"


# =============================================================================
# 解析执行器输出
# =============================================================================
class TestParseOutput:
    def test_reads_text_events(self):
        out = "\n".join([
            _evt({"type": "text", "text": "加好了登录页", "sessionID": "s1"}),
        ])
        got = parse_opencode_output(out)
        assert got.ok
        assert "加好了登录页" in got.summary
        assert got.session_id == "s1"

    def test_joins_multiple_text_events(self):
        out = "\n".join([
            _evt({"type": "text", "text": "第一段"}),
            _evt({"type": "text", "text": "第二段"}),
        ])
        assert "第一段" in parse_opencode_output(out).summary
        assert "第二段" in parse_opencode_output(out).summary

    def test_error_event_fails(self):
        got = parse_opencode_output(
            _evt({"type": "error", "error": {"name": "APIError",
                    "data": {"message": "Insufficient account funds"}}})
        )
        assert got.ok is False
        assert "Insufficient" in got.summary

    def test_dirty_lines_ignored(self):
        """混入日志行不能整次崩掉 —— 崩了这条事务会永远重试。"""
        out = "\n".join([
            "some log line",
            "{not valid json",
            "",
            _evt({"type": "text", "text": "正常输出"}),
            "trailing garbage",
        ])
        got = parse_opencode_output(out)
        assert got.ok
        assert "正常输出" in got.summary

    def test_no_output_fails_honestly(self):
        got = parse_opencode_output("")
        assert got.ok is False
        assert "没有产出" in got.summary

    def test_tool_names_collected(self):
        out = "\n".join([
            _evt({"type": "tool_use", "name": "read"}),
            _evt({"type": "tool_use", "name": "edit"}),
            _evt({"type": "tool_use", "name": "read"}),   # 去重
            _evt({"type": "text", "text": "好了"}),
        ])
        assert parse_opencode_output(out).tool_calls == ("read", "edit")

    def test_whitespace_only_text_ignored(self):
        out = _evt({"type": "text", "text": "   "})
        got = parse_opencode_output(out)
        assert got.ok is False, "空白不算产出"


def _evt(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False)


#: **真实观察到的**事件形状（2026-09-27 实测 opencode 1.18.31）。
#:
#: 这个 fixture 是从真的 opencode 输出里抄的，不是设计出来的 ——
#: 我原本按「文本在顶层」写，测了 30 多次全绿，结果真跑一次才发现
#: 文本嵌在 ``part`` 里，于是**每一次成功的委派都会被误报成没产出**。
#: 教训：事件流的形状不能靠猜，必须拿真输出当 fixture。
_REAL_TEXT_EVENT = {
    "type": "text",
    "timestamp": 1790473276899,
    "sessionID": "ses_f1f7a1dadffenQKDvDTIzXlDmr",
    "part": {
        "id": "prt_0e0860dc0001jarL0v4ar3f0pb",
        "messageID": "msg_0e085e51d001a9h7Cl0Jbx6yCy",
        "sessionID": "ses_f1f7a1dadffenQKDvDTIzXlDmr",
        "type": "text",
        "text": "OK",
        "time": {"start": 1790473276864, "end": 1790473276868},
    },
}

_REAL_ERROR_EVENT = {
    "type": "error",
    "timestamp": 1790471229492,
    "sessionID": "ses_f1f995bacffedFEohwBqKk001I",
    "error": {
        "name": "APIError",
        "data": {
            "message": "Upstream request failed: Insufficient account funds",
            "statusCode": 402,
            "isRetryable": False,
        },
        "metadata": {"url": "https://opencode.ai/zen/v1/messages"},
    },
}


class TestRealObservedEventShape:
    """用**真跑一次**抄下来的事件形状测，不是设计出来的。

    我原本假设文本在事件顶层，写了三十多个绿测。真跑一次才发现文本嵌在
    ``part`` 里 —— 于是解析器会把每一次**成功**的委派误报成「没有产出」。
    这个 fixture 存在的意义就是不让那种错误再发生。
    """

    def test_reads_text_nested_in_part(self):
        got = parse_opencode_output(_evt(_REAL_TEXT_EVENT))
        assert got.ok, "真实事件形状必须能解析出文本"
        assert "OK" in got.summary
        assert got.session_id == "ses_f1f7a1dadffenQKDvDTIzXlDmr"

    def test_reads_402_error_from_real_shape(self):
        got = parse_opencode_output(_evt(_REAL_ERROR_EVENT))
        assert got.ok is False
        assert "Insufficient account funds" in got.summary

    def test_full_realistic_stream(self):
        """step_start → text → step_finish 的完整流。"""
        out = "\n".join([
            _evt({"type": "step_start", "timestamp": 1790473276899,
                  "sessionID": "s1", "part": {"type": "step-start"}}),
            _evt(_REAL_TEXT_EVENT),
            _evt({"type": "step_finish", "timestamp": 1790473276900,
                  "sessionID": "s1",
                  "part": {"type": "step-finish", "reason": "stop",
                           "tokens": {"total": 28798}}}),
        ])
        got = parse_opencode_output(out)
        assert got.ok and "OK" in got.summary

    def test_top_level_text_still_supported(self):
        """顶层形态也认 —— 换版本时不至于立刻瞎。"""
        got = parse_opencode_output(_evt({"type": "text", "text": "旧形状"}))
        assert got.ok and "旧形状" in got.summary

    def test_text_not_double_counted(self):
        """外层与 part 都是 text 时不能计入两次。"""
        got = parse_opencode_output(_evt(_REAL_TEXT_EVENT))
        assert got.summary.count("OK") == 1


    def test_failure_note_includes_reason_from_stderr(self, tmp_path):
        """**回归**：失败原因（stderr）必须出现在终端报告里。

        原本 note 只拼 ``summary``，而原因在 ``detail`` 里 ——
        终端只显示「执行器退出码 1」，用户无从下手，尽管原因就存在产物链里。
        报错信息不进 stderr 等于没报。
        """
        project = tmp_path / "app"
        project.mkdir()
        db = tmp_path / "d.db"
        app = build_app(db, clock=FrozenClock(datetime(2026, 9, 26, 14, 0)))
        role = app.roles.create("工作")
        task = app.tasks.create("做点事", [role.id], project_path=str(project))
        app.tasks.start(task.id)
        app.close()

        def runner(argv, cwd, timeout):
            return 1, "", "Error: Insufficient account funds"

        report = run_once(
            db_path=db,
            runner=runner,
            policy=DelegationPolicy(projects=(str(project),)), gate=_gate())
        joined = "\n".join(report.notes)
        assert "Insufficient account funds" in joined, (
            "失败原因必须能被看到，实际报告：%s" % joined
        )
        assert report.failed == 1

    def test_model_flag_is_passed_when_set(self, tmp_path):
        """选了模型就必须真传出去 —— 不然等于没选。"""
        captured: dict = {}
        project = tmp_path / "app"
        project.mkdir()
        db = tmp_path / "d.db"
        app = build_app(db, clock=FrozenClock(datetime(2026, 9, 26, 14, 0)))
        role = app.roles.create("工作")
        task = app.tasks.create("做点事", [role.id], project_path=str(project))
        app.tasks.start(task.id)
        app.close()

        def runner(argv, cwd, timeout):
            captured["argv"] = list(argv)
            return 0, _evt(_REAL_TEXT_EVENT), ""

        run_once(
            db_path=db,
            runner=runner,
            policy=DelegationPolicy(
                projects=(str(project),), model="opencode/big-pickle"
            ), gate=_gate())
        argv = captured["argv"]
        assert "--model" in argv
        assert argv[argv.index("--model") + 1] == "opencode/big-pickle"
        assert "--auto" not in argv, "绝不能传 --auto"

    def test_no_model_flag_when_policy_has_none(self, tmp_path):
        """没选模型就不该出现 ``--model``，让 opencode 用它自己的默认。"""
        captured: dict = {}
        project = tmp_path / "app"
        project.mkdir()
        db = tmp_path / "d.db"
        app = build_app(db, clock=FrozenClock(datetime(2026, 9, 26, 14, 0)))
        role = app.roles.create("工作")
        task = app.tasks.create("做点事", [role.id], project_path=str(project))
        app.tasks.start(task.id)
        app.close()

        def runner(argv, cwd, timeout):
            captured["argv"] = list(argv)
            return 0, _evt(_REAL_TEXT_EVENT), ""

        run_once(
            db_path=db, runner=runner,
            policy=DelegationPolicy(projects=(str(project),)), gate=_gate())
        assert "--model" not in captured["argv"]


# =============================================================================
# 端到端：假执行器
# =============================================================================
class TestRunOnce:
    def _policy(self, tmp_path) -> DelegationPolicy:
        project = tmp_path / "app"
        project.mkdir(exist_ok=True)
        return DelegationPolicy(projects=(str(project),))

    def test_dry_run_dispatches_nothing(self, app, db_path, work, tmp_path):
        from freeagent import delegate as runner_mod

        make_delegation(app, work, str(tmp_path / "app"))
        arm_all(app)
        calls = []

        report = runner_mod.run_once(
            db_path=db_path,
            runner=lambda argv, cwd, timeout: calls.append(argv) or (0, "", ""),
            policy=self._policy(tmp_path),
            dry_run=True, gate=_gate())
        assert report.considered == 1
        assert report.dispatched == 0
        assert calls == [], "dry-run 绝不能真的执行"

    def test_never_passes_auto_flag(self, app, db_path, work, tmp_path):
        """opencode 自己把 --auto 标成 dangerous。我们不能替用户按下它。"""
        from freeagent import delegate as runner_mod

        make_delegation(app, work, str(tmp_path / "app"))
        arm_all(app)
        seen = {}

        def runner(argv, cwd, timeout):
            seen["argv"] = list(argv)
            return 0, _evt({"type": "text", "text": "改完了", "sessionID": "s9"}), ""

        runner_mod.run_once(
            db_path=db_path, runner=runner, policy=self._policy(tmp_path), gate=_gate())
        assert "--auto" not in seen["argv"]
        assert "--format" in seen["argv"] and "json" in seen["argv"]

    def test_argv_has_no_shell_metacharacters_risk(self, app, db_path, work, tmp_path):
        """命令以数组传给 subprocess，不经 shell —— 这里确认 brief 是单个参数。"""
        from freeagent import delegate as runner_mod

        make_delegation(app, work, str(tmp_path / "app"))
        arm_all(app)
        seen = {}

        def runner(argv, cwd, timeout):
            seen["argv"] = list(argv)
            return 0, _evt({"type": "text", "text": "ok"}), ""

        runner_mod.run_once(
            db_path=db_path, runner=runner, policy=self._policy(tmp_path), gate=_gate())
        assert seen["argv"][0].endswith("opencode")
        assert seen["argv"][1] == "run", "第二个参数才是子命令"
        # brief 必须是**一个** argv 元素（不经 shell，所以不会被拆开）。
        # 原来这里断言「含换行」，那是在简报还是多行 markdown 时的代理指标；
        # 现在简报合法地变成单行了，改成直接验证「它是一个整体」。
        brief = seen["argv"][2]
        assert "邮箱登录" in brief or "登录" in brief or brief
        assert seen["argv"][3] == "--format", "brief 之后紧跟 flag，没被拆开"

    def test_records_result_as_artifact(self, app, db_path, work, tmp_path):
        from freeagent import delegate as runner_mod

        tid = make_delegation(app, work, str(tmp_path / "app"))
        arm_all(app)

        def runner(argv, cwd, timeout):
            return 0, _evt({
                "type": "text", "text": "加好了登录页", "sessionID": "s7"
            }), ""

        runner_mod.run_once(
            db_path=db_path, runner=runner, policy=self._policy(tmp_path), gate=_gate())
        artifacts = app.artifacts.list_for_task(tid)
        assert artifacts, "产出应落成 artifact"
        assert "加好了登录页" in artifacts[-1].content
        assert "s7" in artifacts[-1].content, "应记下 session 便于追溯"

    def test_failure_is_recorded_not_silently_dropped(self, app, db_path, work, tmp_path):
        from freeagent import delegate as runner_mod

        tid = make_delegation(app, work, str(tmp_path / "app"))
        arm_all(app)

        def runner(argv, cwd, timeout):
            return 1, "", "boom: 需要人工确认"

        report = runner_mod.run_once(
            db_path=db_path, runner=runner, policy=self._policy(tmp_path), gate=_gate())
        assert report.failed == 1 and report.succeeded == 0
        types = [r.type for r in app.record_repo.list_for_task(tid)]
        assert RecordType.DELEGATION_FAILED in types
        assert RecordType.DELEGATION_DISPATCHED in types

    def test_does_not_auto_complete_the_task(self, app, db_path, work, tmp_path):
        """执行器说做完了 ≠ 你验收过了。任务必须留在 active 等你看。"""
        from freeagent import delegate as runner_mod

        tid = make_delegation(app, work, str(tmp_path / "app"))
        arm_all(app)

        runner_mod.run_once(
            db_path=db_path,
            runner=lambda a, c, t: (0, _evt({"type": "text", "text": "搞定"}), ""),
            policy=self._policy(tmp_path), gate=_gate())
        assert app.tasks.get(tid).state is TaskState.ACTIVE

    def test_rerun_is_a_noop(self, app, db_path, work, tmp_path):
        from freeagent import delegate as runner_mod

        make_delegation(app, work, str(tmp_path / "app"))
        arm_all(app)
        calls = []

        def runner(argv, cwd, timeout):
            calls.append(1)
            return 0, _evt({"type": "text", "text": "ok"}), ""

        kw = dict(db_path=db_path, runner=runner, policy=self._policy(tmp_path))
        first = runner_mod.run_once(**kw, gate=_gate())
        second = runner_mod.run_once(**kw, gate=_gate())
        assert first.dispatched == 1
        assert second.considered == 0
        assert len(calls) == 1, "不该重复执行"

    def test_disabled_policy_dispatches_nothing(self, app, db_path, work, tmp_path):
        from freeagent import delegate as runner_mod

        make_delegation(app, work, str(tmp_path / "app"))
        arm_all(app)
        calls = []
        report = runner_mod.run_once(
            db_path=db_path,
            runner=lambda a, c, t: calls.append(1) or (0, "", ""),
            policy=DelegationPolicy(), gate=_gate())
        assert report.dispatched == 0
        assert calls == []
        assert "未启用" in report.notes[0]

    def test_whitelist_violation_does_not_run(self, app, db_path, work, tmp_path):
        """白名单外的路径：即使事务已经是 active，执行器也必须拒绝跑。"""
        from freeagent import delegate as runner_mod

        outside = tmp_path / "secret"
        outside.mkdir()
        tid = make_delegation(app, work, str(outside))
        arm_all(app)
        calls = []
        report = runner_mod.run_once(
            db_path=db_path,
            runner=lambda a, c, t: calls.append(1) or (0, "", ""),
            policy=DelegationPolicy(projects=(str(tmp_path / "other"),)), gate=_gate())
        assert calls == [], "白名单外绝不能执行"
        assert report.failed == 1
        # 失败必须留痕，否则这条事务会永远停在 active 反复重试
        types = [r.type for r in app.record_repo.list_for_task(tid)]
        assert RecordType.DELEGATION_FAILED in types

    def test_one_violation_does_not_block_the_others(self, app, db_path, work,
                                                    tmp_path):
        """一条违规不能拖垮整批 —— 否则别的任务永远派不到。"""
        from freeagent import delegate as runner_mod

        good = tmp_path / "app"
        good.mkdir()
        bad = tmp_path / "secret"
        bad.mkdir()
        good_id = make_delegation(app, work, str(good))
        make_delegation(app, work, str(bad))
        arm_all(app)
        calls = []

        def runner(argv, cwd, timeout):
            calls.append(cwd)
            return 0, _evt({"type": "text", "text": "ok"}), ""

        report = runner_mod.run_once(
            db_path=db_path, runner=runner,
            policy=DelegationPolicy(projects=(str(good),)), gate=_gate())
        assert report.dispatched == 2
        assert report.succeeded == 1 and report.failed == 1
        assert len(calls) == 1, "只该跑白名单内那一条"
        assert app.artifacts.list_for_task(good_id), "合规那条应正常产出"

    def test_timeout_is_forwarded(self, app, db_path, work, tmp_path):
        from freeagent import delegate as runner_mod

        make_delegation(app, work, str(tmp_path / "app"))
        arm_all(app)
        seen = {}

        def runner(argv, cwd, timeout):
            seen["timeout"] = timeout
            return 0, _evt({"type": "text", "text": "ok"}), ""

        runner_mod.run_once(
            db_path=db_path, runner=runner,
            policy=DelegationPolicy(projects=(str(tmp_path / "app"),), timeout=42.0), gate=_gate())
        assert seen["timeout"] == 42.0

    def test_runs_in_the_project_directory(self, app, db_path, work, tmp_path):
        from freeagent import delegate as runner_mod

        make_delegation(app, work, str(tmp_path / "app"))
        arm_all(app)
        seen = {}

        def runner(argv, cwd, timeout):
            seen["cwd"] = cwd
            return 0, _evt({"type": "text", "text": "ok"}), ""

        runner_mod.run_once(
            db_path=db_path, runner=runner,
            policy=DelegationPolicy(projects=(str(tmp_path / "app"),)), gate=_gate())
        assert seen["cwd"].resolve() == (tmp_path / "app").resolve()


# =============================================================================
# 闭环：结果回推发起它的飞书会话
# =============================================================================
class TestResultPushBack:
    """从飞书发起的委派，跑完要**自己把结果飞回去**。

    没有这一段，「在飞书里派一件事」就只是单向的 —— 你还得切回终端看结果，
    闭环不算合上。
    """

    CHAT = "oc_from_feishu"

    def _captured(self, monkeypatch, tmp_path):
        """把 FeishuSender.send_text 换成收集器，**不发真请求**。"""
        from freeagent.feishu import sender as sender_mod

        sent: list[tuple[str, str]] = []

        def fake(self, chat_id, text):
            sent.append((chat_id, text))

        monkeypatch.setattr(sender_mod.FeishuSender, "send_text", fake)
        monkeypatch.setenv("FEISHU_APP_ID", "cli_x")
        monkeypatch.setenv("FEISHU_APP_SECRET", "s")
        return sent

    def _run(self, app, db_path, tmp_path, chat_id, outcome_ok=True):
        from freeagent import delegate as runner_mod

        def runner(argv, cwd, timeout):
            return (
                0 if outcome_ok else 1,
                _evt(_REAL_TEXT_EVENT) if outcome_ok else "",
                "" if outcome_ok else "boom",
            )

        runner_mod.run_once(
            db_path=db_path, runner=runner,
            policy=DelegationPolicy(projects=(str(tmp_path / "app"),)), gate=_gate())
        return app

    def test_pushes_back_to_originating_chat(
        self, app, db_path, work, tmp_path, monkeypatch
    ):
        project = tmp_path / "app"
        project.mkdir()
        sent = self._captured(monkeypatch, tmp_path)
        app.tasks.create(
            "加个登录", [work.id], project_path=str(project),
            delegate_chat_id=self.CHAT,
        )
        arm_all(app)
        self._run(app, db_path, tmp_path, self.CHAT)
        assert sent, "从飞书发起的委派必须回推结果"
        assert sent[0][0] == self.CHAT

    def test_terminal_delegation_does_not_push(
        self, app, db_path, work, tmp_path, monkeypatch
    ):
        """从终端发起的（没有 chat_id）不该去打扰飞书。"""
        project = tmp_path / "app"
        project.mkdir()
        sent = self._captured(monkeypatch, tmp_path)
        app.tasks.create("加个登录", [work.id], project_path=str(project))
        arm_all(app)
        self._run(app, db_path, tmp_path, self.CHAT)
        assert sent == [], "没有 delegate_chat_id 就不该回推"

    def test_skipped_when_feishu_not_configured(
        self, app, db_path, work, tmp_path, monkeypatch, caplog
    ):
        """**回归**：飞书没配时必须**出声警告**，不能静默跳过。

        踩过的坑：这里原本直接 return。而执行器是**独立进程**，手册又要求
        「另开一个终端跑它」—— 于是 ``FEISHU_*`` 很可能只设在桥接那个终端。
        这边读不到 → 静默跳过 → 结果没飞回飞书 → **而用户以为闭环是通的**。
        宁可吵一点。
        """
        import logging

        project = tmp_path / "app"
        project.mkdir()
        sent = self._captured(monkeypatch, tmp_path)
        monkeypatch.setenv("FEISHU_APP_ID", "")
        monkeypatch.setenv("FEISHU_APP_SECRET", "")
        app.tasks.create(
            "加个登录", [work.id], project_path=str(project),
            delegate_chat_id=self.CHAT,
        )
        arm_all(app)
        with caplog.at_level(logging.WARNING, logger="freeagent.delegate"):
            self._run(app, db_path, tmp_path, self.CHAT)
        assert sent == []
        assert "没有推回飞书" in caplog.text, (
            "缺环境变量时必须明确告诉用户结果没推回去，"
            "否则他会以为闭环是通的。实际日志：%r" % caplog.text
        )

    def test_no_warning_for_terminal_delegation(
        self, app, db_path, work, tmp_path, monkeypatch, caplog
    ):
        """终端发起的委派**不该**被提醒设飞书 —— 那是正常路径，不是问题。"""
        import logging

        project = tmp_path / "app"
        project.mkdir()
        self._captured(monkeypatch, tmp_path)
        monkeypatch.setenv("FEISHU_APP_ID", "")
        monkeypatch.setenv("FEISHU_APP_SECRET", "")
        app.tasks.create("加个登录", [work.id], project_path=str(project))
        arm_all(app)
        with caplog.at_level(logging.WARNING, logger="freeagent.delegate"):
            self._run(app, db_path, tmp_path, self.CHAT)
        assert "没有推回飞书" not in caplog.text, (
            "终端发起的委派没有来源会话，不该提飞书环境变量"
        )

    def test_push_failure_does_not_break_delegation(
        self, app, db_path, work, tmp_path, monkeypatch
    ):
        """**核心**：通知渠道挂了**不等于**任务没做完。

        所以飞书推送失败时：委派结果照常落库、状态照常是 active、
        执行器不能抛错。反过来也不成 —— 通知通了不代表任务做完了。
        """
        project = tmp_path / "app"
        project.mkdir()
        from freeagent.feishu import sender as sender_mod

        def boom(self, chat_id, text):
            raise RuntimeError("飞书挂了")

        monkeypatch.setattr(sender_mod.FeishuSender, "send_text", boom)
        monkeypatch.setenv("FEISHU_APP_ID", "cli_x")
        monkeypatch.setenv("FEISHU_APP_SECRET", "s")
        task_id = app.tasks.create(
            "加个登录", [work.id], project_path=str(project),
            delegate_chat_id=self.CHAT,
        ).id
        arm_all(app)
        self._run(app, db_path, tmp_path, self.CHAT)   # 不该抛
        # 本地该有的痕迹一样不少
        types = {r.type for r in app.record_repo.list_for_task(task_id)}
        assert RecordType.DELEGATION_SUCCEEDED in types
        assert app.artifacts.list_for_task(task_id)
        assert app.tasks.get(task_id).state is TaskState.ACTIVE

    def test_push_text_tells_user_to_verify(
        self, app, db_path, work, tmp_path, monkeypatch
    ):
        """文案必须写明「待你验收」—— 机器不能替人判断做完没有。"""
        project = tmp_path / "app"
        project.mkdir()
        sent = self._captured(monkeypatch, tmp_path)
        app.tasks.create(
            "加个登录", [work.id], project_path=str(project),
            delegate_chat_id=self.CHAT,
        )
        arm_all(app)
        self._run(app, db_path, tmp_path, self.CHAT)
        assert "验收" in sent[0][1], "回推文案必须提醒人验收"
        assert "进行中" in sent[0][1], "要说明事务状态没自动变完成"
