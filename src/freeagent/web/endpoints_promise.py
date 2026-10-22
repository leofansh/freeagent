"""端点实现：承诺**结账**（11.19 / 11.20，Web「承诺」视图的操作）。

与只读端点分开：结账（keep / break / withdraw）是**写**操作 —— 兑现了、
破了、收回了，都要落库。白名单在 :mod:`actions`（``PROMISE_ACTIONS``），
这里只**转发到服务层**（``PromiseService.keep`` / ``break_`` / ``withdraw``），
不自算任何判定（12.1.1「一份能力一份实现」）。承诺不存在时服务层抛
``NotFoundError``，由 server 统一转 400。
"""

from __future__ import annotations

from ..app import App
from ..domain import FreeAgentError
from .actions import PROMISE_ACTIONS

__all__ = ["promise_action"]


def promise_action(app: App, promise_id: str, action: str) -> dict:
    """对一条承诺做结账动作。白名单在 :data:`PROMISE_ACTIONS`。"""
    if action not in PROMISE_ACTIONS:
        raise FreeAgentError(f"不认识的承诺动作：{action}")
    if action == "keep":
        app.promise.keep(promise_id)
    elif action == "break":
        app.promise.break_(promise_id)
    else:
        app.promise.withdraw(promise_id)
    return {"ok": True}
