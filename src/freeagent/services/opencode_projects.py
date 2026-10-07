r"""从 OpenCode 的项目注册表**只读**查询项目清单（设计文档 12.7.1）。

## 为什么是「只读」且「在主进程」

委派执行器跑在配置隔离的实例里：``HOME`` / ``USERPROFILE`` / ``APPDATA`` /
``XDG_*`` 全被重定向进一次性jail（见 :mod:`services.opencode_server`的
``_JAILLED_ENV``）。而项目库正在**被重定向的那批目录**里
（``%USERPROFILE%\.local\share\opencode\opencode.db``），
所以执行器**读不到**它。

这不是缺陷，是隔离在正常工作。于是分工写死：

- **本模块（主进程）** 只读查询——只读，且在受信任的进程里
- **执行器** 只拿主进程递给它的绝对路径干活

## 为什么不缓存成第二份真源

项目名、图标、颜色都由OpenCode 拥有。抄一份就多一处会漂移的地方，
而漂移的表现是「两边名字对不上，且没人知道哪边对」。

数据源是官方只读 HTTP 端点 ``GET /project``（OpenAPI ``/doc`` 里定义）。
**不再直查 SQLite**：schema 是实现细节，改版会断，而 HTTP 是承诺过的契约；
而且「0 个项目」在 HTTP 里是**正常的空数组**，与「查询失败」分得开。

## 斜杠方向：两种都出现过

- ``opencode db``  → ``D:/PycharmProjects/openmos``（正斜杠）
- ``GET /project`` → ``D:\PycharmProjects\openmos``（反斜杠）

:func:`list_projects` 里统一成正斜杠，因为 :func:`authorized_names`
拿同一套归一化去比对 ``config.json`` 的白名单。
"""


from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

__all__ = ["OpenCodeProject", "list_projects", "resolve",
           "authorized_names", "reset_cache", "DEFAULT_PROJECT_WORKTREE"]


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


#: 查询结果缓存的有效期（秒）。
#:
#: **为什么必须缓存**：``GET /project`` 要起一个 opencode 进程，实测**5.7 秒**
#: 一次（起服务 + 等就绪 + 读项目库）。而这个函数在**每条飞书消息**的
#: 能力路由里都会被调到（:func:`~freeagent.cli.app.Repl._delegate_intent`
#: → ``caps.labels()`` → :func:`authorized_names`）—— 不缓存的话，发一句话
#: 要等 6 秒才有回音。
#:
#: 30 秒的取舍：够一整轮翻页与连续提问，又能在用户用 Desktop 新建项目后
#: **不用重启**就能看到。数据本身是「项目注册表」，变化以「天」计，
#: 所以 30 秒远小于它的变化尺度。
_CACHE_TTL = 30.0

#: ``(取回时刻, 结果)``。模块级而不是实例属性：没有实例，调用方是函数。
_CACHE: tuple[float, list[OpenCodeProject]] | None = None


def reset_cache() -> None:
    """清掉缓存。**只给测试与「我要立刻看到新项目」用。**"""
    global _CACHE
    _CACHE = None


def list_projects(*, runner=None) -> list[OpenCodeProject]:
    r"""列出可引用的项目。**任何异常都退化成空列表**，绝不掀翻调用方。

    ## 为什么不直接 parse `opencode db` 了

    原本走 `opencode db "SELECT worktree, name FROM project" --format json`。
    它有三个问题：

    1. 每次调用都要起一个子进程、等它初始化、再解析 stdout。
    2. 「空结果」和「查询失败」在子进程的世界里长得一样
       —— 都是 returncode=0 + 空 stdout，于是「正常的0个项目」被误判成错。
    3. 我们已经有一套现成的 HTTP 客户端
       (:class:`~freeagent.services.opencode_server.OpenCodeServer`)，
       又养一套子进程 + stdout 解析逻辑就是两份会漂移的实现。

    改走 ``GET /project`` 之后：
    - 空数组就是「正常的 0 个项目」，而不是错。
    - 服务没起来时抛 :exc:`ServerError`，调用方能看到真实原因。
    - 项目列表、配置、调用全走 ``OpenCodeServer`` 这条路径。

    ## 兼容说明

    保留 ``runner`` 参数是为了让测试能继续注入假子进程。
    生产中请忽略它，传 ``OpenCodeServer`` 实例。

    ## 结果缓存

    实测每次查询要起一个 opencode 进程（**5.7 秒**），而这条路径在**每条
    消息**上都会被走到，所以结果缓存 30 秒（见 :data:`_CACHE_TTL` 的理由）。
    传了 ``runner`` 时**不缓存** —— 那是测试路径，每次都该真跑一遍。
    """
    global _CACHE
    if runner is None and _CACHE is not None:
        age = time.monotonic() - _CACHE[0]
        if age < _CACHE_TTL:
            return list(_CACHE[1])

    if runner is not None:
        # 兼容旧的测试（注入假 runner）
        try:
            proc = runner(("opencode",) + _DB_ARGS)
        except (OSError, subprocess.SubprocessError):
            return []
        if getattr(proc, "returncode", 1) != 0:
            return []
        return _parse(getattr(proc, "stdout", "") or "")

    try:
        from .opencode_server import OpenCodeServer

        # ``discovery()`` 而不是直接构造：它**不隔离**，所以看得见真实的项目
        # 注册表。隔离实例看到的是一个空世界（``/project`` 返回空数组）——
        # 症状「明明有 3 个项目却一个都没有」会被误读成白名单没配。
        with OpenCodeServer.discovery() as server:
            raw = server.list_projects()
    except Exception:
        # 失败**不写缓存**：否则一次偶发的起不来会被记住 30 秒，
        # 症状是「刚才还好的，现在一直说没有项目」而查不出原因。
        return []

    projects: list[OpenCodeProject] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        worktree = str(row.get("worktree") or row.get("path") or "").strip()
        # **斜杠方向两种都出现过**（实测 2026-10-07）：
        #   - ``opencode db``      → ``D:/PycharmProjects/openmos``（正斜杠）
        #   - ``GET /project``     → ``D:\PycharmProjects\openmos``（反斜杠）
        # 而 ``authorized_names()`` 用同一套归一化比对白名单，所以**这里
        # 必须统一**，否则白名单里的正斜杠路径匹配不上 HTTP 返回的反斜杠 ——
        # 症状是「授权了 3 个项目，界面却说一个都没有」。
        worktree = worktree.replace("\\", "/")
        if not worktree or worktree == DEFAULT_PROJECT_WORKTREE:
            continue
        name = str(row.get("name") or "").strip()
        display = name or _basename(worktree)
        if not display:
            continue
        projects.append(
            OpenCodeProject(key=display.casefold(), display=display, worktree=worktree)
        )
    if runner is None:
        _CACHE = (time.monotonic(), list(projects))
    return projects


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