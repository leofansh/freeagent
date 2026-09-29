"""GET 路由表。

为什么单独一个文件：``server.py`` 只该做「HTTP 机制」（收发、头、错误），
而「哪个路径对应哪个函数」是另一类关注点。两者混在一起时，``server.py``
会一路涨 —— 飞书通道接进来就顶破了 250 行上限（``TestWebPackageStaysSmall``
守着那条线）。

路由表**只放只读端点**。写端点在 ``do_POST`` 里按 parts 匹配，不走这里 ——
因为写操作要能被单独审一遍（见 :mod:`endpoints_write` 的模块说明）。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..app import App
from . import (
    endpoints_feishu,
    endpoints_feishu_config,
    endpoints_feishu_control,
    endpoints_read,
    serialize,
    serialize_api,
)

__all__ = ["READ_ROUTES", "feishu_routes"]


def feishu_routes() -> dict[str, Callable[..., Any]]:
    """飞书通道的路由（设计方案 12.7）。

    ``running`` 的判定在 :mod:`endpoints_feishu`：状态新鲜 + 自称连上 +
    **不处于降级**，三者缺一不可。进程被强杀后状态文件会留在盘上，照着它
    显示「已连接」是对着尸体报健康；而 ``degraded`` 时连接确实在、只是
    bot 身份没探到（群里 @ 它不回），算成「已连接」等于把最关键的问题
    藏起来 —— 那正是 12.7 要防的「分不清原因」。
    """
    return {
        "/api/feishu/status": endpoints_feishu.feishu_status,
        "/api/feishu/log": endpoints_feishu.feishu_log,
        "/api/feishu/config": endpoints_feishu_config.feishu_config,
        # 启停现状（只读）。动作在 POST 侧，见 routes_write。
        "/api/feishu/bridge": endpoints_feishu_control.feishu_bridge,
    }


#: GET 路由表。加接口在这里加一行，不碰 handler。
READ_ROUTES: dict[str, Callable[..., Any]] = {
    "/api/health": endpoints_read.health,
    "/api/today": lambda app, q: serialize.today_payload(
        app.today.view(), endpoints_read.role_names(app)
    ),
    "/api/all": lambda app, q: endpoints_read.all_tasks(
        app, (q.get("scope") or ["open"])[0]
    ),
    "/api/roles": endpoints_read.roles,
    "/api/reminders": lambda app, q: serialize_api.reminder_payload(
        app.reminders.check()
    ),
    "/api/settings": endpoints_read.settings,
    "/api/chat": endpoints_read.chat_menu,
    "/api/vision": endpoints_read.vision_status,
    **feishu_routes(),
}
