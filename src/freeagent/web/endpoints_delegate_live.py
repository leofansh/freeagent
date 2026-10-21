"""``GET /api/delegate/live`` —— 委派实时面板的**只读**快照（设计文档 12.8）。

## 它回答一个问题

「此刻本机上有什么委派在动、有什么在等我批」。所以它只读两处：

* ``delegate_progress`` —— 执行器写的执行期投影（状态 / 最近操作 / 计数）
* ``pending_approvals`` —— 执行器写、等答复的待决项

两条都是**别的进程写的**，本端点一次读就返回，绝不在请求里等。

## 三条纪律（都是踩过坑才有的）

1. **纯只读，不许有副作用。** 全仓有一条前车之鉴：
   ``GET /api/reminders`` 曾经**消费**提醒，于是浏览器预取、探活、
   监控轮询都会把提醒吃掉（见 :mod:`.routes` 里 ``_reminders`` 那段）。
   本端点是每 2 秒调一次的高频端点，**任何**副作用都会在几秒内
   变成事故 —— 所以它不碰提醒、不消费待批项、不落库。
2. **不持长锁。** ``server.py`` 把整个 handler 放在 ``app.lock`` 里，
   本函数只在锁内读一次库（毫秒级）。若在这里睡 2 秒等变化，
   整个界面（含今天视图、对话、设置页）会一起卡住。
3. **可被关掉。** ``FREEAGENT_LIVE_POLL_SECONDS=0`` 时 ``poll_seconds``
   回 0，前端据此停掉定时器 —— 脚本化与低配机器上省掉这 2 秒一次。

## 「在跑」是**推出来的**，不是查出来的

库里没有「正在运行」这一列：``delegate_progress`` 有行只说明**最近写过一次**。
执行器被强杀时行会留在盘上。所以判「还在跑」只有一个依据 ——
``updated_at`` 与读方时钟的差**小于**陈旧阈值。

⚠️ 阈值（:data:`_STALE_AFTER_SECONDS`）**只在这里**算，别在存储层算：
执行器与 Web 是两个进程各有各的时钟，把「多少秒算旧」写进
``storage/repos.py`` 就等于让存储层知道有个 Web 在读它。
"""
from __future__ import annotations

import os
from typing import Any

from ..app import App
from ..services.approval import ApprovalStore, PendingApproval
from ..services.delegate import PROJECTION_STALE_SECONDS, projection_age_seconds

__all__ = ["delegate_live", "can_decide_in_web"]

#: 进度多久没更新就算「疑似卡住」（秒）。**定义在
#: :mod:`..services.delegate`**（``PROJECTION_STALE_SECONDS``）：执行器
#: 启动时的恢复扫描（12.11.1）必须与这里是**同一个**口径 —— 各写一份
#: 必然漂移，漂移的方向是「执行器认领了、面板还当它在跑」。上面那份
#: 模块说明解释了为什么取 60、以及为什么它不是「超时」判据。
#:
#: ⚠️ 阈值（``_STALE_AFTER_SECONDS``）**只在这里**算，别在存储层算：
#: 执行器与 Web 是两个进程各有各的时钟，把「多少秒算旧」写进
#: ``storage/repos.py`` 就等于让存储层知道有个 Web 在读它。
_STALE_AFTER_SECONDS = PROJECTION_STALE_SECONDS

#: 轮询间隔（秒）。默认 2 秒，与设计文档 12.8 的「~2s 轮询」一致。
#:
#: 环境变量刻意**留空即默认**而不是「填错就报错」：这个数字只影响
#: 多快看到更新，填错的后果是多几次库读，而拦住启动反而更糟。
_DEFAULT_POLL_SECONDS = 2.0


def _poll_seconds() -> float:
    raw = (os.environ.get("FREEAGENT_LIVE_POLL_SECONDS") or "").strip()
    try:
        value = float(raw) if raw else _DEFAULT_POLL_SECONDS
    except ValueError:
        return _DEFAULT_POLL_SECONDS
    # 下限 0.2s：比它更快的轮询没有任何意义（一次 SQLite 读就那么久），
    # 却会让面板在开着很多标签页时把 CPU 吃满。
    return max(0.0, min(value, 60.0))


def can_decide_in_web(item: PendingApproval) -> bool:
    """这个待决项**能不能**在 WEB 界面上决（设计文档 12.8「可决范围」）。

    判据只有一条：**待决行有没有带发起人身份**。

    * ``requested_by`` 为空 → WEB / 终端发起，没有飞书身份声明 →
      本机控制台即权威，**可决**。
    * ``requested_by`` 非空 → 飞书发起，带着 ``ou_…`` → **不可决**。

    ## 为什么不能「WEB 就是本机那个人，所以放行」

    看起来很有道理，实际上会把 11.9.7 规则 2（只有发起人能批）击穿：
    :meth:`ApprovalStore.resolve` 内部是
    ``if requester and decided_by: …`` —— ``decided_by`` 为空时**根本不查**。
    于是 WEB 用一个空身份就能批掉飞书发起的卡，而那条卡当初正是
    为了「只有那个人能批」才发的。

    ## 那 Web 用户就真的批不了自己发起的飞书委派？

    是的，**而这是正确的结果**：那条委派是在飞书那个上下文里推进的，
    审批的意义是「我确认这一步要动」。要换地方批，去飞书批。
    WEB 侧显示它、但不给按钮，并把原因写出来（见 :func:`_pending_payload`）。

    ⚠️ 附带一个副作用：飞书发起的提问也一样答不了。这与飞书侧
    「只有发起人能答」是同一口径（``ApprovalStore.put_answer``），
    所以不是 WEB 的缺陷，而是「只有发起人能交互」这条规则的忠实实现。
    """
    return not (item.requested_by or "").strip()


def _decision_hint(item: PendingApproval) -> str:
    """不可决时给用户的一句**去处**（12.7.2 R3：拒绝要给出路）。"""
    if item.is_question:
        return "这条提问由飞书发起，只有发起人能答 —— 去飞书回一句。"
    return "这条由飞书发起，只有发起人能批 —— 去飞书点一下。"


def _pending_payload(item: PendingApproval) -> dict[str, Any]:
    """一条待决项 → 界面载荷。

    ## 为什么 detail **原样**给前端

    ``detail`` 是 :meth:`ApprovalStore.request_tool_call` 精心拼的
    「动作 / 影响哪些路径 / **它想改成什么（diff）** / 授权多久」四件套。
    前端把它按 Markdown 原样渲染即可，**不许**在这里解析成结构化字段再拼 ——
    解析一遍就是第二套解析器，而 diff 一旦被截断或转义错，
    用户点的就是「信任」而不是「确认」。
    """
    decidable = can_decide_in_web(item)
    payload: dict[str, Any] = {
        "credential": item.credential,
        "kind": "question" if item.is_question else "approval",
        "subject": item.subject,
        "detail": item.detail or "",
        "asked_at": item.asked_at.isoformat(),
        "expires_at": item.expires_at.isoformat(),
        "task_id": item.task_id,
        "can_decide": decidable,
    }
    if not decidable:
        payload["blocked_reason"] = _decision_hint(item)
    if item.is_question:
        answers = item.spec.get("answers") or []
        next_q = None
        for q, a in zip(item.spec.get("questions") or [], answers):
            if not a:
                next_q = q
                break
        payload["question"] = next_q or ""
        payload["answered_count"] = sum(1 for a in answers if a)
        payload["question_count"] = len(item.spec.get("questions") or [])
    return payload


def delegate_live(app: App) -> dict[str, Any]:
    """``GET /api/delegate/live`` —— 在跑的委派 + 等我批的东西。

    刻意**不返回产物内容**（那是 ``/api/task/<id>`` 的事）：这个端点的
    载荷每 2 秒传一次，往里塞 diff 与草稿全文会把带宽吃光，
    而面板上真正要「立刻看见」的只有状态与待决项。
    """
    repo = app.task_repo
    # 注意传的是 ``app.clock.now``（**方法**）而不是 ``app.clock``：
    # :class:`ApprovalStore` 的时钟约定是「一个返回 datetime 的可调用」
    # （不传时默认 ``datetime.now``），而 ``App.clock`` 是 Clock **协议对象**。
    # 传错的话症状是 ``TypeError: 'FrozenClock' object is not callable`` ——
    # 测试里注入的冻结时钟直接把这个类型错暴露出来了。
    store = ApprovalStore(app.conn, clock=app.clock.now)

    now = app.clock.now()
    rows = repo.open_delegate_progress()
    pending = store.pending_for_web()

    # 待决项按 task_id 归堆；没有归属的（本地操作类）单独成组。
    by_task: dict[str, list[PendingApproval]] = {}
    for item in pending:
        by_task.setdefault(item.task_id or "", []).append(item)

    items: list[dict[str, Any]] = []
    for row in rows:
        task_id = str(row["task_id"])
        updated_at = str(row["updated_at"])
        # 陈旧判定用**读方**的时钟（见模块说明）。判定与恢复扫描共用
        # :func:`~services.delegate.projection_age_seconds` —— 解析失败
        # 返回无穷大，即「这条显示成卡住」，而那比「显示成正在跑」安全。
        age = projection_age_seconds(updated_at, now)
        stale = age >= _STALE_AFTER_SECONDS
        # 「事务已删但进度行还在」的坏行毒死整个面板（真机 2026-10-18：
        # get() 对不存在的事务**抛** NotFoundError，面板整体 400）。
        # find() 返回 None，兜底文案（"(事务已不存在)"）本来就在。
        task = repo.find(task_id)
        # 12.11.1（V1.39）：陈旧且**还没结束**的委派给两个恢复动作
        # 「重新派发 / 放弃」，前端据此画按钮。
        # * 非陈旧不给 —— 怕误触一条正在正常跑的东西；
        # * 已结束（DONE / DROPPED）不给 —— 重派会被服务层以
        #   「已经做完了」拒掉，放一个必然报错的按钮比不放更糟。
        actions: list[str] = []
        if stale and task is not None and task.state.is_open:
            actions = ["redispatch", "drop"]
        # 出路文案：指向下面的按钮（不再让人去终端敲命令 —— 那是
        # 「按钮只给文案」的不友好根源）。它**只改显示、不下结论**：
        # 是不是真没执行器在跑，只有人能确认。
        outlet = ""
        if stale:
            outlet = ("若这条一直不动，多半是执行器中断了 —— "
                      "可以在下面重新派发，或放弃。")
        items.append({
            "task_id": task_id,
            "title": task.title if task is not None else "(事务已不存在)",
            "project": row["project"],
            "status": row["status"],
            "recent": row["recent"],
            "tool_calls": row["tool_calls"],
            "allowed": row["allowed"],
            "updated_at": updated_at,
            "stale": stale,
            "age_seconds": None if age == float("inf") else int(age),
            "state": task.state.value if task is not None else "",
            "outlet": outlet,
            "actions": actions,
            "pending": [_pending_payload(i) for i in by_task.pop(task_id, [])],
        })

    # 没有进度行、却有待批项的：例如执行器正要发第一张卡就挂了，
    # 或者待批项是**别人**（飞书/终端）发起的、WEB 只是旁观。
    # 照样显示 —— 12.7.1 的「列出来 ≠ 已授权、但不藏」同一条道理。
    for task_id, group in by_task.items():
        task = repo.find(task_id) if task_id else None
        items.append({
            "task_id": task_id,
            "title": task.title if task is not None else "",
            "project": task.project_path if task is not None else "",
            "status": "等你决定",
            "recent": "",
            "tool_calls": 0,
            "allowed": 0,
            "updated_at": group[0].asked_at.isoformat(),
            "stale": False,
            "age_seconds": 0,
            "state": task.state.value if task is not None else "",
            "outlet": "",
            "actions": [],
            "pending": [_pending_payload(i) for i in group],
        })

    return {
        "kind": "delegate_live",
        "items": items,
        "poll_seconds": _poll_seconds(),
    }