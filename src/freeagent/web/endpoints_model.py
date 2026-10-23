"""端点实现：个人画像（设计文档 11.17 / V1.59）。

为什么单独一个文件：``endpoints_read.py`` 已顶到 250 行上限
（``TestWebPackageStaysSmall`` 守着），而画像是「个人模型」整面的只读投影 ——
一个端点、一个关注点，放一起才读得懂。

**不做的事**：这里不重新发明任何业务规则。逾期标 ``overdue``、违约交给用户，
关系账与承诺只摆事实 —— 全部委托给 :mod:`personal_model` 服务层，
web 层只做「取数 + 拼形状」（12.1.1「一份能力一份实现」）。
"""

from __future__ import annotations

from ..app import App
from . import serialize

__all__ = ["model"]


def model(app: App) -> dict:
    """``GET /api/model`` —— 个人画像（节奏 / 关系 / 承诺 三面），只读。

    复用 :class:`~freeagent.services.personal_model.PersonalModelService`，
    不新写任何聚合逻辑。与 /api/contacts、/api/promises 同一份「只摆事实」纪律
    （11.19.5：判违约是用户的事，系统只标「已逾期」）。
    """
    return serialize.model_payload(app.personal_model.view(), app.clock.now())
