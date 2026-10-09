"""OpenCode 四段选择的 Web **写**端点：记住四段选择、增删「日常可选模型」。

## 为什么与读侧是两个模块

读侧（:mod:`.endpoints_oc_selection`）是**纯观测**，可以随手做；
这里是**配置变更**，得单独一步且要能审计。照抄委派白名单那条规矩
（设计文档 12.7.1）。原先两者同处一个文件，说了「刻意拆成两条」而没拆。

**建委派事务那条路不在这里** —— :func:`~.endpoints_oc_dispatch.oc_dispatch`
落的是 ``tasks`` 表，不是选择配置。

## 为什么值要对着选项校验（**闸门，不是校验**）

载荷来自浏览器，而浏览器载荷用户可以随便改（改 JS、重发请求）。
不校验就等于「任何本地页面都能让机器人把活派到任意目录」——
那正好绕过项目白名单这唯一一道准入闸门。
"""

from __future__ import annotations

from typing import Any

from ..app import App
from ..domain import FreeAgentError
from ..services import oc_discovery as discovery
from ..services import oc_selection as sel
from .endpoints_feishu import state_home_for


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

    推理档位是**依附于模型**的（``:func:`variant_options`` 要拿模型去查它有哪
    些档）。第一版照 ``body`` 的键顺序逐段校验，于是同一次请求里
    ``{"model": "…fledge", "variant": "high"}`` 会失败：轮到 ``variant``
    时模型**还没落盘**，于是拿不到档位列表、``high`` 被判成不可选。

    症状是「选模型 + 选 High」——**最常见的那个组合**——根本提交不了，
    而用户看到的只是一句「这个variant不可选」，指向完全错误的方向。

    所以改成：先把四段归一到一个目标 :class:`Selection`，再**按
    project → agent → model → variant 的顺序**依次校验并写入。

    ## 为什么值要对着选项校验

    见模块 docstring：那是准入闸门，不是顺手做的校验。

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