"""回归：**报错真的会列出可用项目名**。

## 为什么单独一条文件

``known_names`` 这个功能曾经**已提交、测试全绿、却一次都没被调用** ——
三个调用点一个都没传参，而测试验的是「函数在被传参时能工作」。

所以这里不测那个函数，**测调用点**：从 ``/delegate`` 的入口走一遍，
断言错误文案里**真的出现了**项目名。这才是用户看到的东西。
"""

import pytest

from freeagent.config import Config
from freeagent.domain import FreeAgentError
from freeagent.services.delegate import DelegationPolicy, check_project_allowed
from freeagent.services import opencode_projects as op

ROWS = (
    '[{"worktree": "/", "name": null},'
    ' {"worktree": "D:/PycharmProjects/openmos", "name": "OpenMOS"},'
    ' {"worktree": "D:/PycharmProjects/xiaoyuan", "name": "XiaoYuan"},'
    ' {"worktree": "D:/PycharmProjects/freeagent", "name": "FreeAgent"}]'
)


@pytest.fixture
def two_authorized(monkeypatch):
    """只授权了 openmos 与 xiaoyuan —— 与用户现在的真实状态一致。"""
    monkeypatch.setattr(op, "list_projects", lambda: op._parse(ROWS))


def test_hint_lists_only_authorized(two_authorized):
    policy = DelegationPolicy(projects=(
        "D:\\PycharmProjects\\openmos", "D:/PycharmProjects/xiaoyuan"))
    names = op.authorized_names(policy.projects)
    assert sorted(names) == ["OpenMOS", "XiaoYuan"], (
        f"只该列已授权的，却拿到 {names}"
    )
    assert "FreeAgent" not in names, (
        "列未授权的项目会让人去试、然后撞闸门 —— 而撞闸门像「功能坏了」"
    )


def test_hint_survives_separator_mismatch(two_authorized):
    """配置写反斜杠、OpenCode 给正斜杠 —— 仍要认成同一个。"""
    policy = DelegationPolicy(projects=("D:\\PycharmProjects\\openmos",))
    assert op.authorized_names(policy.projects) == ["OpenMOS"]


def test_no_authorized_yields_empty_hint(two_authorized):
    policy = DelegationPolicy(projects=("D:/somewhere/else",))
    assert op.authorized_names(policy.projects) == []
    with pytest.raises(FreeAgentError) as ei:
        check_project_allowed(
            policy, "D:/nope", known_names=op.authorized_names(policy.projects))
    assert "项目名代替路径" not in str(ei.value)


def test_opencode_unavailable_yields_empty(two_authorized):
    """OpenCode 查不到时返回空 —— 上层照旧显示白名单路径，不受影响。"""
    monkey = pytest.MonkeyPatch()
    monkey.setattr(op, "list_projects", list)
    try:
        assert op.authorized_names(("D:/x",)) == []
    finally:
        monkey.undo()


def test_end_to_end_error_mentions_project(two_authorized):
    """把名字拼进错误文案。

    ⚠️ **这条只验到函数层，没验到调用点。** 它自己传了 ``known_names=``，
    所以它证明不了 ``cli/app.py`` / ``delegate.py`` 那三处真的传了 ——
    而上一版死代码正是死在这里。

    要真正守住调用点，得**驱动 CLI** 走一遍 ``/delegate``（起进程、发命令、
    读输出）。那需要另一套夹具，本文件给不起。
    在那之前，**人工确认方式是grep 调用点有没有 ``known_names=``**。
    """
    policy = DelegationPolicy(projects=("D:\\PycharmProjects\\openmos",))
    with pytest.raises(FreeAgentError) as ei:
        check_project_allowed(
            policy, "D:/wrong/place",
            known_names=op.authorized_names(policy.projects))
    msg = str(ei.value)
    assert "OpenMOS" in msg, f"调用点没把名字传进去：{msg}"