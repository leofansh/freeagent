"""Web 表面的**命令层**（设计文档 12.7.2「通道的统一边界」）。

## 为什么单独一个模块

不是「看着整齐」，而是 :mod:`endpoints_read` 有**行数上限**
（``tests/test_web.py::test_no_web_module_exceeds_limit``）。把命令层塞进去
会把它顶到 327 行 —— 那条守卫存在的意义正是逼这种职责分流。

## 这里只有派发，**没有业务规则**

真正的规则全在 :class:`~freeagent.cli.app.Repl` 里。这里只做三件事：
判断「该不该走命令层」、把输出抓出来、拼成 Web 界面已经认得的形状。
"""

from __future__ import annotations

import io

from ..app import App
from ..cli.app import MODE_BUILD, Repl, _ChannelCtx
from . import serialize_api

__all__ = ["WEB_CHAT_ID", "command_payload", "is_planning"]

#: Web 侧的「会话 id」。只用于 Plan 状态的落盘键（设计文档 12.7.2）。
#:
#: 为什么需要它：``Repl.restore_plan()`` 靠 ``channel_ctx.chat_id`` 找盘上的
#: Plan。没有它，``/mode-build`` 在浏览器里就永远读不回上一轮攒的计划。
#: 而它**不是**用户身份 —— Web 只监听回环、且有 CSRF 令牌（见
#: :mod:`freeagent.web.session`），所以拿一个固定串当键是安全的。
WEB_CHAT_ID = "web"


def _web_repl(app: App):
    """Web 表面**那一个** :class:`~freeagent.cli.app.Repl`，懒建并缓存。

    ## 为什么要一个**常驻**的，而不是每次请求新建

    ``Repl`` 持有两处状态：``_pending``（角色追问的半截输入）与 ``_last_items``
    （上一轮的事务 id）。每次新建就等于把多轮对话切断 —— 而 ``/mode-plan``
    攒计划、``/mode-build`` 确认，本来就是多轮的。

    这**确实**让 Web 对话变成了有状态的，与下面自然语言那条路不同。
    那条路刻意无状态（``last_items`` 由客户端回传），是有意的：重启不丢、
    易测。加命令层是拿这个换「命令能用」，我认为值 —— 但要如实记下来，
    而不是假装两边一样。
    """
    repl = app.web_repl
    if repl is None:
        repl = Repl(
            app,
            out=io.StringIO(),
            channel_ctx=_ChannelCtx(chat_id=WEB_CHAT_ID, sender_open_id=""),
        )
        # 建好就把上一轮攒的计划读回来 —— 浏览器关掉再开也算数。
        try:
            repl.restore_plan()
        except Exception:  # noqa: BLE001 - 读不回来就当没在规划
            pass
        app.web_repl = repl
    return repl


def command_payload(app: App, text: str) -> dict:
    """文本以 ``/`` 开头时走**命令层**，与飞书/终端**同一个**派发。

    ## 为什么不把 Web 整体改走 ChannelService

    那样会把自然语言那段的**结构化回复**退化成纯文字：Web 界面是靠
    :class:`~freeagent.services.chat.ChatReplyKind`
    （ANSWER/RECORDED/CLARIFY/CANNOT/HELP）决定怎么渲染的，还要拿
    结构化的 ``ChatItem`` 列表 —— 换成只有文字的 ``ChannelReply`` 就 degrade 了。

    所以这里**只**把命令分流出去，其余仍走 ``ChatService``。命令那条路复用
    **同一个** ``Repl._command``，12.1.1「一份能力一份实现」成立。

    ## 返回值仍然走 ``chat_payload``

    刻意**不新造返回结构**：构造一个 ``ChatReply`` 再交给同一个序列化，
    于是 Web 的渲染层完全不用改。
    """
    from ..services.chat import ChatReply, ChatReplyKind

    repl = _web_repl(app)
    buffer = io.StringIO()
    repl.out = buffer          # 每条消息重绑，否则第二次起就静默
    repl._last_choices = ()
    repl.handle(text, check_reminders=False)
    said = buffer.getvalue().strip()
    return serialize_api.chat_payload(ChatReply(
        kind=ChatReplyKind.ANSWER,
        text=said or "（这条命令没有输出。）",
        # 命令产生的按钮（只读视图选项、Plan 确认卡）**不在这条路上**：
        # 它们需要飞书那套卡片回调，而 Web 的渲染只认 items/suggestions。
        # 刻意留空而不是塞半份数据 —— 半份比没有更难处理。
    ))


def is_planning(app: App) -> bool:
    """这个 Web 会话**正在规划中**吗？

    单独一个函数，是为了让 :func:`freeagent.web.endpoints_read.chat` 不必知道
    「规划中」这件事该问谁。路由条件因此只有一句「命令 或 规划中」。
    """
    return _web_repl(app)._mode != MODE_BUILD
