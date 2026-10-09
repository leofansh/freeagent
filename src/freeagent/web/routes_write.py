"""POST 路由表 —— :mod:`routes` 的写侧对应物。

为什么单独一个文件：和 :mod:`routes` 同理，``server.py`` 只该做 HTTP 机制
（收发、头、错误），「哪个路径对应哪个函数」是另一类关注点。

这里声明「哪个端点要哪个请求体上限」，**具体怎么读、读到哪算超**在
:mod:`body` —— 那是「读」的约束，路由只负责挑。两者分开是因为上限既是
路由的知识（拍照识物要 12 MB），也是读取的知识（超了怎么收尾）。

**POST 不做成 ``{路径: 函数}`` 字典**：写端点的签名不统一 ——
有的要 201，有的要空体，有的要求大上限。硬塞进字典就得给每个函数
塞一堆布尔开关参数，那比 if 链更难读。写端点数量少、每个都要单独审，
所以留成显式 if 链。

``read_body`` 由调用方（handler）传入：读请求体要碰 ``self.rfile`` 和
``self.headers``，那是 HTTP 机制，不该从这层伸手进来。
"""

from __future__ import annotations

from collections.abc import Callable
from http import HTTPStatus
from typing import Any

from ..app import App
from . import (
    endpoints_delegate_projects,
    endpoints_feishu_config,
    endpoints_feishu_control,
    endpoints_oc_dispatch,
    endpoints_oc_selection_write,
    endpoints_read,
    endpoints_write,
    llm_probe,
)
from .actions import is_allowed_action
from .body import MAX_IMAGE_BODY

__all__ = ["dispatch_post", "MAX_IMAGE_BODY"]

#: 读请求体的回调：``read_body(allow_empty=..., limit=...)`` -> dict
_ReadBody = Callable[..., "dict[str, Any]"]


def dispatch_post(
    app: App, parts: list[str], read_body: _ReadBody
) -> tuple[int, "dict[str, Any]"] | None:
    """把 POST 路径派到端点。``parts`` 是去掉空段后的路径片段。

    返回 ``(状态码, payload)``；路径不认识时返回 ``None``，由调用方回 404。
    """
    if parts == ["api", "task"]:
        return HTTPStatus.CREATED, endpoints_write.create_task(app, read_body())

    if parts == ["api", "settings"]:
        return HTTPStatus.OK, endpoints_read.save_settings(app, read_body())

    if parts == ["api", "llm", "test"]:
        # 走 POST 而不是 GET：它会**花掉一次真实请求**（计费），所以不能
        # 存在「顺手 GET 一下就触发」的路径上。
        return HTTPStatus.OK, llm_probe.llm_test(app, read_body())

    if parts == ["api", "llm", "models"]:
        return HTTPStatus.OK, llm_probe.llm_models(app, read_body())

    if parts == ["api", "chat"]:
        return HTTPStatus.OK, endpoints_read.chat(app, read_body())

    if parts == ["api", "vision"]:
        # 照片体积大，这里单独放宽；其他端点不受影响。
        return HTTPStatus.OK, endpoints_read.vision_identify(
            app, read_body(limit=MAX_IMAGE_BODY)
        )

    if parts == ["api", "feishu", "config"]:
        return HTTPStatus.OK, endpoints_feishu_config.feishu_config_save(
            app, read_body()
        )

    if parts == ["api", "delegate", "projects"]:
        # **授权**动作，只走 POST：能改权限的操作不该有「顺手 GET 一下就
        # 触发」的路径（浏览器预取、爬虫、`<img src>` 都会自动发 GET）。
        return HTTPStatus.OK, endpoints_delegate_projects.delegate_projects_save(
            app, read_body()
        )

    if parts == ["api", "feishu", "bridge"]:
        # 能起/杀带凭据的进程，所以**只能**走 POST（也只受令牌保护）。
        # 不额外注册 GET 侧的别名：一个能杀进程的操作不该有「顺手 GET 一下
        # 就能触发」的路径。
        return HTTPStatus.OK, endpoints_feishu_control.feishu_bridge_action(
            app, read_body()
        )

    if parts == ["api", "oc", "selection"]:
        # 改「四段选择」—— 决定**下一次**委派用哪个 agent/model/档位。
        # 只走 POST：它是配置变更，且值来自浏览器载荷（可伪造），必须在
        # 服务层对着真实选项校验，而不是「顺手 GET 一下就能改」。
        return HTTPStatus.OK, endpoints_oc_selection_write.oc_selection_save(
            app, read_body()
        )

    if parts == ["api", "oc", "curation"]:
        # 改「日常可选模型」清单。同样只走 POST，理由同上。
        return HTTPStatus.OK, endpoints_oc_selection_write.oc_curation_save(
            app, read_body()
        )

    if parts == ["api", "oc", "dispatch"]:
        # **建委派事务** —— 终点是本地代码执行，所以它必须走
        # Repl._create_delegation（白名单闸门在那儿），而不是网页自己拼一条
        # ``/delegate`` 文本命令。只走 POST。
        return HTTPStatus.CREATED, endpoints_oc_dispatch.oc_dispatch(
            app, read_body()
        )

    if len(parts) == 4 and parts[:2] == ["api", "task"]:
        action = parts[3]
        # 白名单在这里查，不在端点里：动作名是 URL 的一部分，
        # 漏过一个就是一条没人看过的写路径。
        if is_allowed_action(action):
            return HTTPStatus.OK, endpoints_write.mutate(
                app, parts[2], action, read_body(allow_empty=True)
            )

    return None
