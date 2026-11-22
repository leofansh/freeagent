"""把人点的「允许 / 拒绝」送回引擎 —— 送不到就如实说，绝不假装成功。

为什么独立一份（而不是各调用点自己 try/except）
------------------------------------------------
委派执行器（``delegate.run_with_tool_gate``）与工作台轮次
（``AgentTurnService``）都做同一件事：拿到人对授权卡的决策，调引擎适配器的
``reply_permission`` / ``reply_question``。早先前者**完全不设防**，后者用
``contextlib.suppress(Exception)`` **吞掉异常** —— 于是卡面已经显示「已允许」，
引擎那边却还挂着一个没人回的请求，人看到的是「批了却没反应」，而会话一路
卡到 600s 超时。这是最坏的失效形态：**看起来在工作，其实什么都没发生**
（插件 v0.2.22 修的正是这类「审批放行被静默吞掉」）。

一份能力一份实现（12.1.1）：重试次数、间隔与「送不到算什么」在这里定死，
两个调用点只管各自怎么把那句话摆给人看（飞书进度卡 / 工作台轮次备注）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

__all__ = ["ReplyResult", "deliver", "DEFAULT_ATTEMPTS", "DEFAULT_DELAY"]

#: 瞬时抖动（连接抖一下、引擎正好在切会话）通常下一次就通。三次仍失败，
#: 多半是真的送不到（请求已过期 / 引擎已停），继续重试只是刷日志。
DEFAULT_ATTEMPTS = 3
#: 短间隔即可 —— 这是「送一个已经决定好的答复」，不是「等一个慢操作」。
DEFAULT_DELAY = 0.4


@dataclass(frozen=True)
class ReplyResult:
    """送达与否 + 失败原因（原因给调用点如实呈现，不在这里拼人话）。"""

    ok: bool
    error: str = ""


def deliver(
    send: Callable[[], Any],
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    delay: float = DEFAULT_DELAY,
    sleep: Callable[[float], Any] = time.sleep,
) -> ReplyResult:
    """把决策送回引擎，有限重试；送不到返回 ``ok=False`` 并带原因。

    ``send`` 是**零参 callable**，调用点把 rid / decision 绑好 —— 这样重试
    不需要知道引擎适配器的形状，``reply_permission`` 与 ``reply_question``
    共用同一份策略。

    ``sleep`` 可注入：测试里失败路径不能真睡（0.4s × N 条测试会让套件变慢，
    更重要的是「等待」本身不该是测试依赖）。成功路径一次就通，不睡。

    异常一律捕获：适配器的异常形状不统一（``ServerError`` / ``OSError`` /
    别的实现里的任意异常），而这一层的职责不是分类错误，是**不让任何一次
    失败被静默吞掉**。分类与下一步出路交给调用点按通道呈现。
    """
    last = ""
    for i in range(max(1, attempts)):
        try:
            send()
            return ReplyResult(True)
        except Exception as exc:  # noqa: BLE001 - 形状不统一，见上
            last = f"{type(exc).__name__}: {exc}"
            if i + 1 < attempts:
                sleep(delay)
    return ReplyResult(False, last)
