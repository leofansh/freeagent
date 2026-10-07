"""OpenCode 四段选择的 Web 端点：读选项、改「日常可选模型」清单。

## 为什么单独一个模块

编程页签要的东西和飞书那四张卡**完全一样**，所以它必须走
:mod:`freeagent.services.oc_discovery` —— 那是两个前端共用的发现层。
如果 Web 自己查一遍 OpenCode，就会变成两套实现，而它们的症状是
「Web 上选得到、飞书里选不到」，且没人知道该信哪个。

## 只读 / 写**刻意拆成两条**

照抄委派白名单那条规矩（设计文档 12.7.1）：

- 读是纯观测，可以随手做（页面每次打开都要读）
- 写清单是**配置变更**，得单独一步，且要能审计

## 为什么不提供「连接提供商」

``/config/providers`` 只有 GET，而且实测**返回明文 API Key**
（2 个 provider 带 key）。让凭据流经一个**无鉴权**的 Web 界面，等于把
密钥摊在页面上。所以「配凭据」留在 OpenCode Desktop 里做 ——
FreeAgent 只管「日常用哪些模型」。

## 为什么清单放 FreeAgent 侧而不是推进 OpenCode

OpenCode 的 ``PATCH /config`` 技术上可行（实测端点存在）。但那样 FreeAgent
与 Desktop 就**共同拥有同一个配置文件**，两个进程写一个文件迟早互相覆盖。
而飞书只需要知道「日常能用哪些」，不需要知道 OpenCode 的配置长什么样。
"""

from __future__ import annotations

from typing import Any

from ..app import App
from ..domain import FreeAgentError
from ..services import oc_discovery as discovery
from ..services import oc_selection as sel
from .endpoints_feishu import state_home_for

__all__ = ["oc_options", "oc_selection_save", "oc_curation_save"]


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
        "curated": {
            "models": curated,
            # 空 = 未挑过 = 界面应显示「全部 85 个」而不是「一个都没配」。
            "configured": bool(curated),
            "available": _options_as_dict(every_model),
        },
        "discovery_ok": snap.projects is not None and snap.providers is not None,
    }


def _validate_stage(stage: Any, value: Any) -> tuple[str, str]:
    """校验并归一一个选择值。**空串是合法值**（=「不指定」）。

    ``variant`` 段的空串表示「用模型基线」，而其余段的空串表示「用默认」——
    所以这里不许把空串当非法，否则用户**清空**下拉框就会报错。
    """
    if not isinstance(stage, str) or not stage.strip():
        raise FreeAgentError("缺少 stage")
    stage = stage.strip()
    # 段名就是 Selection 的字段名 —— 写错一个就在 getattr 时炸，
    # 而 getattr 那个名字来自**用户载荷**，所以必须先过白名单。
    if stage not in ("project", "agent", "model", "variant"):
        raise FreeAgentError(
            f"不认识的段：{stage}（应为 project/agent/model/variant）")
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise FreeAgentError(f"{stage} 必须是字符串")
    return stage, value.strip()


def oc_selection_save(app: App, body: dict[str, Any]) -> dict[str, Any]:
    """``POST /api/oc/selection`` —— 记住四段选择。

    ## 必须**按依赖顺序**落，不能照载荷顺序

    推理档位是**依附于模型**的（``:func:`variant_options` 要拿模型去查它有哪些
    档）。第一版照 ``body`` 的键顺序逐段校验，于是同一次请求里
    ``{"model": "…fledge", "variant": "high"}`` 会失败：轮到 ``variant``
    时模型**还没落盘**，于是拿不到档位列表、``high`` 被判成不可选。

    症状是「选模型 + 选 High」——**最常见的那个组合**——根本提交不了，
    而用户看到的只是一句「这个variant不可选」，指向完全错误的方向。

    所以改成：先把四段归一到一个目标 :class:`Selection`，再**按
    project → agent → model → variant 的顺序**依次校验并写入。

    ## 为什么值要对着选项校验

    载荷来自浏览器，而浏览器载荷用户可以随便改（改 JS、重发请求）。
    不校验就等于「任何本地页面都能让机器人把活派到任意目录」——
    那正好绕过项目白名单这唯一一道准入闸门。

    而 ``variant`` 还要**额外交叉验一次**：它在 OpenCode 侧**不校验**
    （实测错档也返回 204），所以错档只会表现成「选了 High 却没生效」。
    """
    home = state_home_for(app)
    given = body.get("selection")
    if not isinstance(given, dict):
        raise FreeAgentError("缺少 selection")

    # 1) 先把这次要改的四段归一，落到**目标状态**上。
    #    clear_from 在每段赋值后清下游，于是「换了模型」会自动带走旧档位。
    target = sel.load_selection(home)
    incoming: dict[str, str] = {}
    for key, raw in given.items():
        stage, value = _validate_stage(key, raw)
        # **赋值必须在 if 外面**：空串是合法输入，意思是「清掉这一段」。
        # 把它也放进 ``if value:`` 里，症状就是「用户在下拉框里清空模型、
        # 点保存，旧模型还在」—— 界面上看不出任何错误。
        setattr(target, stage, value)
        target.clear_from(stage)
        if value:
            incoming[stage] = value

    # 2) 按依赖顺序校验。``variant`` 用的是 **target.model**（本次请求里的
    #    那个），不是盘上那个 —— 这正是第一版的 bug。
    snap = discovery.snapshot()
    for stage in ("project", "agent", "model", "variant"):
        value = incoming.get(stage, "")
        if not value:
            continue
        if stage == "variant":
            options = sel.variant_options(snap.providers or {}, target.model)
        else:
            options, _ = discovery.stage_options(stage, home=home)
        if value not in {o.value for o in options}:
            raise FreeAgentError(
                f"这个{stage}不可选：{value[:40]}。"
                "（可能已从清单里去掉，或 OpenCode 不再提供）")

    sel.save_selection(target, home)
    return {"ok": True, "selection": {
        "project": target.project, "agent": target.agent,
        "model": target.model, "variant": target.variant}}


def oc_curation_save(app: App, body: dict[str, Any]) -> dict[str, Any]:
    """``POST /api/oc/curation`` —— 增删一个「日常可选模型」。

    一次只动一个，因为界面上每个模型是一个开关；而批量替换（整份清单
    PUT）需要一个「这份清单从哪来」的权威来源 —— 现在没有，
    将来有了再说。**不做不存在的能力**比留一个空壳好。
    """
    home = state_home_for(app)
    action = body.get("action")
    model = body.get("model")
    if action not in ("add", "remove"):
        raise FreeAgentError("action 只能是 add 或 remove")
    if not isinstance(model, str):
        raise FreeAgentError("model 必须是字符串")

    snap = discovery.snapshot()
    available = {
        o.value for o in sel.list_model_options(
            snap.providers or {},
            provider_filter=sel.connected_providers(snap.providers or {}))
    }
    model = model.strip()
    if model not in available:
        raise FreeAgentError(
            f"这个模型现在不可用：{model[:40]}。"
            "（只有已连接凭据的 provider 下的模型才能进清单）")

    try:
        models = sel.curate(action, model, home)
    except ValueError as exc:
        raise FreeAgentError(str(exc)) from exc
    return {"ok": True, "models": models}