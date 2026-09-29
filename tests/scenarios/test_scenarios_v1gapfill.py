"""S7 — 补齐后的 V1 功能端到端：草稿全生命周期 + 角色全生命周期 + 等候跟进。

S1–S6 见 ``test_scenarios.py``。这里覆盖的是 13.1 里此前只有服务层、
没有入口的那几条。
"""

from __future__ import annotations

import io
from datetime import date, datetime, timedelta

import pytest

from freeagent.app import App
from freeagent.cli.app import Repl
from freeagent.domain import ArtifactStatus, TaskKind, TaskState
from freeagent.services.clock import FrozenClock
from freeagent.services.sorting import DISCLAIMER_MARKER

NOW = datetime(2026, 9, 26, 9, 0)


@pytest.fixture()
def cli(app: App, roles):
    return Repl(app, out=io.StringIO())


def _say(cli: Repl, *lines: str) -> str:
    cli.out.truncate(0)
    cli.out.seek(0)
    for line in lines:
        cli.handle(line)
    return cli.out.getvalue()


class TestS7DraftLifecycle:
    """文档 5.2：版本化 + 采纳语义 + 永不原地覆写。"""

    def test_draft_revise_accept_chain(self, app: App, roles, cli):
        tid = app.tasks.create(
            "下周二要交的销售周报初稿", [roles["work"].id], intent="先理一版"
        ).id
        key = tid[:8]

        out = _say(cli, f"/draft {key} 重点讲增长")
        assert "version 1" in out and "[TODO]" in out

        _say(cli, f"/start {key}")
        _say(cli, f"/note {key} 草稿写到有了框架，还没填数据")
        # 先采纳 v1，再改 —— 文档规则 2：改一个「已采纳」的产物才会把它降级
        assert "已采纳 version 1" in _say(cli, f"/accept {key}")
        _say(cli, f"/revise {key} 补上数据后的版本")
        _say(cli, f"/accept {key}")

        versions = app.artifacts.list_for_task(tid)
        assert [a.version for a in versions] == [1, 2]
        assert app.artifacts.accepted(tid).version == 2
        # 旧版永不删除、且被新版本取代后降为 SUPERSEDED
        assert versions[0].status is ArtifactStatus.SUPERSEDED
        assert versions[0].content != versions[1].content

    def test_unaccepted_draft_is_not_superseded_by_revision(self, app: App, roles, cli):
        """没采纳过的草稿被改时保持 DRAFT —— superseded 只表示「被取代且已结账」。"""
        tid = app.tasks.create("周报", [roles["work"].id], intent="先理一版").id
        key = tid[:8]
        _say(cli, f"/draft {key}")
        _say(cli, f"/revise {key} 第二版")
        versions = {a.version: a for a in app.artifacts.list_for_task(tid)}
        assert versions[1].status is ArtifactStatus.DRAFT
        assert versions[2].supersedes == versions[1].id, "版本链要连上"

    def test_draft_is_derived_from_task_fields_only(self, app: App, roles, cli):
        """草稿只复述已有信息，未知处标 [TODO]（文档第十五章：不伪造事实）。"""
        tid = app.tasks.create("写季度总结", [roles["work"].id]).id
        out = _say(cli, f"/draft {tid[:8]}")
        art = app.artifacts.current(tid)
        assert "写季度总结" in art.content
        assert "[TODO]" in art.content
        # 不该凭空出现具体数字
        assert not any(ch.isdigit() for ch in art.content)

    def test_reopening_does_not_lose_history(self, app: App, roles, cli):
        tid = app.tasks.create("周报", [roles["work"].id], intent="先理一版").id
        key = tid[:8]
        _say(cli, f"/draft {key}")
        _say(cli, f"/accept {key}")
        _say(cli, f"/revise {key} 第二版")
        _say(cli, f"/accept {key}")
        out = _say(cli, f"/artifact {key}")
        assert "v1" in out and "v2" in out
        assert "旧版全部保留" in out

    def test_interrupted_draft_restores_within_two_turns(self, app: App, roles, cli, clock):
        """H2 判据在草稿路径上同样成立：打开 1 轮 + 执行 1 轮。"""
        tid = app.tasks.create("周报", [roles["work"].id], intent="先理一版").id
        key = tid[:8]
        _say(cli, f"/draft {key}")
        _say(cli, f"/start {key}")
        _say(cli, f"/note {key} 有框架了")

        clock.advance(days=3, hours=1)
        # 第 1 轮：打开
        view = app.restore.open_task(tid)
        assert view.current_artifact is not None and view.current_artifact.version == 1
        assert any("version 1" in a for a in view.next_actions)
        # 第 2 轮：直接重做，不需要再查任何东西
        app.artifacts.revise(tid, view.current_artifact.content + "\n- 补数据")
        assert app.artifacts.current(tid).version == 2


class TestS7RoleLifecycle:
    """文档 3.5：改名 / 静置 / 合并 / 删除的完整生命周期。"""

    def test_full_role_lifecycle(self, app: App, roles, cli):
        # 改名
        _say(cli, "/role-rename 跑腿杂项 杂事")
        assert app.roles.find_by_name("杂事") is not None
        # 静置后从默认列表消失，数据保留
        _say(cli, "/role-silence 杂事")
        assert "杂事" not in _say(cli, "/roles")
        assert "另有 1 个静置角色" in _say(cli, "/roles")
        assert "杂事" in _say(cli, "/roles all")
        # 恢复
        _say(cli, "/role-silence 杂事 on")
        assert "杂事" in _say(cli, "/roles")
        # 删除（无事务）
        _say(cli, "/role-del 杂事")
        assert app.roles.find_by_name("杂事") is None

    def test_delete_with_tasks_is_refused_with_guidance(self, app: App, roles, cli):
        app.tasks.create("家里的事", [roles["family"].id])
        out = _say(cli, "/role-del 家庭")
        assert "不能删" in out and "/merge" in out and "/move" in out

    def test_merge_then_delete_flow(self, app: App, roles, cli):
        """先 merge 把事务搬走 —— 之后源角色就不再是「可操作的角色」了。"""
        t = app.tasks.create("跨脉络", [roles["work"].id, roles["family"].id])
        _say(cli, "/merge 家庭 工作项目A")
        assert app.task_repo.get(t.id).role_ids == (roles["work"].id,)
        # 命令层按名字找不到它了（已合并），但原始行还在、仍可解析
        out = _say(cli, "/role-del 家庭")
        assert "找不到角色" in out
        assert app.roles.find_by_name("家庭") is None
        assert app.roles.get(roles["family"].id).merged_into == roles["work"].id
        assert app.roles.resolve(roles["family"].id).id == roles["work"].id

    def test_silence_preserves_tasks_and_merge(self, app: App, roles, cli):
        t = app.tasks.create("家里的事", [roles["family"].id])
        _say(cli, "/role-silence 家庭")
        assert app.task_repo.get(t.id) is not None
        # 静置角色仍可被合并
        _say(cli, "/merge 家庭 工作项目A")
        assert app.roles.resolve(roles["family"].id).id == roles["work"].id


class TestS7WaitingFollowup:
    """文档 9.5：跟进可更新，且真的驱动 OVERDUE_WAIT。"""

    def test_followup_drives_the_signal_end_to_end(self, app: App, roles, cli, clock):
        from freeagent.domain import WaitingKind, WaitingOn

        tid = app.tasks.create(
            "等孩子带材料回来", [roles["family"].id], kind=TaskKind.WAIT,
            waiting_on=WaitingOn(WaitingKind.PERSON, "孩子", NOW),
        ).id
        app.tasks.schedule(tid, date(2026, 9, 26))
        key = tid[:8]

        # 还没设跟进时间 → 不该有 OVERDUE_WAIT
        assert "该催" not in _say(cli, "/today")

        # 设一个已过期的跟进时间
        _say(cli, f"/followup {key} 昨天")
        out = _say(cli, "/today")
        assert "该催 孩子 了" in out
        # 权重最高，应排在最前
        numbered = [ln for ln in out.splitlines() if ln.strip().startswith("1.")]
        assert numbered and "等孩子带材料回来" in numbered[0], out

        # 跟进一次并改到未来 → 信号消失
        _say(cli, f"/note {key} 催过了")
        _say(cli, f"/followup {key} 3天后")
        assert "该催 孩子 了" not in _say(cli, "/today")

    def test_waiting_can_be_closed_directly(self, app: App, roles, cli):
        """等候类到期即销账（文档 4.4）。"""
        from freeagent.domain import WaitingKind, WaitingOn

        tid = app.tasks.create(
            "等合同回复", [roles["work"].id], kind=TaskKind.WAIT,
            waiting_on=WaitingOn(WaitingKind.PERSON, "对方", NOW),
        ).id
        _say(cli, f"/done {tid[:8]}")
        got = app.task_repo.get(tid)
        assert got.state is TaskState.DONE
        assert got.kind is TaskKind.WAIT, "终态不改 kind"
        assert got.waiting_on is None

    def test_waiting_can_become_an_action(self, app: App, roles, cli):
        from freeagent.domain import WaitingKind, WaitingOn

        tid = app.tasks.create(
            "等审批", [roles["work"].id], kind=TaskKind.WAIT,
            waiting_on=WaitingOn(WaitingKind.SYSTEM, "审批系统", NOW),
        ).id
        _say(cli, f"/start {tid[:8]}")
        got = app.task_repo.get(tid)
        assert got.state is TaskState.ACTIVE
        assert got.kind is TaskKind.ACTION, "离开 BLOCKED 应自动转动作类"
        assert got.waiting_on is None
        # 原等候信息进了历史，不会丢
        assert any("审批系统" in r.content for r in app.record_repo.list_for_task(tid))


class TestS7PlanAndAll:
    def test_plan_never_issues_a_verdict(self, app: App, roles, cli):
        tid = app.tasks.create("周报", [roles["work"].id]).id
        app.tasks.schedule(tid, date(2026, 9, 26))
        out = _say(cli, f"/plan {tid[:8]}")
        assert DISCLAIMER_MARKER in out
        assert "由你决定" in out

    def test_all_scopes_cover_the_full_lifecycle(self, app: App, roles, cli):
        a = app.tasks.create("开着的", [roles["work"].id]).id
        b = app.tasks.create("做完的", [roles["work"].id]).id
        c = app.tasks.create("放弃的", [roles["work"].id]).id
        app.tasks.complete(b)
        app.tasks.drop(c)
        assert "开着的" in _say(cli, "/all open")
        assert "做完的" not in _say(cli, "/all open")
        assert "做完的" in _say(cli, "/all closed")
        assert "放弃的" in _say(cli, "/all closed")
        everything = _say(cli, "/all all")
        assert all(t in everything for t in ("开着的", "做完的", "放弃的"))
