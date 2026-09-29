"""端点实现的门面。

真正的实现在两个模块里：

* :mod:`endpoints_read` —— 只读 + 设置 + 对话（无副作用）
* :mod:`endpoints_write` —— 新建事务、状态迁移、排期（会改数据）

这个文件只做**再导出**，让 ``endpoints.mutate`` 这类引用不必改。
新代码请直接用上面两个模块 —— 名字能说明有没有副作用。
"""

from __future__ import annotations

from .actions import ACTION_FOR_STATE, with_actions
from .endpoints_read import (
    all_tasks,
    chat,
    chat_menu,
    health,
    resolve_task,
    role_names,
    roles,
    save_settings,
    settings,
    task,
    task_state,
)
from .endpoints_write import create_task, mutate

__all__ = [
    "ACTION_FOR_STATE",
    "with_actions",
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
    "create_task",
    "mutate",
]
