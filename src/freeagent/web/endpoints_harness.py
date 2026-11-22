"""端点：Harness 一页（V1.143 / 设计文档 21.7）。

为什么单独一个文件：``endpoints_read.py`` 已顶到 250 行上限
（``TestWebPackageStaysSmall`` 守着），而这一页是「四块已有事实的只读投影」
—— 一个端点、一个关注点，放一起才读得懂。

**不做的事**：不重新算任何计数、不解析 ``AGENTS.md`` 内容、不读偏好里的秘密。
四块全在 :mod:`freeagent.services.harness` 里聚合，web 层只做「取数 + 拼形状」。
"""

from __future__ import annotations

from typing import Any

from ..app import App
from ..services import harness
from .endpoints_agent_projects import load_prefs

__all__ = ["harness_page"]


def harness_page(app: App, _q: dict[str, list[str]] | None = None) -> dict[str, Any]:
    """``GET /api/harness`` —— 只读。

    ``mode``（授权档）从工作台偏好取：服务层不读 web 偏好（分层），
    这里取一次传进去。偏好读不出来时传空串 —— 界面上按默认档显示，
    而 ``notes`` 不因此多一条：偏好缺失本来就有默认值（``ENGINE_ASK``）。
    """
    mode = str(load_prefs(app).get("mode") or "")
    payload = harness.as_dict(harness.view(app, mode=mode))
    payload["kind"] = "harness"
    return payload
