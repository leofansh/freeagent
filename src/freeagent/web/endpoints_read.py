"""端点实现：从 ``App`` 到响应 payload 的纯函数（只读 + 设置 + 对话）。

写端点在 :mod:`endpoints_write`，路由与 HTTP 机制在 :mod:`server`。

为什么按「只读 / 写」切
--------------------
只读端点没有副作用，可以随便调；写端点会改数据，必须能被单独审一遍。
混在一个文件里时，「这个函数会不会改我的数据」要靠通读全文才看得出来。

**不做的事**：这里不重新发明任何业务规则。状态合法性、排期语义、
角色约束全部委托给服务层，web 层只做「取数 + 拼形状」。
"""

from __future__ import annotations

import io

from ..app import App
from ..domain import FreeAgentError
from ..services.sorting import score_and_sort
from .commands import command_payload, is_planning
from . import serialize, serialize_api
from .actions import with_actions

__all__ = [
    "health",
    "all_tasks",
    "roles",
    "task",
    "task_state",
    "resolve_task",
    "role_names",
    "settings",
    "save_settings",
    "chat_menu",
    "chat",
    "vision_status",
    "vision_identify",
    "with_actions",
]

MAX_CHAT_TEXT = 500
MAX_CONTEXT_IDS = 20


def role_names(app: App) -> dict[str, str]:
    return {
        r.id: r.name
        for r in app.roles.list_roles(include_inactive=True, include_merged=True)
    }


# -- 只读端点 ---------------------------------------------------------------


def health(app: App) -> dict:
    config = app.config
    return {
        "ok": True,
        "llm": app.llm_name,
        "key_configured": bool(config and config.api_key),
        "energy_windows": app.energy_windows is not None,
        "disclaimer": serialize.DISCLAIMER_MARKER,
    }


def all_tasks(app: App, scope: str) -> dict:
    if scope not in ("open", "closed", "all"):
        raise FreeAgentError("scope 只能是 open / closed / all")
    tasks = app.task_repo.list_all()
    if scope == "open":
        tasks = [t for t in tasks if t.is_open]
    elif scope == "closed":
        tasks = [t for t in tasks if not t.is_open]
    scored = score_and_sort(
        tasks,
        app.clock.now(),
        app.clock.today(),
        dependents_of=app.task_repo.count_dependents,
        energy_windows=app.energy_windows,
    )
    payload = serialize.all_payload(scored, role_names(app))
    payload["scope"] = scope
    return payload


def roles(app: App) -> dict:
    listed = app.roles.list_roles(include_merged=True)
    payload = serialize.roles_payload(listed, role_names(app))
    payload["options"] = serialize.roles_options(listed)
    return payload


def resolve_task(app: App, partial: str) -> str:
    """按 id 前缀找事务。找不到就报错 —— 不猜。"""
    if not partial:
        raise FreeAgentError("缺少事务 id")
    for task in app.task_repo.list_all():
        if task.id == partial or task.id.startswith(partial):
            return task.id
    raise FreeAgentError(f"找不到事务 {partial}")


def task(app: App, task_id: str) -> dict:
    view = app.restore.open_task(resolve_task(app, task_id))
    return with_actions(serialize_api.task_payload(view, role_names(app)))


def task_state(app: App, target) -> dict:
    """轻量结果：状态 + 排期 + 合法迁入，供前端局部刷新。"""
    payload = with_actions(
        serialize_api.task_payload(app.restore.build_view(target), role_names(app))
    )
    return {
        "kind": "ok",
        "task": payload["task"],
        "allowed_next_states": payload["allowed_next_states"],
    }


# -- 设置端点 ---------------------------------------------------------------


def _config(app: App):
    config = app.config
    if config is None:
        raise FreeAgentError("当前实例没有配置文件可改（通常是测试里注入了智能层）")
    return config


def settings(app: App) -> dict:
    return serialize_api.settings_payload(_config(app), app.llm_name)


def save_settings(app: App, body: dict) -> dict:
    """改配置：先校验、再落盘、最后热替换，**不用重启**。

    顺序是有意的：校验不过就不该写文件；写完才替换，
    避免「内存里生效了但磁盘没存」的半吊子状态。

    Key 走**同一条**纪律：先摘出来但**不写**，等非秘密配置校验通过了
    才落 ``llm.env``。反过来的话，配置没过而 Key 已改，就正好造出上面
    那个要避免的半吊子状态 —— 而且 Key 那一半用户根本看不到。
    """
    from ..config import config_from_settings, save_config
    from .llm_settings import split_key_fields

    base = _config(app)
    settings_body, pending_key = split_key_fields(body, base)
    new_config = config_from_settings(settings_body, base=base)   # 先校验
    if pending_key is not None:
        pending_key.commit()                                    # 过了才写
    save_config(new_config)
    app.apply_config(new_config)
    return settings(app)


# -- 对话端点 ---------------------------------------------------------------


def chat_menu(app: App) -> dict:
    """开场白 —— 告诉用户能问什么。

    猜谜语式的助手没人会用第二次，所以**开口就给例子**，
    界面上直接渲染成可点的按钮。
    """
    from ..services.chat import CAN_DO

    return {
        "kind": "menu",
        "can_do": list(CAN_DO),
        "notice": "我能读你已记的事，也能记新的；不改已有事务。",
    }


def chat(app: App, body: dict) -> dict:
    """真的问一句。**可能新建事务**（当这句话是在记事时）。

    ``last_items`` 是上一轮答复里的事务 id，**由客户端回传**。
    服务端不存对话状态：这样多轮能接上，同时服务端无状态、
    重启不丢、也便于测试。
    """
    text = str(body.get("text") or "").strip()
    if len(text) > MAX_CHAT_TEXT:
        raise FreeAgentError(f"太长了（上限 {MAX_CHAT_TEXT} 字）")
    raw_context = body.get("last_items") or []
    if not isinstance(raw_context, list):
        raise FreeAgentError("last_items 必须是数组")
    context = [str(x) for x in raw_context[:MAX_CONTEXT_IDS] if isinstance(x, str)]
    # 路由到 **Repl** 的两种情形（与飞书/终端同一个派发，
    # 见设计文档 12.7.2「通道的统一边界」）：
    #
    # 1) 文本以 ``/`` 开头 —— 命令本来就�� Repl 的职责，塞进 ChatService
    #    就是第二份命令表，而那份必然与 Repl 漂移。
    # 2) **这个会话正在规划中** —— 这条是我第一版漏掉的，写测试才发现：
    #    Plan 模式靠**自由文本**累积，而自由文本若照旧发给 ChatService，
    #    计划就永远是空的，于是 ``/mode-build`` 报「没有待执行的计划」。
    #    只分流命令在**原理上**就不够。
    #
    # 代价要说清：走 Repl 意味着这条回复是**纯文字**，没有 ``items`` ——
    # 那是 Web 结构化渲染的依据。所以只有命令与规划期才走这条路。
    if text.startswith("/") or is_planning(app):
        return command_payload(app, text)
    return serialize_api.chat_payload(app.chat.respond(text, context_ids=context))


# -- 拍照识物 ---------------------------------------------------------------


def vision_status(app: App) -> dict:
    """能不能拍照识别。没配 Key 就**如实说没有**，而不是返回个假的「已就绪」。"""
    from ..config import API_KEY_ENV

    return {
        "kind": "vision_status",
        "available": app.vision is not None,
        "provider": app.vision.name if app.vision is not None else None,
        "model": (app.config.vision_model if app.config else None),
        "key_env_name": API_KEY_ENV,
        "notice": (
            "能识别。图片只读进内存，用完即弃，不写进数据库也不落盘。"
            if app.vision is not None
            else f"没有 {API_KEY_ENV}，所以拍照识别用不了。"
                 "其余功能不受影响。"
        ),
    }


def vision_identify(app: App, body: dict) -> dict:
    """读一张图。**图片字节只在内存里，不落盘。**"""
    from ..services import vision as vision_svc

    data_url = str(body.get("image") or "")
    result = vision_svc.identify(app.vision, data_url)
    ident = result.identification
    return {
        "kind": "vision",
        "ok": ident.ok,
        "summary": ident.summary,
        "brand": ident.brand,
        "model": ident.model,
        "spec": ident.spec,
        "raw_text": ident.raw_text,
        "note": ident.note,
        "query": result.query,
        "links": [{"name": n, "url": u} for n, u in result.links],
        "task_seed": vision_svc.as_task_payload(ident) if ident.ok else None,
    }
