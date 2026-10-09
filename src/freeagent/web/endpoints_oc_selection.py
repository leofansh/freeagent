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


def _merged_agent_options(
    exec_rows: list[dict[str, Any]], discovery_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """工作模式选项：**以执行世界为准**，界面多出来的标成不可用。

    ## 为什么不能只用界面那份（discovery）

    两个坑，都是实测踩出来的（2026-10-09）：

    1. **同一个模式在两个世界里的 ``mode`` 都不一样。** ``build`` / ``plan``
       在隔离实例里是 ``primary``，在带配置的非隔离实例里却是 ``subagent``
       （那份配置把它们降级了）。而 :func:`~.oc_selection.list_agent_options`
       只收 primary —— 于是**唯一能派发的那两个被过滤掉**。
    2. 它们的 ``model`` 是 null，原来那条「model 非空」门槛又挡一道。

    两条相加的结果是：下拉里**一个能跑的都没有**，只剩云端模式
    （``Atlas - Plan Executor``、``Sisyphus - ultraworker`` 之类），而那些
    在隔离世界里不存在 —— 用户选哪个派发都报「工作模式已失效」。

    ## 所以这里怎么拼

    先用**执行世界**的清单出可选项（这些一定能跑），再把界面清单里多出来
    的（云端/项目级）附在后面、标 ``executable=False`` —— 仍然列出来是为了
    「它在界面里看得见」这件事对用户是真实信息，但**选了会失败**必须写明，
    由前端标注并禁选。
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for o in sel.list_agent_options(exec_rows):
        out.append({"value": o.value, "label": o.label, "hint": o.hint,
                    "executable": True})
        seen.add(o.value)
    for o in sel.list_agent_options(discovery_rows):
        if o.value not in seen:
            out.append({"value": o.value, "label": o.label, "hint": o.hint,
                        "executable": False})
            seen.add(o.value)
    return out


def oc_options(app: App) -> dict[str, Any]:
    """``GET /api/oc/options`` —— 四段选项 + 当前选择 + 清单状态。

    一次请求给全，因为：选项来自**同一个快照**（起一次 opencode 进程约
    7 秒，缓存 30 秒），而四个下拉框是**同时**要渲染的。分四次请求只会让
    用户看四次转圈。
    """
    home = state_home_for(app)

    current = sel.load_selection(home)
    snap = discovery.snapshot()
    agent_rows = snap.agents or []
    # 执行世界（隔离实例）的工作模式。**只查一次**，缓存 10 分钟。
    #
    # 它决定「选了之后派发跑不跑得起来」，所以工作模式那一段的选项必须
    # 以**它**为准生成 —— 见 :func:`_merged_agent_options`。
    exec_rows = discovery.exec_world_agents(current.project)

    def stage(name: str) -> dict[str, Any]:
        options, note = discovery.stage_options(name, home=home)
        payload = {"options": _options_as_dict(options), "note": note}
        if name == "agent":
            payload["exec_world_ok"] = exec_rows is not None
            if exec_rows is None:
                # 查不到执行世界 → 降级：**不标**、不禁选。「查不到」不等于
                # 「不可用」，误标会让一次环境抖动废掉全部选项。
                for item in payload["options"]:
                    item["executable"] = None
            else:
                payload["options"] = _merged_agent_options(exec_rows,
                                                           agent_rows)
        return payload
    agent_warning = None
    if current.agent:
        if exec_rows is not None and not sel.agent_is_executable(
            exec_rows, current.agent
        ):
            # 这条才是「派发必失败」的那种：模式在界面里看得见（有登录态），
            # 但派发用的隔离实例里没有它。
            agent_warning = (
                f"工作模式「{current.agent}」在隔离执行环境里不存在，"
                "派发会失败 —— 云端/项目级模式只在带登录态的界面里可见，"
                "而派发出去用的是隔离实例。请在上方改成 build 或 plan。"
            )
        elif agent_rows and not sel.current_agent_is_valid(agent_rows, current.agent):
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