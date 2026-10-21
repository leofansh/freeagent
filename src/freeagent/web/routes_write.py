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
    endpoints_artifact,
    endpoints_delegate_action,
    endpoints_delegate_projects,
    endpoints_feishu_config,
    endpoints_oc_dispatch,
    endpoints_oc_selection_write,
    endpoints_read,
    endpoints_services,
    endpoints_system,
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

    # 委派实时交互（12.8 / V1.35）。三条都是「点一下就改授权状态」，
    # 全部 POST-only —— 与 delegate/projects 同一条纪律：能被预取的
    # 方法绝不能挂在改变安全状态的操作上。
    if (len(parts) == 4 and parts[:3] == ["api", "delegate", "approvals"]):
        return HTTPStatus.OK, endpoints_delegate_action.approval_action(
            app, parts[3], read_body()
        )

    if (len(parts) == 5 and parts[:3] == ["api", "delegate", "approvals"]
            and parts[4] == "stop"):
        # 「停止这条委派」是**两件事**：回掉当前挂起 + 记停止请求。
        # 缺任一件的症状不同但都像「点了没反应」，见端点说明。
        return HTTPStatus.OK, endpoints_delegate_action.approval_stop(
            app, parts[3]
        )

    if (len(parts) == 4 and parts[:3] == ["api", "delegate", "questions"]):
        return HTTPStatus.OK, endpoints_delegate_action.question_answer(
            app, parts[3], read_body()
        )

    # 陈旧委派的恢复动作（12.11.1 / V1.39）：重新派发 / 放弃。
    # 与上面三条同一条纪律：改状态的操作只走 POST，绝不挂 GET。
    # 放在 ``approvals`` / ``questions`` 之后、``tasks`` 命名空间下，
    # 与「待批项按 credential」的路径天然不相撞。
    if (len(parts) == 5 and parts[:3] == ["api", "delegate", "tasks"]
            and parts[4] in ("redispatch", "drop")):
        if parts[4] == "redispatch":
            return HTTPStatus.OK, endpoints_delegate_action.delegate_redispatch(
                app, parts[3]
            )
        return HTTPStatus.OK, endpoints_delegate_action.delegate_drop(
            app, parts[3]
        )

    if len(parts) == 4 and parts[:2] == ["api", "services"]:
        # 后台服务统一控制（启停重启）。POST-only 的理由同旧飞书控制：
        # 能杀进程的操作不能挂在 GET 上。动作白名单在端点里。
        return HTTPStatus.OK, endpoints_services.service_action(
            app, parts[2], parts[3]
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

    if parts == ["api", "system", "reset-data"]:
        # **恢复出厂**：整个 web 层唯一真正删数据的端点。口令在端点里
        # 校验（services/reset 只认 confirmed 布尔）。刻意放在
        # /api/system/ 命名空间下，不和事务写操作混在一起。
        return HTTPStatus.OK, endpoints_system.reset_data(app, read_body())

    if parts == ["api", "tasks", "drop"]:
        # **批量「算了」**。三段路径，与下面的四段单条路由
        # （/api/task/<id>/<action>）天然不会相撞。单独成端点而不是让
        # 前端连发 N 次单条：批量要有整体上限与逐条汇总
        # （见 endpoints_write.drop_tasks 的说明）。
        return HTTPStatus.OK, endpoints_write.drop_tasks(app, read_body())

    if parts == ["api", "tasks", "delete"]:
        # **批量硬删**（不可恢复）。与 /api/tasks/drop 对称：同样三段路径、
        # 同样整体上限 + 逐条汇总。POST-only 的理由见 drop —— 删数据的
        # 操作更不能挂在 GET 上。确认链是「前端两道 confirm → 端点只认
        # confirmed 真布尔」（见 endpoints_write.delete_tasks）。
        return HTTPStatus.OK, endpoints_write.delete_tasks(app, read_body())

    # 产物验收（2026-10-17）。刻意排在下面那个通用 `/api/task/<id>/<action>`
    # 分支**之前**：那里的动作名来自 ``STATE_ACTIONS``（状态迁移白名单），
    # 而验收改的是 artifacts.status、不迁入任何状态 —— 两者语义不同，
    # 混进同一个白名单会让「合法迁入」这个词不再成立。
    #
    # POST-only 的理由与 delegate/projects 同一条：改状态的按钮不能挂在
    # GET 上（浏览器预取、爬虫、`<img src>` 都会自动发 GET）。
    if len(parts) == 4 and parts[:2] == ["api", "task"] and parts[3] == "accept":
        return HTTPStatus.OK, endpoints_artifact.artifact_accept(app, parts[2])

    if len(parts) == 4 and parts[:2] == ["api", "task"] and parts[3] == "reopen":
        return HTTPStatus.OK, endpoints_artifact.artifact_reopen(app, parts[2])

    if len(parts) == 4 and parts[:2] == ["api", "task"]:
        action = parts[3]
        # 白名单在这里查，不在端点里：动作名是 URL 的一部分，
        # 漏过一个就是一条没人看过的写路径。
        if is_allowed_action(action):
            return HTTPStatus.OK, endpoints_write.mutate(
                app, parts[2], action, read_body(allow_empty=True)
            )

    return None
