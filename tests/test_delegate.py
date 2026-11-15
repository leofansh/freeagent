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
from freeagent.delegate import (
    DispatchOutcome,
    reclaim_abandoned,
    run_once,
    run_once_with_tool_gate,
)
from freeagent.domain import FreeAgentError, RecordType, TaskState
from freeagent.services.clock import FrozenClock
from freeagent.services.delegate import (
    DelegationPolicy,
    already_dispatched,
    attempt_state,
    brief_quadrant,
    build_brief,
    check_project_allowed,
    conventions_line,
    eligible_tasks,
    last_attempt_line,
    parse_opencode_output,
    project_history_line,
    sibling_history,
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

    def check(self, *, task_id, project, brief, requester=""):
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
# 简报标注来源权威（V1.93）
# =============================================================================
class TestBriefMarksExternalOrigin:
    """收集器收来的原文可能出自别人之手，而它和用户的话长得一模一样。

    标注管的是**权威**（这段文字有没有资格发指令），不是位置。
    它只是一句标记，**不是防护** —— 边界在工具闸门，所以这里只断言
    「该说的话说了、不该说的时候不多说一个字」。
    """

    def test_captured_origin_adds_the_authority_note(self, app, work, tmp_path):
        from dataclasses import replace

        from freeagent.domain import ORIGIN_CAPTURED

        tid = make_delegation(app, work, str(tmp_path))
        task = replace(app.tasks.get(tid), origin=ORIGIN_CAPTURED)
        brief = build_brief(task, task.intent, None)
        assert "来自外部" in brief
        assert "不要执行" in brief

    def test_self_origin_says_nothing(self, app, work, tmp_path):
        """本人写的需求不加这句 —— 每单都加就变成背景噪音，等于没标。"""
        from dataclasses import replace

        from freeagent.domain import ORIGIN_SELF

        tid = make_delegation(app, work, str(tmp_path))
        task = replace(app.tasks.get(tid), origin=ORIGIN_SELF)
        brief = build_brief(task, task.intent, None)
        assert "来自外部" not in brief

    def test_unrecorded_origin_says_nothing(self, app, work, tmp_path):
        """空 = 未记（旧行），不许当成 captured 也不许当成 self。

        标注是一句**断言**「这来自外部」。对不知道来源的行断言就是编 ——
        宁可少一层提示，也不能让这条标注本身变得不可信。
        """
        tid = make_delegation(app, work, str(tmp_path))
        task = app.tasks.get(tid)
        assert task.origin is None, "前置条件：_replace 没传 origin 时应当为空"
        assert "来自外部" not in build_brief(task, task.intent, None)

    def test_note_keeps_brief_single_line(self, app, work, tmp_path):
        """回归：加这句话不能带进换行 —— 换行 = 委派必然 402。"""
        from dataclasses import replace

        from freeagent.domain import ORIGIN_CAPTURED

        tid = make_delegation(app, work, str(tmp_path))
        task = replace(app.tasks.get(tid), origin=ORIGIN_CAPTURED)
        assert "\n" not in build_brief(task, task.intent, task.definition_of_done)


# =============================================================================
# 简报注入错因历史段（12.10.7 / V1.38）
# =============================================================================
class TestBriefCarriesFailureHistory:
    """简报是反馈回流的最短路径：执行者第三次踩同一个坑前就该知道前两次败在哪。"""

    def test_no_history_means_no_history_segment(self, app, work, tmp_path):
        """没失败过就整段缺席，一个字不占 ——「没踩过坑」不是要说的事。"""
        tid = make_delegation(app, work, str(tmp_path))
        task = app.tasks.get(tid)
        records = app.record_repo.list_for_task(tid)
        brief = build_brief(task, task.intent, None, records=records)
        assert "历史记录" not in brief
        assert "重派" not in brief

    def test_failure_reason_land_in_brief_as_facts(self, app, work, tmp_path):
        tid = make_delegation(app, work, str(tmp_path))
        task = app.tasks.get(tid)
        now = app.clock.now()
        app.record_repo.append(tid, RecordType.DELEGATION_DISPATCHED, "派出", now)
        app.record_repo.append(
            tid, RecordType.DELEGATION_FAILED, "闸门缺口", now
        )
        brief = build_brief(
            task, task.intent, None,
            records=app.record_repo.list_for_task(tid),
        )
        assert "\n" not in brief, "历史段同样必须单行"
        assert "历史记录" in brief
        assert "闸门缺口" in brief
        # 只摆事实：不允许出现指令类语（12.10.2 末条）。
        for banned in ("建议", "务必", "注意", "请务必"):
            assert banned not in brief.split("历史记录")[-1], (
                f"历史段里不该有指令类语「{banned}」"
            )

    def test_repeated_failures_carry_the_count(self, app, work, tmp_path):
        tid = make_delegation(app, work, str(tmp_path))
        task = app.tasks.get(tid)
        for i in range(2):
            base = app.clock.now()
            app.record_repo.append(
                tid, RecordType.DELEGATION_DISPATCHED, f"第{i}次派出", base
            )
            app.record_repo.append(
                tid, RecordType.DELEGATION_FAILED, "又挂了", base
            )
        brief = build_brief(
            task, task.intent, None,
            records=app.record_repo.list_for_task(tid),
        )
        assert "累计失败 2 次" in brief

    def test_refused_history_is_not_injected(self, app, work, tmp_path):
        """refused（派发前被拒）不进历史段 —— 连执行都没进过。"""
        tid = make_delegation(app, work, str(tmp_path))
        task = app.tasks.get(tid)
        now = app.clock.now()
        app.record_repo.append(tid, RecordType.DELEGATION_DISPATCHED, "派出", now)
        app.record_repo.append(
            tid, RecordType.DELEGATION_FAILED, "闸门缺口", now
        )
        # 再来一次 succeeded 前置的 failed：形状上 in_flight 已结、
        # 结局判定按 attempt_state 计算 —— 这里直接控制在 refused 形状：
        # failed 排在 dispatched 之后同刻等场景过于精细，用最直白的
        # 「只有一条失败、无 dispatched」形状即可判 refused。
        app2_records = [
            r for r in app.record_repo.list_for_task(tid)
            if r.type is not RecordType.DELEGATION_DISPATCHED
        ]
        brief = build_brief(task, task.intent, None, records=list(app2_records))
        assert "历史记录" not in brief

    def test_derivation_must_come_from_feedback_module(self):
        """**不许复制推导**（12.1.1）：failure_history 的判定必须复用
        feedback.read_outcome —— 两份判定一漂移，简报和履历就打架。"""
        import inspect

        from freeagent.services import delegate as svc

        src = inspect.getsource(svc.failure_history_line)
        assert "read_outcome" in src, "必须调用 feedback.read_outcome"
        assert "def determine" not in src and "attempt_state(" not in src
        # feedback 模块头也没从简报侧反向引用（防循环、防双实现）。
        from freeagent.services import feedback as fb

        assert "build_brief" not in inspect.getsource(fb)

# =============================================================================
# 协作象限标注（12.10.12 / V1.103）
# =============================================================================
class TestBriefQuadrant:
    """四象限判的是「谁手里有做法的一手材料」，不是「谁更懂」。

    * 讲授 = 人给过做法，机器没跑过
    * 共识 = 两边都有
    * 暗知 = 机器跑过（留下账），人没给做法
    * 双盲 = 两边都空

    两条纪律用测试守着：① 判空必须复用产出那段的那份推导（不另写规则）；
    ② 象限**只标注不决策** —— 它绝不出现在派发资格/阻断判定里。
    """

    def _failed(self, app, tid):
        now = app.clock.now()
        app.record_repo.append(tid, RecordType.DELEGATION_DISPATCHED, "派出", now)
        app.record_repo.append(
            tid, RecordType.DELEGATION_FAILED, "闸门缺口", now
        )
        return app.record_repo.list_for_task(tid)

    def test_nothing_on_either_side_is_blind(self):
        q = brief_quadrant()
        assert (q.quadrant, q.label) == ("blind", "双盲")
        assert q.human_side == () and q.machine_side == ()
        assert q.says_how_much and q.what_to_add

    def test_plan_alone_is_teach(self):
        q = brief_quadrant(accepted_plan="先做接口再做界面")
        assert (q.quadrant, q.label) == ("teach", "讲授")
        assert q.human_side == ("你已采纳的方案",)
        assert q.machine_side == ()

    def test_conventions_alone_is_teach(self):
        q = brief_quadrant(conventions=("这个项目不用 npm",))
        assert q.quadrant == "teach"
        assert q.human_side == ("人记过的约定",)

    def test_machine_records_alone_is_tacit(self, app, work, tmp_path):
        tid = make_delegation(app, work, str(tmp_path))
        q = brief_quadrant(records=self._failed(app, tid))
        assert (q.quadrant, q.label) == ("tacit", "暗知")
        assert q.human_side == ()
        assert q.machine_side == ("本任务上次失败的错因",)
        # 暗知最危险的误读：把机器的账当人定的规则。
        assert "不是人定的规则" in q.brief_line

    def test_last_attempt_alone_is_tacit(self):
        q = brief_quadrant(last_attempt="上次实际用的模型：claude-opus-5")
        assert q.quadrant == "tacit"
        assert q.machine_side == ("上次实际用的模型与工具",)

    def test_both_sides_is_shared(self, app, work, tmp_path):
        tid = make_delegation(app, work, str(tmp_path))
        q = brief_quadrant(
            accepted_plan="先做接口再做界面",
            records=self._failed(app, tid),
        )
        assert (q.quadrant, q.label) == ("shared", "共识")
        assert set(q.human_side) and set(q.machine_side)

    def test_blank_input_counts_as_nothing(self):
        """空白不是材料 —— 否则会出现「讲授」但正文里一个字的方案都没有。"""
        q = brief_quadrant(accepted_plan="   ", conventions=("  ", " "))
        assert q.quadrant == "blind"

    def test_unrelated_project_history_is_not_material(self, app, work, tmp_path):
        """同项目「其它任务」若没踩过坑，不算机器有账（判空走同一份推导）。"""
        other = app.tasks.create(
            "另一次委派", [work.id], intent="在别处完成",
            project_path=str(tmp_path),
        )
        app.record_repo.append(
            other.id, RecordType.DELEGATION_DISPATCHED, "派出", app.clock.now()
        )
        history = [(other.title, app.record_repo.list_for_task(other.id))]
        q = brief_quadrant(project_history=history)
        assert q.quadrant == "blind"
        assert project_history_line(history) == ""

    # ---- 进简报的那一句：只在会被误读时说话 ---- #

    def test_teach_and_shared_add_nothing(self, app, work, tmp_path):
        """人有材料时一个字不加 —— 材料就在上面，摆着比说着有力。"""
        tid = make_delegation(app, work, str(tmp_path))
        task = app.tasks.get(tid)
        teach = build_brief(
            task, task.intent, None, accepted_plan="先做接口再做界面"
        )
        assert "本次没有" not in teach
        shared = build_brief(
            task, task.intent, None,
            accepted_plan="先做接口再做界面",
            records=self._failed(app, tid),
        )
        assert "本次没有" not in shared

    def test_blind_names_all_three_missing_pieces(self, app, work, tmp_path):
        tid = make_delegation(app, work, str(tmp_path))
        task = app.tasks.get(tid)
        brief = build_brief(task, task.intent, None)
        assert "本次没有已采纳方案、约定和历史执行记录" in brief

    def test_tacit_says_records_are_not_rules(self, app, work, tmp_path):
        tid = make_delegation(app, work, str(tmp_path))
        task = app.tasks.get(tid)
        brief = build_brief(
            task, task.intent, None, records=self._failed(app, tid)
        )
        assert "不是人定的规则" in brief

    def test_quadrant_does_not_gate_anything(self):
        """象限是**标注**不是闸门：不许出现在资格/阻断判定里（12.10.2）。"""
        import inspect

        from freeagent.services import delegate as svc

        for fn in (svc.eligible_tasks, svc.dispatch_blocker,
                   svc.already_dispatched):
            assert "quadrant" not in inspect.getsource(fn), (
                f"{fn.__name__} 不该读象限"
            )

    def test_brief_is_assembled_in_exactly_one_place(self):
        """预览与实发必须逐字相同 —— 组装只能有一处（12.1.1）。"""
        import inspect

        from freeagent import delegate as outer

        src = inspect.getsource(outer.run_once_with_tool_gate)
        assert "assemble_brief(app, task).text" in src
        assert "build_brief(" not in src, "不许再内联拼装第二份"

    def test_assemble_brief_collects_the_same_parts(self, app, work, tmp_path):
        """``assemble_brief`` 的产物与手工拼 build_brief 一致。"""
        from freeagent.delegate import assemble_brief

        tid = make_delegation(app, work, str(tmp_path))
        records = self._failed(app, tid)
        task = app.tasks.get(tid)
        assembled = assemble_brief(app, task)
        manual = build_brief(
            task, task.intent, task.definition_of_done,
            records=records,
            project_history=sibling_history(app.task_repo, app.record_repo, task),
            accepted_plan="",
            last_attempt=last_attempt_line(app.artifacts.list_for_task(tid)),
            conventions=(),
        )
        assert assembled.text == manual
        assert assembled.quadrant.quadrant == "tacit"


# =============================================================================
# 启动恢复（12.11.1 / V1.39）
# =============================================================================
class TestReclaimAbandoned:
    """进程死了不是终点：悬置的委派要被认领成「执行期失败」，然后交还给人。"""

    def test_stale_pending_is_claimed_as_failure(self, app, work, db_path):
        """挂起待批 + 无投影 ⇒ 认领：落一条执行期失败记录，给出两条出路。"""
        from freeagent.services.approval import ApprovalStore

        store = ApprovalStore(app.conn, clock=app.clock.now)
        tid = make_delegation(app, work, str(db_path))
        store.request_delegation(
            project=str(db_path), brief="做不到也行，只为测试", task_id=tid
        )
        # 没有投影行 → 按陈旧处理。db_path 必须与夹具同一个库。
        notes = reclaim_abandoned(db_path=str(db_path), clock=app.clock)
        assert len(notes) == 1
        records = app.record_repo.list_for_task(tid)
        # 创建时追加了 CREATED，认领追加 FAILED —— 两条，最后一条是认领。
        assert len(records) == 2
        assert records[-1].type is RecordType.DELEGATION_FAILED
        content = records[-1].content or ""
        assert "执行器中断后无人续上" in content
        assert "/redispatch" in content
        assert "/drop" in content

    def test_second_run_does_not_duplicate(self, app, work, db_path):
        """执行器反复重启不该把失败记录刷成瀑布 —— 幂等认领。"""
        from freeagent.services.approval import ApprovalStore

        store = ApprovalStore(app.conn, clock=app.clock.now)
        tid = make_delegation(app, work, str(db_path))
        store.request_delegation(project=str(db_path), brief="x", task_id=tid)
        assert len(reclaim_abandoned(db_path=str(db_path), clock=app.clock)) == 1
        assert reclaim_abandoned(db_path=str(db_path), clock=app.clock) == ()
        # CREATED + 一条认领 FAILED —— 幂等，不能有第三条。
        assert len(app.record_repo.list_for_task(tid)) == 2

    def test_fresh_progress_leaves_the_delegation_alone(self, app, work, db_path):
        """投影新鲜 ⇒ 执行器多半还活着 —— 宁可漏认领，不许把活的记成死的。"""
        from freeagent.services.approval import ApprovalStore

        store = ApprovalStore(app.conn, clock=app.clock.now)
        tid = make_delegation(app, work, str(db_path))
        app.task_repo.set_delegate_progress(
            task_id=tid, status="跑", recent="", tool_calls=0, allowed=0,
            project=str(db_path), now=app.clock.now(),
        )
        store.request_delegation(project=str(db_path), brief="x", task_id=tid)
        notes = reclaim_abandoned(db_path=str(db_path), clock=app.clock)
        assert notes == ()
        # 只有创建时的 CREATED —— 没追加认领记录。
        records = app.record_repo.list_for_task(tid)
        assert all(r.type is not RecordType.DELEGATION_FAILED for r in records)

    def test_pending_without_task_id_is_untouched(self, app, db_path):
        """没归属的待决项不是委派 —— 恢复清单不该把手伸向本地操作。"""
        from freeagent.services.approval import ApprovalStore

        store = ApprovalStore(app.conn, clock=app.clock.now)
        store.request_tool_call(permission="写文件", paths=("a.txt",))
        notes = reclaim_abandoned(db_path=str(db_path), clock=app.clock)
        assert notes == ()


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


class TestToolGatePathRecordsDispatch:
    """默认派发路径（执行期逐次授权）必须写下 ``DELEGATION_DISPATCHED``。

    回归：这条路此前**只占租约、不写派出记录**，于是 ``attempt_state``
    按「``failed`` 无前置 dispatched = 派发前被拒」把执行期失败判成
    ``refused`` —— 模型 402、执行器崩溃这类**可重试**的失败从此永不
    自动重派，``/feedback`` 还把它归进「派发前被拒」并附上一句并不成立
    的理由。

    执行全程离线：``run_with_tool_gate`` 用 monkeypatch 换掉，不起 opencode。
    """

    @staticmethod
    def _started(tmp_path: Path, project: Path) -> str:
        app = build_app(tmp_path / "a.db")
        try:
            role = app.roles.create("工作")
            task = app.tasks.create(
                "改点东西", [role.id], project_path=str(project)
            )
            return app.tasks.start(task.id).id
        finally:
            app.close()

    @staticmethod
    def _records(tmp_path: Path, task_id: str):
        app = build_app(tmp_path / "a.db")
        try:
            return app.record_repo.list_for_task(task_id)
        finally:
            app.close()

    def test_execution_failure_stays_retryable(self, tmp_path, monkeypatch):
        """执行期失败 ⇒ ``failed``（可重派），不是 ``refused``。"""
        import freeagent.delegate as delegate_module

        project = tmp_path / "proj"
        project.mkdir()
        task_id = self._started(tmp_path, project)
        monkeypatch.setattr(
            delegate_module, "run_with_tool_gate",
            lambda *a, **k: DispatchOutcome(ok=False, summary="模型 402：余额不足"),
        )
        report = run_once_with_tool_gate(
            db_path=tmp_path / "a.db", sender=None, approver="ou_x",
            policy=DelegationPolicy(projects=(str(project),)),
        )
        assert report.failed == 1

        records = self._records(tmp_path, task_id)
        assert RecordType.DELEGATION_DISPATCHED in {r.type for r in records}, (
            "默认路径必须留下派出记录，否则失败无法被正确分类"
        )
        assert attempt_state(records) == (False, "failed")
        assert already_dispatched(records) is False, "可重试的失败不该被跳过"

    def test_whitelist_refusal_leaves_no_dispatch_record(
        self, tmp_path, monkeypatch
    ):
        """白名单拒绝 ⇒ **不写**派出记录，且根本没进执行。

        顺序是这条语义的前提：先记派出再检查白名单的话，被拒的委派会
        留下「已派出」的痕迹，``refused`` 这个类别就不存在了。
        """
        import freeagent.delegate as delegate_module

        project = tmp_path / "proj"
        project.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        task_id = self._started(tmp_path, project)
        called: list = []
        monkeypatch.setattr(
            delegate_module, "run_with_tool_gate",
            lambda *a, **k: called.append(1),
        )
        run_once_with_tool_gate(
            db_path=tmp_path / "a.db", sender=None, approver="ou_x",
            policy=DelegationPolicy(projects=(str(elsewhere),)),
        )
        assert called == [], "白名单之外的项目绝不能进执行"

        records = self._records(tmp_path, task_id)
        assert RecordType.DELEGATION_DISPATCHED not in {r.type for r in records}
        assert attempt_state(records)[1] == "refused"


class TestBriefCarriesProjectHistory:
    """同项目其它委派的踩坑史进简报（12.10.7 扩到项目级）。

    执行器每次都是一个新生儿：它拿不到的是**这个项目以前败在哪**，
    而那恰恰是「上层」唯一该给下层的东西。本任务级历史 V1.38 已有；
    这一组锁的是**项目级**的聚合与边界。
    """

    def _mk(self, tmp_path):
        app = build_app(tmp_path / "a.db")
        role = app.roles.create("工作")
        return app, role

    def _failed(self, app, role_id, project, title, reason):
        t = app.tasks.create(title, [role_id], project_path=str(project))
        t = app.tasks.start(t.id)
        now = app.clock.now()
        app.record_repo.append(t.id, RecordType.DELEGATION_DISPATCHED, "派出", now)
        app.record_repo.append(t.id, RecordType.DELEGATION_FAILED, reason, now)
        return t

    def test_sibling_failure_enters_brief(self, tmp_path):
        app, role = self._mk(tmp_path)
        try:
            project = tmp_path / "proj"
            project.mkdir()
            self._failed(app, role.id, project, "装依赖", "npm 安装失败：网络超时")
            current = app.tasks.create("写测试", [role.id], project_path=str(project))
            current = app.tasks.start(current.id)
            brief = build_brief(
                current, current.intent, current.definition_of_done,
                records=app.record_repo.list_for_task(current.id),
                project_history=sibling_history(
                    app.task_repo, app.record_repo, current
                ),
            )
            assert "同项目其它委派的历史" in brief
            assert "装依赖" in brief
            assert "npm 安装失败" in brief
            assert "\n" not in brief, "简报必须单行"
        finally:
            app.close()

    def test_other_project_and_self_are_excluded(self, tmp_path):
        """别的项目与本任务自己都不得混入。"""
        app, role = self._mk(tmp_path)
        try:
            pa = tmp_path / "a"
            pb = tmp_path / "b"
            pa.mkdir(); pb.mkdir()
            self._failed(app, role.id, pb, "别项目的坑", "败因不该出现")
            current = app.tasks.create("当前", [role.id], project_path=str(pa))
            current = app.tasks.start(current.id)
            now = app.clock.now()
            app.record_repo.append(
                current.id, RecordType.DELEGATION_DISPATCHED, "派出", now
            )
            app.record_repo.append(
                current.id, RecordType.DELEGATION_FAILED, "本任务自己的坑", now
            )
            entries = sibling_history(app.task_repo, app.record_repo, current)
            assert entries == [], "本任务与别项目都该被排除"
        finally:
            app.close()

    def test_bounded_to_three_and_clipped(self, tmp_path):
        """默认最多三条、单条错因 160 字内：简报是工作记忆不是编年史。"""
        app, role = self._mk(tmp_path)
        try:
            project = tmp_path / "proj"
            project.mkdir()
            for i in range(5):
                self._failed(app, role.id, project, f"任务{i}", "错因" * 200)
            current = app.tasks.create("当前", [role.id], project_path=str(project))
            entries = sibling_history(app.task_repo, app.record_repo, current)
            line = project_history_line(entries)
            assert line.count("执行失败") == 3, "默认有界三条"
            assert ("错因" * 200) not in line, "错因必须被截断"
        finally:
            app.close()

    def test_no_failure_no_block(self, tmp_path):
        app, role = self._mk(tmp_path)
        try:
            project = tmp_path / "proj"
            project.mkdir()
            app.tasks.create("没跑过的", [role.id], project_path=str(project))
            current = app.tasks.create("当前", [role.id], project_path=str(project))
            entries = sibling_history(app.task_repo, app.record_repo, current)
            assert project_history_line(entries) == ""
        finally:
            app.close()


class TestBriefCarriesAcceptedPlan:
    """已采纳的方案进简报（12.10.8）—— 人定的方案优先于机器转述。"""

    def test_accepted_plan_enters_brief(self, tmp_path):
        app = build_app(tmp_path / "a.db")
        try:
            role = app.roles.create("工作")
            t = app.tasks.create("改登录", [role.id])
            t = app.tasks.start(t.id)
            draft = app.artifacts.create_draft(
                t.id, "方案", "第一步改鉴权中间件，第二步补测试"
            )
            app.artifacts.accept(draft.id)
            brief = build_brief(
                t, t.intent, t.definition_of_done,
                accepted_plan=app.artifacts.accepted(t.id).content,
            )
            assert "你已经定下的方案，按它实施" in brief
            assert "第一步改鉴权中间件" in brief
            assert "\n" not in brief
        finally:
            app.close()

    def test_draft_only_does_not_enter_via_helper(self, tmp_path):
        """只采纳的才算：草稿未采纳时 ``_accepted_plan_text`` 必须给空串。"""
        from freeagent.delegate import _accepted_plan_text

        app = build_app(tmp_path / "a.db")
        try:
            role = app.roles.create("工作")
            t = app.tasks.create("改登录", [role.id])
            app.artifacts.create_draft(t.id, "方案", "还没定的草案")
            assert _accepted_plan_text(app, t.id) == ""
            draft = app.artifacts.list_for_task(t.id)[0]
            app.artifacts.accept(draft.id)
            assert _accepted_plan_text(app, t.id) == "还没定的草案"
        finally:
            app.close()

    def test_long_plan_is_clipped_with_marker(self, tmp_path):
        app = build_app(tmp_path / "a.db")
        try:
            role = app.roles.create("工作")
            t = app.tasks.create("x", [role.id])
            t = app.tasks.start(t.id)
            brief = build_brief(t, None, None, accepted_plan="长" * 2000)
            assert "方案过长，此处截断" in brief, "截断必须留标记（3.6）"
            assert "长" * 2000 not in brief
            assert "\n" not in brief
        finally:
            app.close()

    def test_plan_precedes_failure_history(self, tmp_path):
        """方案是要照做的正文，历史是背景 —— 方案必须在历史之前。"""
        app = build_app(tmp_path / "a.db")
        try:
            role = app.roles.create("工作")
            t = app.tasks.create("改登录", [role.id])
            t = app.tasks.start(t.id)
            now = app.clock.now()
            app.record_repo.append(t.id, RecordType.DELEGATION_DISPATCHED, "派出", now)
            app.record_repo.append(t.id, RecordType.DELEGATION_FAILED, "上次败因", now)
            brief = build_brief(
                t, None, None,
                records=app.record_repo.list_for_task(t.id),
                accepted_plan="照这个方案做",
            )
            assert brief.index("你已经定下的方案") < brief.index("历史记录")
        finally:
            app.close()


class TestAttemptLedger:
    """一次委派的账：实际模型 + 用过的工具（12.10.9 / schema v27+v28）。

    在此前这两个事实只写在产物正文的 HTML 注释里 —— 给人看的格式，
    读侧要用就得解析自己的文案（「把文案当契约」，11.9.12 禁掉的那类事）。
    """

    def test_roundtrip_and_enters_brief(self, tmp_path):
        app = build_app(tmp_path / "a.db")
        try:
            role = app.roles.create("工作")
            t = app.tasks.create("改登录", [role.id])
            t = app.tasks.start(t.id)
            app.artifacts.create_draft(
                t.id, "opencode 产出", "正文",
                model="opencode/big-pickle", tool_calls="edit、bash",
            )
            # 用户手写草稿两列皆空，天然被排除 —— 不靠标题认「执行器产出」。
            app.artifacts.create_draft(t.id, "我的草稿", "手写方案")
            line = last_attempt_line(app.artifacts.list_for_task(t.id))
            assert "opencode/big-pickle" in line
            assert "edit、bash" in line
            assert "\n" not in line
            brief = build_brief(t, None, None, last_attempt=line)
            assert "上次实际用的模型" in brief
        finally:
            app.close()

    def test_no_attempt_no_line(self, tmp_path):
        app = build_app(tmp_path / "a.db")
        try:
            role = app.roles.create("工作")
            t = app.tasks.create("x", [role.id])
            assert last_attempt_line(app.artifacts.list_for_task(t.id)) == ""
            app.artifacts.create_draft(t.id, "草稿", "手写")
            assert last_attempt_line(app.artifacts.list_for_task(t.id)) == ""
        finally:
            app.close()



class TestBriefCarriesConventions:
    """人记的「约定」进简报（12.10.10）—— 世界的事实，不是人的倾向。

    这与 11.9.11「不做自我进化」不冲突：那条否掉的是**学人的倾向**，
    同时写明该积累的是世界的事实 —— 约定是人**显式**记下的事实，
    简报只是复述，不推断。
    """

    def test_enters_brief_with_qualifier(self, tmp_path):
        app = build_app(tmp_path / "a.db")
        try:
            role = app.roles.create("工作")
            t = app.tasks.create("改登录", [role.id])
            brief = build_brief(
                t, None, None,
                conventions=["本项目禁用 requests，只用标准库"],
            )
            assert "人记过的约定" in brief
            assert "可能过期，以当前为准" in brief, "记忆不是事实，定性必须带"
            assert "禁用 requests" in brief
            assert "\n" not in brief
        finally:
            app.close()

    def test_bounded_and_clipped(self):
        line = conventions_line(["约" * 300] * 5)
        assert "约" * 121 not in line, "单条必须截到 120 字"
        assert "约" * 120 in line, "截断不是丢弃 —— 120 字内必须保留"
        assert line.count("；") == 2, "五条只取三条"

    def test_empty_gives_empty_line(self):
        assert conventions_line([]) == ""
        assert conventions_line(["", "   "]) == ""

    def test_conventions_precede_failure_history(self, tmp_path):
        """约定是人定的规则，错因是机器的历史 —— 前者排在前面。"""
        app = build_app(tmp_path / "a.db")
        try:
            role = app.roles.create("工作")
            t = app.tasks.create("x", [role.id])
            t = app.tasks.start(t.id)
            now = app.clock.now()
            app.record_repo.append(t.id, RecordType.DELEGATION_DISPATCHED, "派出", now)
            app.record_repo.append(t.id, RecordType.DELEGATION_FAILED, "上次败因", now)
            brief = build_brief(
                t, None, None,
                records=app.record_repo.list_for_task(t.id),
                conventions=["禁止动配置文件"],
            )
            assert brief.index("人记过的约定") < brief.index("历史记录")
        finally:
            app.close()
