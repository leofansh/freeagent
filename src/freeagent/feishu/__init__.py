"""飞书通道：把本地助手接到飞书/Lark 上。

分层刻意很硬：

* :mod:`~freeagent.services.channel` —— 消息路由，**零依赖**，
  与终端共用同一份命令语义。
* :mod:`events` —— 事件解析，纯函数，**零依赖**。
* :mod:`sender` —— 发消息，stdlib ``urllib``，**零依赖**。
  取 token 和发消息都只是 REST 调用，不需要 SDK。
* :mod:`bridge` —— **唯一**需要 ``lark-oapi`` 的地方：
  长连接握手、心跳、自动重连。这部分自己手搓不划算，
  所以它是可选依赖（``pip install ".[feishu]"``）。

这样切的结果：核心包依然 ``pip install`` 完就能跑，
不装飞书 SDK 也不会 import 失败。
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
