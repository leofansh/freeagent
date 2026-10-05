"""从 OpenCode 的项目注册表**只读**查询项目清单（设计文档 12.7.1）。

## 为什么是「只读」且「在主进程」

委派执行器跑在配置隔离的实例里：``HOME`` / ``USERPROFILE`` / ``APPDATA`` /
``XDG_*`` 全被重定向进一次性jail（见 :mod:`services.opencode_server`的
``_JAILLED_ENV``）。而项目库正在**被重定向的那批目录**里
（``%USERPROFILE%\\.local\\share\\opencode\\opencode.db``），
所以执行器**读不到**它。

这不是缺陷，是隔离在正常工作。于是分工写死：

- **本模块（主进程）** 只读查询——只读，且在受信任的进程里
- **执行器** 只拿主进程递给它的绝对路径干活

## 为什么不缓存成第二份真源

项目名、图标、颜色都由OpenCode 拥有。抄一份就多一处会漂移的地方，
而漂移的表现是「两边名字对不上，且没人知道哪边对」。

数据源是官方只读入口：``opencode db "<SQL>" --format json``。
不用起 HTTP server、不解析配置文件、不猜目录。
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

__all__ = ["OpenCodeProject", "list_projects", "resolve",
           "authorized_names", "DEFAULT_PROJECT_WORKTREE"]


class _Missing:
    """``opencode`` 没找到时的替身。

    刻意做成一个**对象**而不是抛异常：让「查不到原因」走到与其他失败
    相同的分支，于是 :func:`list_projects` 的退化逻辑只有一条路——
    第二条路迟早会漏测。
    """

    returncode = 127
    stdout = ""
    stderr = "opencode not found on PATH"

#: OpenCode 里那条 ``global`` 项目的 worktree。不是真实目录，必须过滤掉
#: ——委派到它没有意义，而且它恰好是 ``name`` 为空的那一条，
#: 不排除会被误当成「没命名的项目」。
DEFAULT_PROJECT_WORKTREE = "/"

#: 探测命令。**不给 ``shell=True``**，argv 是自己组装的固定列表。
_DB_ARGS = ("db", "SELECT worktree, name FROM project", "--format", "json")

#: 查询超时（秒）。不给它兜底，一次网络/磁盘卡住就能把界面的 HTTP 线程挂住。
_TIMEOUT = 10.0


@dataclass(frozen=True)
class OpenCodeProject:
    """一个可被引用的项目。

    :attr:`key` 是**引用键**（匹配用，大小写不敏感），
    :attr:`display` 是给人看的名字。
    """

    key: str
    display: str
    worktree: str

    @property
    def is_default(self) -> bool:
        return self.worktree == DEFAULT_PROJECT_WORKTREE


def _basename(worktree: str) -> str:
    """``worktree`` 的最后一段。

    刻意容忍两种写法：OpenCode 存的是正斜杠（``D:/PycharmProjects/freeagent``），
    Windows 上传进来的可能是反斜杠。``PurePath`` 在 POSIX 上不会把反斜杠当
    分隔符，所以**先统一**再取。
    """
    return worktree.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def _parse(payload: str) -> list[OpenCodeProject]:
    """把 ``opencode db --format json`` 的输出变成项目列表。

    每一行都**独立**判一遍，而不是「整体解析失败就全丢」：OpenCode 的项目表
    混着 ``global`` 那条，它的 ``name`` 是空的，是正常状态而非损坏。
    """
    try:
        rows = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(rows, list):
        return []

    out: list[OpenCodeProject] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        worktree = str(row.get("worktree") or "").strip()
        if not worktree or worktree == DEFAULT_PROJECT_WORKTREE:
            continue                      # Default Project：过滤掉
        name = str(row.get("name") or "").strip()
        base = _basename(worktree)
        # ``name`` 为空就退回目录名——实测三个项目里未命名的那些就是这么显示的。
        display = name or base
        if not display:
            continue
        out.append(OpenCodeProject(key=display.casefold(),
                                   display=display,
                                   worktree=worktree))
    return out


#: ``opencode`` 可执行文件名，按优先级尝试。
#:
#: **为什么需要这一列**（实测 2026-10-04，Windows）：``where opencode`` 只返回
#: ``...\\npm\\opencode``（无扩展名）与 ``opencode.cmd``，而 **Python 的
#: ``subprocess`` 传裸 ``"opencode"`` 时不按 PATHEXT 解析** —— 于是
#: ``FileNotFoundError``，尽管它在 cmd 里跑得好好的。
#:
#: 第一版只试了裸名，症状是「查询永远返回空列表」：异常被下面「任何异常都退化」
#: 的逻辑**静默吞掉**，于是测试全绿而真机是空的。**这正是断言落在错的通道上**
#: —— 当时那条测试用假 runner，验的是「解析逻辑」，不是「能不能真的查到」。
#:
#: ``.exe`` 在最前是因为原生可执行文件无需 shim；``.cmd`` 是 npm 在 Windows 上
#: 的实际形态。
_EXE_NAMES = ("opencode.exe", "opencode.cmd", "opencode.bat", "opencode")


def _find_executable() -> str | None:
    """在 PATH 里找可用的 ``opencode``。**自己拼名字，不靠 PATHEXT。**

    刻意**不用 ``shutil.which``**：它按 PATHEXT 解析，在部分 Windows 配置下
    返回无扩展名的 shim，而那个 shim **不能**直接 ``CreateProcess``。
    """
    path_env = os.environ.get("PATH") or ""
    for base in path_env.split(os.pathsep):
        if not base:
            continue
        for name in _EXE_NAMES:
            candidate = os.path.join(base, name)
            if os.path.isfile(candidate):
                return candidate
    return None


def list_projects(*, runner=None) -> list[OpenCodeProject]:
    """列出可引用的项目。**任何异常都退化成空列表**，绝不掀翻调用方。

    这是**纯观测**功能：OpenCode 没装、库不存在、查询超时、JSON 变了 ——
    每一种都只该让界面显示「查不到」，不该让界面 500。
    """
    if runner is None:
        exe = _find_executable()

        def runner(argv, **kw):
            # 找不到就不跑，交给调用方的 returncode!=0 分支处理 ——
            # 而不是抛出去再被吞掉（那样连「为什么查不到」都丢了）。
            if exe is None:
                return _Missing()
            return subprocess.run(          # noqa: S603 - 固定 argv
                (exe,) + argv[1:], capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=_TIMEOUT, shell=False, **kw)

    try:
        proc = runner(("opencode",) + _DB_ARGS)
    except (OSError, subprocess.SubprocessError):
        return []

    if getattr(proc, "returncode", 1) != 0:
        return []
    return _parse(getattr(proc, "stdout", "") or "")


def authorized_names(allowed_paths, *, projects=None) -> list[str]:
    """白名单里那些项目的**显示名**。

    ## 为什么只列「已授权」的

    提示里若列出未授权的项目，用户会去试、然后撞闸门——而撞闸门的感觉是
    「这功能坏了」，不是「你还没授权」。所以这里**先过滤再给名字**。

    查不到就返回空列表：那是「OpenCode 没装 / 查不到」，此时给不出任何
    可靠提示，让上层照旧显示白名单里的路径（那部分不依赖 OpenCode）。

    :param allowed_paths: 白名单里的路径（``DelegationPolicy.projects``）
    :param projects: 复用已查到的清单，避免同一个错误信息里查两次。
    """
    if projects is None:
        projects = list_projects()
    if not projects:
        return []
    wanted = {p.replace("\\", "/").rstrip("/").casefold() for p in allowed_paths}
    return [p.display for p in projects
            if p.worktree.replace("\\", "/").rstrip("/").casefold() in wanted]


def resolve(name: str, projects: list[OpenCodeProject]) -> OpenCodeProject | None:
    """按名字找项目，**大小写不敏感**。

    为什么必须不敏感（规范性，12.7.1）：实测三个项目的显示名与目录名
    **大小写全不同**（``FreeAgent``/``freeagent``、``OpenMOS``/``openmos``、
    ``XiaoYuan``/``xiaoyuan``）。严格匹配等于逼用户精确复刻 UI 上的大小写，
    而模型也可能改大小写。

    找不到返回 ``None`` —— **不回退**。回退成「按目录名猜」会让一个拼错的
    名字变成一次意外授权。
    """
    want = name.strip().casefold()
    if not want:
        return None
    for p in projects:
        if p.key == want:
            return p
    return None