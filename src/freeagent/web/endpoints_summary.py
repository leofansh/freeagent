"""端点实现：导航角标用的全局计数（设计文档 11.2 / V1.58）。

为什么单独一个文件：``endpoints_read.py`` 已顶到 250 行上限
（``TestWebPackageStaysSmall`` 守着），而角标计数是「一眼看全局」的聚合 ——
一个端点、一个关注点，放一起才读得懂。
"""

from __future__ import annotations

from ..app import App

__all__ = ["summary"]


def summary(app: App) -> dict:
    """导航角标用的计数（V1.58）。

    **只报数，不报内容**：今天几件、几条待处理、几笔该催、几条逾期承诺。
    角标是「不用点开就知道有没有事」，具体内容去对应视图看。口径与对应
    端点逐字一致（``today`` / ``pending`` / ``nudge`` / ``promises``），
    不自算判定（12.1.1「一份能力一份实现」）—— 这里只读已有的服务层聚合。
    """
    today = app.today.view()
    open_promises = app.promise.open_promises()
    overdue = sum(
        1
        for p in open_promises
        if p.promised_for is not None and p.promised_for <= app.clock.now()
    )
    return {
        "kind": "summary",
        "today": len(today.items),
        "pending": app.pending.count(),
        "nudge": app.nudge.count(),
        "promises_overdue": overdue,
    }
