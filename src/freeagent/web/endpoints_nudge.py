"""端点实现：催办草稿（设计文档 11.20 / 20.1.4 / V1.57）。

为什么单独一个文件：与 ``endpoints_pending``（待处理/承诺/关系）同理 ——
``endpoints_read`` 已顶到 250 行上限（``TestWebPackageStaysSmall`` 守着），
催办是另一条线，放一起才读得懂。

**只读 + 拼文本，不写任何表**：催办草稿是「把库里有的事实铺成可编辑草稿」，
不建事务、不改关系状态、**系统没有发送能力**（``NudgeService`` 不持有任何
通道对象）。Web 只把用户显式动作翻译成服务层调用（12.1.1「一份能力一份
实现」）—— 不自算「什么算该催」，候选口径与两个信号逐字对齐。
"""

from __future__ import annotations

from ..app import App
from ..domain import FreeAgentError

__all__ = ["nudge_candidates", "nudge_draft"]


def nudge_candidates(app: App) -> dict:
    """当前**该催**的事务（11.20 / V1.57，Web「催办」视图的列表数据）。

    候选口径与信号逐字对齐（``OVERDUE_WAIT`` / ``PROMISE_OVERDUE``）——
    Web 不自算「什么算该催」，复用 ``NudgeService.targets``（12.1.1）。
    每条只摆事实（方向 / 对象 / 等了多久），不判违约（11.19.5）。
    """
    targets = app.nudge.targets()
    return {
        "kind": "nudge",
        "count": len(targets),
        "items": [
            {
                "task_id": t.task.id,
                "task_title": t.task.title,
                "direction": t.direction,
                "contact_name": t.contact_name,
                "elapsed": t.elapsed,
            }
            for t in targets
        ],
    }


def nudge_draft(app: App, q: dict[str, list[str]]) -> dict:
    """给一条候选**起草**（只显式触发 —— 系统不静默生成，11.20.2）。

    草稿是纯文本拼装、**不写任何表、系统不发**（20.1.4）。``task_id``
    必须显式给出；不在候选里的任务会被服务层拒绝并说明原因（``draft_for_task``
    刻意不做「随便哪条都能起草」—— 指错 id 时说出来比默默起草一条用不上的
    消息更省事）。
    """
    task_id = (q.get("task_id") or [""])[0].strip()
    if not task_id:
        raise FreeAgentError("要指定一条事务：/api/nudge/draft?task_id=xxx")
    return {"ok": True, "draft": app.nudge.draft_for_task(task_id)}
