"""单实体与「信封」类响应：对话、设置、角色下拉、提醒、事务详情。

从 :mod:`serialize` 拆出来是为了单文件别太长。两者的约束完全一致：
**只做转换，不含业务判断**；每条排序信号的理由必须出现在响应里。
"""

from __future__ import annotations

from ..domain import RestoreView
from ._fields import base_task as _base_task
from ._fields import iso as _iso

__all__ = [
    "task_payload",
    "reminder_payload",
    "settings_payload",
    "chat_payload",
]


def chat_payload(reply) -> dict:
    """对话回复的 JSON 形态。

    ``text`` 是给人读的，``items`` 是给界面渲染的 —— 结构化数据不塞进
    文本里，界面才能把每条事务做成可点的卡片。
    """
    return {
        "kind": reply.kind.value,
        "text": reply.text,
        "task_id": reply.task_id,
        "items": [
            {
                "task_id": item.task_id,
                "short_id": item.short_id,
                "title": item.title,
                "roles": list(item.roles),
                "kind_label": item.kind_label,
                "state_label": item.state_label,
                "total_weight": item.total_weight,
                "reasons": list(item.reasons),
                "detail": item.detail,
                "scheduled_for": item.scheduled_for,
            }
            for item in reply.items
        ],
        "suggestions": list(reply.suggestions),
    }


def settings_payload(config, llm_name: str) -> dict:
    """设置页要显示/编辑的**非秘密**配置。

    刻意包含三件容易被漏掉的事：

    * ``overridden_by_env`` —— 环境变量优先级高于配置文件。不告诉用户，
      他在界面上改了模型名却没效果，会以为设置坏了。
    * ``key_configured`` —— 只说「有没有」，绝不说「是什么」。
    * ``key_source`` —— 说清**从哪来的**（环境变量还是 ``llm.env``）。
      不说的话，用户在界面上重填了 Key 却发现没生效（被环境变量压住了），
      会以为设置坏了 —— 而真正要改的地方在启动脚本里。
    """
    from freeagent.config import config_path, overridden_by_env
    from freeagent.llm_env import mask_secret, resolve_key
    from freeagent.web.llm_settings import provider_choices

    key, key_source = resolve_key(config.key_env_var, home=config.home)
    profile = config.provider_profile
    return {
        "kind": "settings",
        "provider": config.provider,
        "provider_label": profile.label if profile is not None else f"（未知 {config.provider}）",
        "provider_needs_key": profile.needs_key if profile is not None else True,
        "provider_key_env_var": config.key_env_var,
        "providers": provider_choices(config),
        "model": config.model,
        "base_url": config.base_url,
        "timeout": config.timeout,
        "allow_fallback": config.allow_fallback,
        "llm_enabled": config.llm_enabled,
        "energy_windows": (
            None
            if config.energy_windows is None
            else {
                "morning": list(config.energy_windows.morning),
                "afternoon": list(config.energy_windows.afternoon),
                "evening": list(config.energy_windows.evening),
            }
        ),
        "key_configured": key is not None,
        "key_masked": mask_secret(key.use()) if key else "",
        "key_env_name": config.key_env_var,
        "key_source": key_source.label,
        "overridden_by_env": overridden_by_env(config),
        "config_path": str(config_path(config.home)),
        "llm_name": llm_name,
        "llm_enabled_note": (
            "已停用（FREEAGENT_RULES_ONLY=1），只能去启动环境里改"
        ),
    }


def reminder_payload(digest) -> dict:
    """到点提醒。

    与终端遵守同一条规则：**合并成一条摘要**，绝不逐条弹（设计文档 9.4）；
    超过 6 小时的如实标为「已错过」，不当作刚发生的新提醒（9.3）。
    """
    return {
        "kind": "reminders",
        "count": digest.count,
        "text": digest.text(),
        "fired": [
            {"task_id": e.task_id, "title": e.title, "due_at": _iso(e.due_at)}
            for e in digest.fired
        ],
        "missed": [
            {"task_id": e.task_id, "title": e.title, "due_at": _iso(e.due_at)}
            for e in digest.missed
        ],
    }


def task_payload(view: RestoreView, names: dict[str, str]) -> dict:
    """恢复契约的完整可视化。这是产品的核心卖点，字段一个都不能少。

    ``allowed_next_states`` 由**服务层的迁移表**算出来，UI 只照着渲染 ——
    这样「哪些按钮可点」永远和服务层的规则一致，不会双写漂移。
    """
    from ..services.tasks import ALLOWED_TRANSITIONS

    task = _base_task(view.task, names)
    artifact = view.current_artifact
    waiting = view.waiting_on
    # 合法迁入由服务层的迁移表决定；中文标签也一并给出，UI 不必硬编码
    allowed = [
        {"value": s.value, "label": s.label}
        for s in sorted(ALLOWED_TRANSITIONS[view.task.state], key=lambda s: s.value)
    ]
    return {
        "kind": "task",
        "task": task,
        "allowed_next_states": allowed,
        "effective_definition_of_done": view.effective_definition_of_done,
        "artifact": None
        if artifact is None
        else {
            "id": artifact.id,
            "version": artifact.version,
            "status": artifact.status.value,
            "title": artifact.title,
            "content": artifact.content,
            "created_at": _iso(artifact.created_at),
        },
        "progress_note": view.progress_note,
        "records": [
            {
                "ts": _iso(r.ts),
                "type": r.type.value,
                "content": r.content,
            }
            for r in view.recent_records
        ],
        "waiting_on": None
        if waiting is None
        else {
            "kind": waiting.kind.value,
            "who_or_what": waiting.who_or_what,
            "since": _iso(waiting.since),
            "follow_up_at": _iso(waiting.follow_up_at),
            "note": waiting.note,
        },
        "next_actions": list(view.next_actions),
    }
