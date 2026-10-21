"""委派实时交互的**写**端点（设计文档 12.8）：允许 / 拒绝 / 停止 / 答问，
以及陈旧委派的恢复动作（12.11.1）：重新派发 / 放弃。

## 全部走既有写入口

| 动作 | 落到哪个方法 |
|---|---|
| 允许 / 拒绝 | :meth:`ApprovalStore.resolve` |
| 停止这条委派 | :meth:`ApprovalStore.request_stop` **＋** ``resolve(deny)`` |
| 回答提问 | :meth:`ApprovalStore.put_answer` |
| 重新派发（陈旧行） | :meth:`TaskService.reset_delegation` |
| 放弃（陈旧行） | :meth:`TaskService.drop` |

**不许自己写 SQL。** 闸门全在那三个方法里：只有发起人能批、过期即拒、
已决定不可翻、提问一次一问、停止与回掉当前挂起必须**都做**。
另起一个写入口 = 第二套闸门，而它一定会在某次改动里与飞书那套漂移
（设计文档 12.1.1「一份能力一份实现」）。

## 「停止」为什么是两件事

只记停止而不回掉当前挂起 → 执行器会一直等到 TTL；只回掉而不记停止
→ agent 换个方向继续。用户点的是「停止这条委派」，两件事都该做。
这与飞书侧 ``bridge._card_action`` 的处理完全一致（同一个 store 方法）。

## 为什么每个端点都要自己判「可不可决」

:meth:`~freeagent.services.approval.ApprovalStore.resolve` 内部判据是
``if requester and decided_by`` —— **WEB 没有 ``decided_by``**，
于是它会**放行**任何一行。要让「只有发起人能批」在 WEB 上不被击穿，
这道判定就得由调用方做，而不能指望 store（见
:func:`~freeagent.web.endpoints_delegate_live.can_decide_in_web`）。
"""
from __future__ import annotations

from typing import Any

from ..app import App
from ..domain import FreeAgentError
from ..services.approval import ApprovalStore
from .endpoints_delegate_live import can_decide_in_web

__all__ = ["approval_action", "approval_stop", "question_answer",
           "delegate_redispatch", "delegate_drop"]


def _store(app: App) -> ApprovalStore:
    """待决读写。**用 ``app.clock.now``** —— 陈旧判定与过期必须同源。

    两个要点：
    * 传 ``app.clock.now``（**方法**）而不是 ``app.clock``：
      :class:`ApprovalStore` 的时钟约定是「返回 datetime 的可调用」
      （不传时默认 ``datetime.now``），而 ``App.clock`` 是 Clock **协议对象**。
    * 传别的时钟会造出「界面上还没过期、库里已经拒了」这种自相矛盾，
      而它的症状是用户点「允许」却被告知已过期，且没有任何线索指向原因。
    """
    return ApprovalStore(app.conn, clock=app.clock.now)


def _require_decidable(app: App, credential: str):
    """取这条待决项，并**在服务端**判它能不能由 WEB 决。

    三种情况给**三句不同的话**（照抄 :meth:`ApprovalStore.can_answer`
    的理由）：「不是发起人」「已决定过」「已过期」如果混成一句
    「没写进去」，用户看到的是「这个按钮坏了」。
    """
    store = _store(app)
    item = store.get(credential)
    if item is None:
        raise FreeAgentError("这条待批项已经不在了（可能已过期或已被答复）")
    if not can_decide_in_web(item):
        raise FreeAgentError(
            "这条由飞书发起，只有发起人能批 —— 去飞书点一下。"
        )
    if item.decision is not None:
        raise FreeAgentError(
            f"这条已经答复过了（{item.decision == 'allow' and '允许' or '拒绝'}）"
            "，不能再改 —— 一张用过的卡不能变成长期授权。"
        )
    if app.clock.now() >= item.expires_at:
        raise FreeAgentError("这条已经过期，沉默即拒绝，不能再批")
    return store, item


def approval_action(app: App, credential: str, body: dict[str, Any]) -> dict[str, Any]:
    """``POST /api/delegate/approvals/<credential>`` —— 允许或拒绝这一次动作。"""
    unknown = sorted(set(body) - {"decision"})
    if unknown:
        raise FreeAgentError(
            f"不认识这些字段：{', '.join(unknown)}。只支持 decision。"
        )
    decision = str(body.get("decision") or "")
    if decision not in ("allow", "deny"):
        raise FreeAgentError("decision 只能是 allow（允许）或 deny（拒绝）")

    store, _item = _require_decidable(app, credential)
    # decided_by 传空串：本机控制台**没有飞书身份**，而那条判据在
    # ``_require_decidable`` 里已经按 WEB 的口径查过一次了。
    # 这里再传一个假身份反而会让审计记录出现一个不存在的 ou_xxx。
    wrote = store.resolve(credential, decision)
    if not wrote:
        # 走到这里说明「查的时候还没决定、写的时候变了」——
        # 另一个界面（飞书）抢先一步。这种竞态不能当成功报回去。
        raise FreeAgentError("这次点击没生效 —— 可能刚刚在飞书那边答复过了")
    return {
        "ok": True,
        "credential": credential,
        "decision": decision,
        "message": "已允许这一步" if decision == "allow"
                   else "已拒绝这一步（agent 会换个方向继续）",
    }


def approval_stop(app: App, credential: str) -> dict[str, Any]:
    """``POST /api/delegate/approvals/<credential>/stop`` —— 停掉**整条**委派。

    与 :func:`approval_action` 的差别不是「范围更大」，而是**两件事都做**：
    回掉当前这次挂起（否则执行器空等到 TTL）＋ 记一条停止请求
    （否则 agent 只是换个方向继续，用户以为已经停住了）。
    """
    store, _item = _require_decidable(app, credential)
    store.request_stop(credential, by="")
    # 先记停止再回掉：反过来的话，中间那一瞬执行器看到「已拒绝」就继续往下走，
    # 而停止请求还没落库 —— 于是「点了停止，agent 还是动了一下」。
    store.resolve(credential, "deny")
    return {
        "ok": True,
        "credential": credential,
        "decision": "stop",
        "message": "已停掉这条委派。已完成的改动仍然有效，剩下的没做。",
    }


def question_answer(app: App, credential: str, body: dict[str, Any]) -> dict[str, Any]:
    """``POST /api/delegate/questions/<credential>`` —— 回答 agent 的提问。"""
    unknown = sorted(set(body) - {"text"})
    if unknown:
        raise FreeAgentError(f"不认识这些字段：{', '.join(unknown)}。只支持 text。")
    text = str(body.get("text") or "")
    if not text.strip():
        raise FreeAgentError("答复不能是空的 —— 那不是答案")

    store, item = _require_decidable(app, credential)
    slot = store.put_answer(credential, text.strip(), answered_by="")
    if slot is None:
        # :meth:`put_answer` 对五种原因都只回 None，而它们要给用户
        # 完全不同的话（空白 / 不是问题 / 已过期 / 全部答完 / 不是发起人）。
        # 上面已经挡掉空白与身份，这里再判一次「全部答完」——
        # 那是唯一还可能发生的（并发：另有一条提问被先答完了）。
        if item.is_complete:
            raise FreeAgentError("这些问题已经答完了 —— 不用再答")
        raise FreeAgentError("这条答复没记上 —— 可能已过期")
    # 题数取**写完之后重新读**的那一份，而不是入参的 item：
    # put_answer 会把答案写回 question_spec，而 slot 的下标是对着
    # 写后那份算的 —— 拿旧的一份算 total 会算错。
    fresh = store.get(credential)
    total = len(fresh.spec.get("questions") or []) if fresh is not None else slot + 1
    done = store.is_complete(credential)
    return {
        "ok": True,
        "credential": credential,
        "slot": slot,
        "total": total,
        "complete": done,
        "message": (
            f"收到了，这 {total} 个问题全部答完。opencode 继续干活。"
            if done else f"收到了（第 {slot + 1}/{total} 问）。"
            f"还有 {total - slot - 1} 个问题，直接在这里答。"
        ),
    }


def delegate_redispatch(app: App, task_id: str) -> dict[str, Any]:
    """``POST /api/delegate/tasks/<id>/redispatch`` —— 让一条卡住的委派重派。

    与终端 ``/redispatch`` 走**同一个写入口**（``TaskService.reset_delegation``）：
    三种「不许重派」的情形（正在跑 / 已做完 / 还没派过）各给一句准确的话，
    全在服务层，这里只把结果翻成界面话。理由见 12.1.1「一份能力一份实现」。

    ## 为什么这里**不**做 ``can_decide_in_web`` 那道判定

    审批那条判据治的是「用一个空身份批掉**飞书发起**的卡」（11.9.7 规则 2）。
    重派 / 放弃改的是**本机一条挂起委派自己的进度**，与「谁有飞书身份」
    无关；Web 只监听回环，操作者就是本机那个人。所以这类恢复动作
    **一律可决**，只在 ``reason`` 里记下「从网页面板点的」，与终端操作
    在记录里可对账。对审批沿用同一判据是**对的**，对恢复动作则会把
    Web 变成二等界面 —— 两件事不是同一条规则。
    """
    with app.lock:
        task = app.tasks.reset_delegation(task_id, "网页面板重新派发")
    return {
        "ok": True,
        "task_id": task_id,
        "action": "redispatch",
        "message": (
            f"已标记重派：{task.title}。执行器下次扫到就会再派一次"
            "（它是独立进程，不会立刻跑）。"
        ),
    }


def delegate_drop(app: App, task_id: str) -> dict[str, Any]:
    """``POST /api/delegate/tasks/<id>/drop`` —— 从面板放弃一条卡住的委派。

    走服务层 ``TaskService.drop``（与终端 ``/drop`` 同一条迁移与落账）。
    放弃是终态、可逆（``DROPPED`` → ``INBOX`` 合法）；破坏性由前端
    多加一次确认拦一道，服务端只做合法迁移，不重写规则。
    """
    with app.lock:
        task = app.tasks.drop(task_id, reason="网页面板放弃")
    return {
        "ok": True,
        "task_id": task_id,
        "action": "drop",
        "message": f"已放弃：{task.title}。记录仍保留、可审计。",
    }