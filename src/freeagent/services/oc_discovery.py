"""OpenCode 的**发现层**：问它「有哪些项目 / 工作模式 / 模型 / 推理档」。

## 为什么从 :mod:`freeagent.feishu.bridge` 里搬出来

它原来住在飞书桥接里，因为**只有飞书用**。加了 Web 编程页签之后就成了
``web → feishu.bridge`` 的依赖 —— 一个 Web 端点去 import 一个飞书通道模块，
方向是倒的：那让「通道」变成了「服务层」，于是换掉飞书（比如只留 Web）
就得连服务层一起删。

搬出来之后依赖是单向的::

    feishu.bridge ─┐
                   ├─→ oc_discovery ─→ OpenCode HTTP
    web.endpoints ─┘

两个前端**共用同一份发现与过滤**，所以「在 Web 里验过的」就是「飞书在跑的」——
这一条是刻意的：否则「Web 是测试」就只是说说。

## 为什么不写进 :mod:`oc_selection`

``oc_selection`` 是**纯函数层**（过滤 + 状态），不碰 IO、便于断言。
这里是**IO 层**：起进程、等就绪、发请求、缓存。混在一起会让「过滤规则对不对」
这种判断也得先起一个 opencode 才能验 —— 而实测起一次要 6 秒。

## 缓存为什么是 30 秒

三份数据来自**同一个**进程、同一次启动。最初拆成三个各自带缓存的函数，
于是冷启动走一遍四段要 **20.6 秒**（6.6 + 6.9 + 7.1）；合成一个快照后
**7.1 秒** —— 剩下那 7 秒是 opencode 自己的冷启动，起不掉。

30 秒够走完一整轮翻页（点四次「下一页」），又能在用户中途用 OpenCode Desktop
登录了新 provider 之后**不用重启**就看到。

## 失败时为什么逐项记成败

一个 provider 登录失败不该让「有哪些项目」也一起消失。所以
``projects`` / ``agents`` / ``providers`` 各自 ``None`` 或有值，
``None``（查不到，该说环境有问题）与 ``[]``（真的没有）有别 ——
混成一个空列表，两种症状长得一模一样。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from . import oc_selection as sel

__all__ = [
    "Snapshot",
    "snapshot",
    "stage_options",
    "reset_cache",
    "exec_world_agents",
    "reset_exec_cache",
]

log = logging.getLogger(__name__)

#: 快照有效期（秒）。理由见模块 docstring。
_TTL = 30.0


@dataclass(slots=True)
class Snapshot:
    """一次发现的三份原始结果。字段为 ``None`` = 那一次查询失败。"""

    at: float
    projects: list[dict[str, Any]] | None = None
    agents: list[dict[str, Any]] | None = None
    providers: dict[str, Any] | None = None


_SNAPSHOT: Snapshot | None = None


def reset_cache() -> None:
    """清缓存。给测试与「我要立刻看到新项目/新模型」用。"""
    global _SNAPSHOT
    _SNAPSHOT = None


def _discovery_client() -> Any:
    """一个**只读发现**客户端。

    刻意用 :meth:`OpenCodeServer.discovery` 而不是自己写 HTTP：认证、错误
    处理、形状校验都在那个类里，这里复制的每一行都会漂移。

    关键是它 **不隔离**（``isolate=False``）：项目注册表与 provider 的登录
    状态都在你真实的配置目录里，而隔离实例把 ``HOME``/``XDG_*`` 全指向
    一次性目录，于是它看到的是一个**空世界** —— ``/project`` 空、
    ``connected`` 空。症状是「明明有 3 个项目、一个模型都没有」，而那会被
    误读成「白名单没配 / 没登录」，于是往错的方向查。

    只用于 GET。绝不用它发 prompt：那会让委派读到你的真实配置，而
    :mod:`services.delegate` 的全部安全声明都建立在隔离之上。
    """
    from .opencode_server import OpenCodeServer

    return OpenCodeServer.discovery()


def snapshot() -> Snapshot:
    """一次 discovery，三份数据。缓存 30 秒。"""
    global _SNAPSHOT
    now = time.monotonic()
    if _SNAPSHOT is not None and now - _SNAPSHOT.at < _TTL:
        return _SNAPSHOT

    snap = Snapshot(at=now)
    try:
        with _discovery_client() as oc:
            try:
                snap.projects = oc.list_projects()
            except Exception:
                log.exception("【发现】查项目失败")
            try:
                snap.agents = oc.list_agents()
            except Exception:
                log.exception("【发现】查工作模式失败")
            try:
                snap.providers = oc.list_models()
            except Exception:
                log.exception("【发现】查 provider 失败")
    except Exception:
        # 连服务都起不来：三份都没拿到。
        log.exception("【发现】起 opencode 服务失败")
    _SNAPSHOT = snap
    return snap


#: 「执行世界」快照有效期（秒）。
#:
#: 比 :data:`_TTL` 长得多，因为查它要**再起一次** opencode（隔离实例，
#: 约 7 秒），不能每次刷新页面都付一遍；而内置工作模式几乎不变。
_EXEC_TTL = 600.0

#: 项目路径 → ``(时刻, 工作模式列表或 None)``。**按项目**缓存：项目级
#: agent（``.opencode/agents/*.md``）在隔离实例里也看得见，所以列表与
#: 项目有关，不能全局共用一份。
_EXEC: dict[str, tuple[float, list[dict[str, Any]] | None]] = {}


def reset_exec_cache() -> None:
    """清「执行世界」缓存。给测试与「我刚装了新 opencode」用。"""
    _EXEC.clear()


def exec_world_agents(project: str = "") -> list[dict[str, Any]] | None:
    """**执行世界**（隔离实例）里有哪些工作模式。查不到返回 ``None``。

    ## 为什么不能直接用 :func:`snapshot` 那份

    两个世界看得见的东西不一样（设计文档 11.13.8 / 11.14，实测踩过两次）：

    - :func:`snapshot` 用**非隔离**实例，看得见登录态与云端工作模式
      （``Sisyphus - ultraworker``、``Atlas - Plan Executor`` 之类）；
    - 真正派发出去用的是**隔离**实例，那里只有内置的几个
      （实测：``build`` / ``plan`` 可用，其余是内部件或 subagent）。

    于是只拿 snapshot 判「有没有这个模式」会得出「选什么都行」的错觉 ——
    用户照着下拉选了云端模式，派发必然 ``工作模式「X」已失效``。**判
    「跑不跑得起来」必须问执行世界**（与执行期
    :func:`~.oc_selection.agent_is_executable` 同一条规则）。

    ## 为什么失败是 ``None`` 而不是 ``[]``

    ``[]`` = 真的一个都没有，``None`` = 没查到。用 ``[]`` 会把「起不来
    服务」误报成「你的 opencode 一个工作模式都没有」，于是界面把**所有**
    模式标成不可用 —— 那是一次环境抖动把功能全废掉。取不到就**降级不过滤**。
    """
    key = str(project or "")
    now = time.monotonic()
    hit = _EXEC.get(key)
    if hit is not None and now - hit[0] < _EXEC_TTL:
        return hit[1]
    try:
        import tempfile
        from pathlib import Path

        from .opencode_server import OpenCodeServer

        cwd = Path(key) if key else Path(tempfile.gettempdir())
        # 与派发**同一套条件**（系统 opencode + 项目级闸门覆盖层）：查到的
        # 列表才对得上派发时的校验，下拉才不会指向不存在的工作模式。
        with OpenCodeServer(project=cwd, executable="opencode",
                            isolate=False, apply_gate=True) as oc:
            rows = oc.list_agents()
    except Exception:  # noqa: BLE001 - 起不来就降级，不误杀选项
        log.exception("【执行世界】隔离实例查工作模式失败")
        rows = None
    _EXEC[key] = (now, rows)
    return rows


def stage_options(stage: str, *, home: Any = None) -> tuple[list[sel.Option], str]:
    """某一阶段的选项。**返回 ``(选项, 提示语)``**。

    失败时返回 ``([], 原因)`` —— 理由是**用户必须看到真实原因**，而不是一张
    空卡。实测踩过：过滤规则写错时症状是「选项一个都没有」，而那句话可以
    被解读成「没有可选的项目」，于是往错的方向查。

    :param home: 读当前选择时用的 ``~/.freeagent`` 位置。测试要隔离就传它。
    """
    snap = snapshot()

    if stage == "project":
        if snap.projects is None:
            return [], "查不到项目列表 —— OpenCode 没起来或读不到它的项目库。"
        return sel.list_project_options(snap.projects), ""

    if stage == "agent":
        if snap.agents is None:
            return [], "查不到 OpenCode 的工作模式列表 —— OpenCode 没起来或版本不认。"
        opts = sel.list_agent_options(snap.agents)
        return opts, "" if opts else "这台机器上没有可用的工作模式。"

    # model 与 variant 都要 ``/provider``（实测 **6.7MB**），而两者读的是同一份
    # 快照 —— 所以选完模型再选档位不再付那 7 秒。
    if stage == "model":
        if snap.providers is None:
            return [], "查不到模型列表 —— OpenCode 没起来或 /provider 不可用。"
        opts = sel.daily_model_options(snap.providers, home=home)
        if not opts:
            return [], (
                "日常清单是空的，且没有已连接凭据的 provider —— "
                "先在 OpenCode 里登录一个，或在设置里勾几个模型。"
            )
        return opts, sel.model_list_note(opts)

    if stage == "variant":
        current = sel.load_selection(home)
        if not current.model:
            return [], "还没选模型 —— 先选模型再选推理档。"
        if snap.providers is None:
            return [], "查不到推理档列表 —— OpenCode 的 /provider 不可用。"
        opts = sel.variant_options(snap.providers, current.model)
        if len(opts) <= 1:
            return opts, "这个模型没有可选推理档（用它的默认设置）。"
        return opts, "「不指定」= 用模型自己的默认强度。"

    return [], f"未知阶段：{stage}"