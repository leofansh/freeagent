"""端点实现：待处理收集器与对外承诺（设计文档 11.18 / 11.19 / V1.51）。

为什么单独一个文件：``endpoints_read.py`` 已经顶到 250 行上限
（``TestWebPackageStaysSmall`` 守着），而这三个端点是**一组**东西 ——
收集器、承诺、关系对象，同属 11.18/11.19 这条线，放一起才读得懂。

这里同时有**读**（``pending`` / ``promises`` / ``contacts`` / ``contact_ledger``）
与**澄清这一处写**（``pending_clarify``，11.18：澄清是唯一建事务入口）。
澄清只把用户动作翻译成服务层调用，**不自猜标题、不自建事务** ——
判定全在服务层（:class:`~freeagent.services.pending.PendingService` 与
:class:`~freeagent.services.promise.PromiseService`），Web 端若自己查一遍
表，两处判定就会漂移（12.1.1「一份能力一份实现」）。
"""

from __future__ import annotations

from ..app import App
from ..domain import FreeAgentError, NotFoundError
from . import serialize, serialize_api

__all__ = ["pending", "promises", "contacts", "contact_ledger", "pending_clarify"]


def pending(app: App) -> dict:
    """待处理收集器（11.18）。

    **只摆事实，不猜内容**：类型、收下时间、大小、本地路径 ——
    「它是什么」留给用户。
    """
    return serialize.pending_payload(app.pending.pending())


def promises(app: App) -> dict:
    """对外承诺（11.19）。

    **只摆事实，不判违约**：逾期的只标 ``overdue``，不写「违约」——
    判违约是用户的事（11.19.5）。
    """
    names = {c.id: c.name for c in app.promise.contacts()}
    return serialize.promises_payload(
        app.promise.open_promises(), names, now=app.clock.now()
    )


def contacts(app: App) -> dict:
    """关系对象（11.19 / 20.1.5）。附「几条承诺未兑现」与「几笔在等他」。

    关系一等化的意义就在这两行数字上：它回答「我跟谁之间还有账」。
    """
    counts: dict[str, int] = {}
    for promise in app.promise.open_promises():
        if promise.contact_id:
            counts[promise.contact_id] = counts.get(promise.contact_id, 0) + 1
    waits: dict[str, int] = {}
    for task in app.tasks.list_open():
        waiting = task.waiting_on
        if waiting is not None and waiting.contact_id:
            waits[waiting.contact_id] = waits.get(waiting.contact_id, 0) + 1
    return serialize.contacts_payload(app.promise.contacts(), counts, waits)


def contact_ledger(app: App, q: dict[str, list[str]]) -> dict:
    """单个关系对象的「一页账」（11.20.6，Web「关系」视图的明细落点）。

    列表端点（``/api/contacts``）给「几条承诺 + 几笔在等」的计数，这里是
    逐条明细。**只按 id 或名字精确匹配，不因查账去创建** —— 与 CLI
    ``/contact`` 同一条纪律：为查账去建一个不存在的人，会把「我有没有这笔账」
    变成「我造了一笔账」。
    """
    raw = (q.get("id") or q.get("name") or [""])[0].strip()
    if not raw:
        raise FreeAgentError("要指定一个关系对象：/api/contact?id=xxx 或 ?name=xxx")
    contact = next(
        (c for c in app.promise.contacts() if c.id == raw or c.name == raw),
        None,
    )
    if contact is None:
        raise NotFoundError("关系对象", raw)
    return serialize_api.ledger_payload(
        app.promise.ledger(contact), now=app.clock.now()
    )


def pending_clarify(app: App, payload: dict) -> dict:
    """把一条捕获**澄清成事务**（11.18 / V1.56，Web 待处理视图的操作）。

    澄清是**唯一建事务的入口**且必须由用户显式触发 —— Web 只把这个动作
    翻译成服务层调用（``PendingService.clarify``），不自猜标题、不自建事务
    （12.1.1「一份能力一份实现」）。标题缺省时走服务层的**诚实**自动标题
    （文件用真名、图片用「图片（时间戳）」，不编内容）。重复澄清/已忽略
    的服务层抛 ``ValidationError``，由 server 统一转 400。
    """
    item_id = str(payload.get("id") or "").strip()
    if not item_id:
        raise FreeAgentError("要指定一条待处理项：{id: ...}")
    title = payload.get("title")
    if title is not None:
        title = str(title).strip() or None
    task = app.pending.clarify(item_id, title=title)
    return {"ok": True, "task_id": task.id, "title": task.title}
