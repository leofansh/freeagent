"""OpenCode 四段选择的 Web **读**端点。

## 为什么读与写是两个模块（2026-10 拆）

这个模块的 docstring 一直写着「只读 / 写**刻意拆成两条**」，而实际上两个
方向挤在同一个文件里——**说了没做**。写侧搬去了
:mod:`.endpoints_oc_selection_write`（改选择、改清单）与
:mod:`.endpoints_oc_dispatch`（建委派事务）。

分三条而不是两条，是因为第三件事**根本不是「改选择」**：
:func:`~.endpoints_oc_dispatch.oc_dispatch` 建的是一条**事务**，
落的是 ``tasks`` 表。把它塞进 ``oc_selection_write`` 会让模块名与内容
不符——而模块名不符的代价是后来的人以为改选择会连带建事务。

**为什么要拆**：``tests/test_web.py::TestWebPackageStaysSmall`` 守着
web 包每个文件 ≤ 250 行。那条守卫的意义正是逼这种职责分流，
所以「超了就拆」不是绕过它，是照它行事。

## 为什么这个页签与飞书共用一份选项

编程页签要的东西和飞书那四张卡**完全一样**，所以它必须走
:mod:`freeagent.services.oc_discovery` —— 那是两个前端共用的发现层。
如果 Web 自己查一遍 OpenCode，就会变成两套实现，而它们的症状是
「Web 上选得到、飞书里选不到」，且没人知道该信哪个。

## 读是**纯观测**，可以随手做

页面每次打开都要读，且一次请求给全——选项来自**同一个快照**
（起一次 opencode 进程约 7 秒，缓存 30 秒），而四个下拉框是**同时**
要渲染的。分四次请求只会让用户看四次转圈。

## 为什么清单放 FreeAgent 侧而不是推进 OpenCode

OpenCode 的 ``PATCH /config`` 技术上可行（实测端点存在）。但那样 FreeAgent
与 Desktop 就**共同拥有同一个配置文件**，两个进程写一个文件迟早互相覆盖。
而飞书只需要知道「日常能用哪些」，不需要知道 OpenCode 的配置长什么样。
"""

from __future__ import annotations

from typing import Any

from ..app import App
from ..services import oc_discovery as discovery
from ..services import oc_selection as sel
from .endpoints_feishu import state_home_for


def _options_as_dict(options: list[sel.Option]) -> list[dict[str, str]]:
    """Option → 界面能直接渲染的形状。

    刻意**只给三个字段**。:class:`Option` 将来加字段（比如「是否需要
    余额」）不该让前端自动开始显示它 —— 那是把内部表示变成对外契约。
    """
    return [{"value": o.value, "label": o.label, "hint": o.hint} for o in options]


def oc_options(app: App) -> dict[str, Any]:
    """``GET /api/oc/options`` —— 四段选项 + 当前选择 + 清单状态。

    一次请求给全，因为：选项来自**同一个快照**（起一次 opencode 进程约
    7 秒，缓存 30 秒），而四个下拉框是**同时**要渲染的。分四次请求只会让
    用户看四次转圈。
    """
    home = state_home_for(app)

    def stage(name: str) -> dict[str, Any]:
        options, note = discovery.stage_options(name, home=home)
        return {"options": _options_as_dict(options), "note": note}

    current = sel.load_selection(home)
    snap = discovery.snapshot()
    # 工作模式失效可见化（设计文档 11.13.8 / 11.14）：复用同一份 snapshot 里的
    # agents，不额外起 opencode 进程。取不到列表时不误报（discovery_ok 已单独提示）。
    agent_rows = snap.agents or []
    agent_warning = None
    if current.agent and agent_rows:
        if not sel.current_agent_is_valid(agent_rows, current.agent):
            agent_warning = (
                f"工作模式「{current.agent}」已失效（该项目可能没有它，"
                "或 OpenCode 已更新）。请在上方重选。"
            )
    curated = sel.load_curated_models(home)

    # 「全部已连接模型」= 把清单临时清空再查。这看着绕，但它是**唯一**
    # 能保证与飞书那份完全一致的办法 —— 复用同一份清单逻辑，而不是
    # 在这里再实现一遍「交集」。
    every_model = sel.list_model_options(
        snap.providers or {},
        provider_filter=sel.connected_providers(snap.providers or {}),
    )
    return {
        "project": stage("project"),
        "agent": stage("agent"),
        "model": stage("model"),
        "variant": stage("variant"),
        "selection": {
            "project": current.project,
            "agent": current.agent,
            "model": current.model,
            "variant": current.variant,
        },
        "agent_warning": agent_warning,
        "curated": {
            "models": curated,
            # 空 = 未挑过 = 界面应显示「全部 85 个」而不是「一个都没配」。
            "configured": bool(curated),
            "available": _options_as_dict(every_model),
        },
        "discovery_ok": snap.projects is not None and snap.providers is not None,
    }