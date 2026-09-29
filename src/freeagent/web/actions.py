"""界面能触发的动作白名单，以及「状态名 → 动作名」的翻译。

为什么这两样必须放一起
--------------------
白名单（``STATE_ACTIONS``）和状态→动作映射（``ACTION_FOR_STATE``）是**同一份
契约的两半**：映射的值如果不在白名单里，界面上就会多出一个点了必 404 的按钮。

真出过这个错：``dropped`` 被映射成 ``reopen``，而白名单里没有 ``reopen``，
于是「已放弃」按钮点了就 404。所以放一个模块里，并在
``tests/test_web.py`` 里断言「映射的值 ⊆ 白名单」。
"""

from __future__ import annotations

__all__ = [
    "STATE_ACTIONS",
    "SCHEDULE_ACTIONS",
    "ACTION_FOR_STATE",
    "with_actions",
    "is_allowed_action",
]

#: 界面允许触发的**状态迁移**。白名单 —— 别的动作一律 404。
#: 草稿采纳、角色合并、改名、静置、删除都**不在**其中，仍只能去终端。
STATE_ACTIONS = ("start", "done", "pause", "drop", "blocked")

#: 排期相关动作。单独列，因为它们不是状态迁移。
SCHEDULE_ACTIONS = ("pin", "unpin")

#: 目标状态 → 接口动作名。
#:
#: 「目标状态」是领域概念（``ALLOWED_TRANSITIONS`` 给的），「动作」是接口概念，
#: 两者名字不一样（状态 ``active`` 对应动作 ``start``）。必须由**接口层**翻译 ——
#: 前端若把状态名直接当动作 POST 出去，会得到 404（这个坑踩过两次）。
ACTION_FOR_STATE: dict[str, str] = {
    "active": "start",
    "inbox": "pause",
    "blocked": "blocked",
    "done": "done",
    "dropped": "drop",
}


def is_allowed_action(action: str) -> bool:
    return action in STATE_ACTIONS or action in SCHEDULE_ACTIONS


def with_actions(payload: dict) -> dict:
    """给任务 payload 附上「合法迁入 + 对应动作名」，供前端直接渲染按钮。"""
    states = payload.get("allowed_next_states") or []
    payload["allowed_next_states"] = [
        {**item, "action": ACTION_FOR_STATE.get(item.get("value"))}
        for item in states
    ]
    return payload
