"""飞书桥接的启停端点（设计方案 12.7）。

**为什么单独一个文件**：:mod:`endpoints_feishu` 只读，这里会**起/杀进程**。
混在一起就得到一个既有查询又有进程的模块，而「这个端点会不会杀我的桥接」
必须是扫一眼就能回答的问题。

## 为什么要令牌

这组端点能**起一个带凭据的进程**，是整个 Web 面里后果最重的一类操作。
它们全是 POST，所以过 12.7 的会话令牌校验（``server._post`` 不查只读白名单，
见那里的说明）。令牌防的是「诱导浏览器替你 POST」（CSRF），而跨源带自定义
header 的请求会触发预检、而本服务不响应预检，所以网页里的恶意脚本够不着。

## 一条不做的事

**没有**「从界面改完配置自动重启」。重启会中断正在收消息的连接，这件事
不该由一次点错触发。界面上「需要重启」只是提示，重启是单独的按钮，
由用户明确按下去。
"""

from __future__ import annotations

from typing import Any

from ..app import App
from ..domain import FreeAgentError
from ..feishu.supervisor import SupervisorError, bridge_state, supervise
from .endpoints_feishu import state_home_for

__all__ = ["feishu_bridge", "feishu_bridge_action", "ACTIONS"]

#: 允许的动作。固定集合，不接受任意字符串转发给 supervisor。
ACTIONS = ("start", "stop", "restart")


def _payload(state: dict[str, Any], message: str) -> dict[str, Any]:
    out = dict(state)
    out["kind"] = "feishu_bridge"
    out["actions"] = list(ACTIONS)
    out["message"] = message
    return out


def feishu_bridge(app: App) -> dict[str, Any]:
    """桥接的启停现状。**只读**，且不含任何进程控制。"""
    state = bridge_state(state_home_for(app))
    return _payload(state, _describe(state))


def feishu_bridge_action(app: App, body: dict[str, Any]) -> dict[str, Any]:
    """执行 start / stop / restart。

    失败时抛 :class:`FreeAgentError`（→ 400），**把 supervisor 的话原样带给
    用户**：那些消息是特意写成人话的（「大概率已经有一个桥接在跑了」），
    在这里换成「操作失败」等于把有用的信息扔掉。
    """
    action = body.get("action")
    if action not in ACTIONS:
        raise FreeAgentError(
            f"action 只能是 {', '.join(ACTIONS)}，收到 {action!r}"
        )
    try:
        state = supervise(action, state_home_for(app))
    except SupervisorError as exc:
        # 附上现状：出错那一刻的状态往往才是用户想看的（比如「其实早就退了」）
        try:
            state = bridge_state(state_home_for(app))
        except Exception:               # noqa: BLE001 - 附不上也不该盖掉主因
            state = {}
        raise FreeAgentError(str(exc)) from exc
    return _payload(state, _describe(state, action))


def _describe(state: dict[str, Any], action: str | None = None) -> str:
    """一句话说清现状。**不给裸字段** —— ``supervised: false`` 谁也看不懂。"""
    if not state:
        return "读不到桥接状态。"
    if state.get("running"):
        base = "桥接在跑"
        if action == "start":
            return base + "，配置已生效（刚起的进程读的就是新配置）。"
        return base + "。"
    if state.get("supervised") and state.get("returncode") is not None:
        return (
            f"桥接已经退出了（退出码 {state['returncode']}）。"
            "原因在日志里。"
        )
    if state.get("port_held"):
        return (
            f"端口 {state.get('lock_port')} 被人占着，但没证据那是我们的桥接"
            "（多半是在别的窗口启的）。那个窗口关掉或 Ctrl+C 之后才能从"
            "这里启动。"
        )
    return "桥接没在跑。点「启动」就会用它。"
