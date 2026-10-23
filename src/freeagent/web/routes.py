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
    endpoints_delegate_browse,
    endpoints_delegate_live,
    endpoints_delegate_projects,
    endpoints_feishu,
    endpoints_feishu_config,
    endpoints_oc_selection,
    endpoints_nudge,
    endpoints_pending,
    endpoints_read,
    endpoints_model,
    endpoints_summary,
    serialize,
    serialize_api,
    endpoints_services,
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
        # 委派白名单候选（只读）。**授权动作另走 POST** —— 查是纯观测、
        # 可以随手做；授权是安全边界，得单独一步。见设计文档 12.7.1。
        "/api/delegate/projects": endpoints_delegate_projects.delegate_projects,
        # 可授权根的目录浏览（只读，12.7.1.1 约束 1–4）。**写入另走 POST**：
        # 授权动作是 allow_pick，登记在 routes_write，这里只列目录。
        "/api/delegate/browse": endpoints_delegate_browse.delegate_browse_list,
        # 委派实时面板的只读快照（12.8 / V1.35）。**纯只读**：每 2 秒调一次，
        # 任何副作用都会在几秒内变成事故（参见 routes 里 _reminders 那段
        # 「GET 有副作用」的旧账）。决定动作在 POST 侧，见 routes_write。
        "/api/delegate/live": endpoints_delegate_live.delegate_live,
        # 后台服务统一状态（只读）。启停动作在 POST 侧，见 routes_write。
        "/api/services": endpoints_services.services,
        # OpenCode 四段选项（只读）。选择与清单的**修改**在 POST 侧 ——
        # 理由同委派白名单：查是纯观测，改是配置变更。
        "/api/oc/options": endpoints_oc_selection.oc_options,
    }


def _reminders(app: App, _q: dict[str, list[str]]) -> dict[str, Any]:
    """``GET /api/reminders`` —— 唯一会写库的 GET，且**刻意**如此。

    它是提醒的**一个送达通道**：用户每次加载界面都被提醒一次，这本身
    就是「送达」（设计文档 9.2 把 Web 列为三条提醒渠道之一）。

    ## 为什么确认放在 payload 构造之后

    以前这个端点直接调消费型的 ``reminders.check()``，于是：

    * **GET 有副作用** —— 刷新、prefetch、探活、监控轮询都会吃掉提醒；
    * **并发双加载互相吞** —— 第一个请求消费掉，第二个拿到空；
    * **失败在构造之前就落库** —— handler 或序列化一抛，提醒永久丢失。

    现在分两步：先查（纯查询），构造出 payload，成功之后再落令牌。
    剩下的极限是「响应构造成功但客户端没渲染」—— HTTP 单向确认测不到
    这一段，这是协议本身的限制，不是这里能补的。

    ``server.py`` 把整个 handler 放在 ``app.lock`` 里，所以查与确认之间
    不会被另一个请求插进来。
    """
    digest = app.reminders.due()
    payload = serialize_api.reminder_payload(digest)
    app.reminders.acknowledge(digest)
    return payload


#: GET 路由表。加接口在这里加一行，不碰 handler。
READ_ROUTES: dict[str, Callable[..., Any]] = {
    "/api/health": endpoints_read.health,
    "/api/today": lambda app, q: serialize.today_payload(
        app.today.view(),
        endpoints_read.role_names(app),
        pending_count=app.pending.count(),
    ),
    "/api/all": lambda app, q: endpoints_read.all_tasks(
        app, (q.get("scope") or ["open"])[0]
    ),
    "/api/roles": endpoints_read.roles,
    "/api/reminders": _reminders,
    "/api/settings": endpoints_read.settings,
    "/api/chat": endpoints_read.chat_menu,
    "/api/vision": endpoints_read.vision_status,
    # 待处理收集器（11.18）与对外承诺（11.19）—— Web 视图补上，
    # 复用同一服务层（12.1.1）。
    "/api/pending": endpoints_pending.pending,
    "/api/promises": endpoints_pending.promises,
    "/api/contacts": endpoints_pending.contacts,
    # 单个关系对象的一页账（11.20.6，Web「关系」视图明细）。
    "/api/contact": endpoints_pending.contact_ledger,
    # 催办草稿（11.20 / V1.57）：候选列表 + 单条起草。**只读 + 拼文本**，
    # 不写表、不发（20.1.4），故都在 GET 侧。
    "/api/nudge/candidates": endpoints_nudge.nudge_candidates,
    "/api/nudge/draft": endpoints_nudge.nudge_draft,
    # 导航角标计数（V1.58）：只报数，不报内容，口径与对应端点一致。
    "/api/summary": endpoints_summary.summary,
    # 连续性个人模型（11.17 / V1.59）：节奏 / 关系 / 承诺 三面，只读聚合。
    "/api/model": endpoints_model.model,
    **feishu_routes(),
}
