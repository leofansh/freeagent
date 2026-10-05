"""闸门默认开启后，「没配飞书」这条路径的可见性与出路。

## 为什么要有这个测试

闸门默认开之后，**没配飞书通道的人从「能用」变成「派不出」**。
这是有意的（默认值该指向安全），但按设计文档 12.7.2 的 R3：
**拒绝必须被看见，且必须给出路** —— 只报一句「不派发」，
用户会以为程序坏了，而「能用」变成「不能用」时不给出路，
就是在制造静默失败。

这些断言盯的是**文案里有没有可执行的下一步**，不是文案好不好听。
"""

from pathlib import Path

from freeagent.app import build_app
from freeagent.config import Config
from freeagent.delegate import (
    ApprovalStore,
    DispatchOutcome,
    run_once_with_tool_gate,
    run_with_tool_gate,
)
from freeagent.delegate import _delegate_policy


def _a_started_delegation(tmp_path: Path, project: Path):
    """造一条「已开始」的委派事务。

    ``eligible_tasks`` 只认三件事（services/delegate.py:254）：
    ``project_path`` 非空、``state is ACTIVE``、没派过或上次失败。
    **``kind`` 与它无关** —— 委派不是一种 kind。

    白名单**不在这里**配：``run_once_with_tool_gate`` 自己 ``build_app``，
    不接受外部注入的 Config。所以策略由调用方通过 ``policy=`` 传进去 ——
    这也正是 ``main()`` 做的事（``--model`` 覆盖就是走这条参数）。
    """
    app = build_app(tmp_path / "a.db")
    role = app.roles.create("工作")
    task = app.tasks.create("改点东西", [role.id], project_path=str(project))
    task = app.tasks.start(task.id)
    app.close()
    return task.id


def _policy_for(project: Path):
    from freeagent.services.delegate import DelegationPolicy

    return DelegationPolicy(projects=(str(project),))


def test_no_sender_refuses_to_dispatch(tmp_path):
    """没有发卡器 = 没有人能批 = 不派发。**绝不静默退化成无人值守。**"""
    app = build_app(tmp_path / "a.db")
    try:
        outcome = run_with_tool_gate(
            task=type("T", (), {"id": "x" * 32, "intent": "y", "title": "z",
                                "definition_of_done": "", "project_path": ""})(),
            project=tmp_path,
            brief="b",
            policy=_delegate_policy(app),
            store=ApprovalStore(app.conn),
            sender=None,
            approver="ou_x",
        )
    finally:
        app.close()
    assert outcome.ok is False


def test_refusal_explains_every_way_out(tmp_path):
    """拒绝文案必须包含**三条真实出路**，且逐条可执行。

    这是 R3 的可测形式。特别盯住 ``--no-tool-gate``：
    闸门默认开启之后，「怎么回到老路」必须**显眼地**写在拒绝理由里 ——
    否则用户只会去改配置，而不会想到命令行上有个开关。
    """
    app = build_app(tmp_path / "a.db")
    try:
        outcome = run_with_tool_gate(
            task=type("T", (), {"id": "x" * 32, "intent": "y", "title": "z",
                                "definition_of_done": "", "project_path": ""})(),
            project=tmp_path,
            brief="b",
            policy=_delegate_policy(app),
            store=ApprovalStore(app.conn),
            sender=None,
            approver="ou_x",
        )
    finally:
        app.close()
    summary = outcome.summary or ""
    assert "飞书" in summary, "必须说清缺的是什么"
    assert "run_feishu" in summary, "出路 1：给出配通道的具体入口"
    assert "--no-tool-gate" in summary, "出路 2：显式关闸的旗标必须写出来"
    assert "--dry-run" in summary, "出路 3：预演"


def test_dry_run_still_works_without_feishu(tmp_path):
    """**预演不受闸门影响** —— 它不派发，所以不需要人批。

    这条很重要：闸门默认开之后，如果连「先看看会派什么」都被堵住，
    用户就失去了唯一的安全探查手段，只能盲改配置。
    """
    project = tmp_path / "proj"
    project.mkdir()
    _a_started_delegation(tmp_path, project)

    report = run_once_with_tool_gate(
        db_path=tmp_path / "a.db", dry_run=True, sender=None, approver="ou_x",
        policy=_policy_for(project),
    )
    assert report.dispatched == 0
    assert any("将派发" in n for n in report.notes), (
        f"预演应当列出将要派发的内容，实际 notes={report.notes}"
    )


def test_outcome_type_is_unchanged():
    """守住一个隐含契约：这是个 ``DispatchOutcome``，不是裸 dict/tuple。"""
    assert isinstance(
        DispatchOutcome(ok=False, summary="x"), DispatchOutcome
    )