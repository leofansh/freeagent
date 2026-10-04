"""回归：``opencode`` 必须在**真机**上查得到，而不只解析得对。

## 为什么要这条测试

第一版 ``list_projects`` 全绿，但真机返回 **0 个项目**。根因：Windows 上
``where opencode`` 只返回无扩展名的 ``...\\npm\\opencode`` 和 ``opencode.cmd``，
而**Python 的 ``subprocess`` 传裸 ``"opencode"`` 不做 PATHEXT 解析** ——
于是 ``FileNotFoundError``，而它恰好被「任何异常退化成空列表」那条逻辑
**静默吞掉**。

所以关键不是「解析对不对」（那是 :mod:`test_opencode_projects` 管的，
用假 runner），而是**「那条查询能不能真的返回数据」**。这条测试
不 mock任何东西：它跑真的 ``opencode db``。

它会**在没装 opencode 的机器上跳过**——那时 ``list_projects`` 返回 ``[]``
是正确行为，硬断言 3 个会变成一条和现实脱节的假测试。
"""

import pytest

from freeagent.services.opencode_projects import _find_executable, list_projects

needs_opencode = pytest.mark.skipif(
    _find_executable() is None,
    reason="这台机器上没有 opencode，查不到是正确行为",
)


@needs_opencode
def test_can_really_query_opencode():
    """真跑一次 ``opencode db``，断言能拿到至少一条像样的项目记录。

    断言刻意很宽（只要有 worktree 像路径就行）：具体有几条、叫什么名字，
    是**用户的状态**，不该让测试随它变。
    """
    projects = list_projects()
    if not projects:
        pytest.fail(
            "查得到 opencode.exe 却返回 0 个项目 —— "
            "查询命令或输出格式变了，或退化逻辑把异常吞了"
        )
    for p in projects:
        assert p.worktree and p.worktree != "/", (
            f"Default Project 不该出现在结果里：{p!r}"
        )
        assert p.display, f"显示名不该为空：{p!r}"


@needs_opencode
def test_default_project_never_leaks_on_real_data():
    """真实数据里**一定**有一条 ``worktree='/'``，而它必须被滤掉。

    这条单独列出来，因为过滤错了的后果最严重：委派到 ``/`` 意味着一台
    机器上的「谁都行」。
    """
    projects = list_projects()
    assert all(p.worktree != "/" for p in projects), (
        "Default Project 漏进结果里了："
        f"{[p.worktree for p in projects]}"
    )