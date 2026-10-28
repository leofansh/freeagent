"""检索留痕（3.7 / V1.98）：**用过什么**要能回答，不只「记过什么」。

## 治的是哪个缺口

在此之前，记忆与角色知识的检索、注入都**不留痕**。于是两个问题无法回答：

- 「哪条记忆 90 天没被用上」—— 读侧只能看到「记过什么」；
- 「本地什么时候真的不够」—— 没有留痕，就不知道检索里有多少次是
  tail 回落、多少次降级，而这是「该不该接在线检索」的唯一判据。

V1.96 的教训在这里同样成立：**没有计数的观点不可反驳**。
「你总在深夜完成」是观点，「近 90 天 37 次完成集中在 21-23 点」是事实。
留痕就是把「用过几次」从观点变成事实的那一步。

## 为什么日志是「只追加」且不自动清理

自动清理 = 静默删除，正是本仓反复避开的那类事（「只进不出」的解法是
给人看的 review 队列，不是悄悄扔）。量级也不值一提：一次检索一行日志
加至多几条命中，远小于任何一张业务表。真要清理，也该是一条明说
删了什么的命令。

## 写失败怎么办

:func:`record_retrieval` 返回 ``False`` 表示没写进去，**调用方不因此失败** ——
留痕是记账，不是检索本身；检索不可用才该降级（见
:meth:`freeagent.services.memory.MemoryService.retrieve` 的既有纪律）。
但失败不能无声无息：``/memory stats`` 报出的「留痕总数 / 最近一次时间」
就是它的自报面 —— 检索一直在用而留痕时间不动，就是在坏。
"""

from __future__ import annotations

import dataclasses
import sqlite3
from datetime import datetime
from typing import Any, Sequence

from .clock import Clock

__all__ = [
    "LOG_EVENTS",
    "LOG_LAYERS",
    "LOG_LEGS",
    "UsageTotals",
    "record_retrieval",
    "usage_totals",
]

#: 留痕事件。``retrieve`` = 搜过一次；``inject`` = 被拼进某个上下文一次。
#:
#: 写时校验（parse-don't-validate）而不是读时判断 —— 与
#: :data:`freeagent.services.memory.MEMORY_KINDS` 同一条理由：
#: 分类是用来聚合的，混进第三类会让「被检索几次 / 被注入几次」失去意义。
LOG_EVENTS: frozenset[str] = frozenset({"retrieve", "inject"})

#: 哪一层的检索。两层检索机制同源（``memory.py`` 与 ``knowledge.py``
#: 共用同一套 BM25 + tail），但条目不同源，聚合时必须能分开。
LOG_LAYERS: frozenset[str] = frozenset({"memory", "knowledge"})

#: 走了哪条腿。检索事件（``retrieve``）与注入事件（``inject``）共用一套取值，
#: 含义各有侧重：
#:
#: - ``bm25`` —— 凭相关度选中（检索：关键词命中；注入：V1.99 起按查询词选）；
#: - ``tail`` —— 相关度落空后回落近期（注入回落时取满预算，见
#:   :meth:`freeagent.services.memory.MemoryService.core_block`）；
#: - ``recency`` —— 注入类事件「没有相关度可依」时的取值：没传查询、
#:   查询切不出 token、或相关度不可用（后两种看 ``degraded`` 列分辨）；
#: - ``none`` —— 没有命中（含降级）。
#:
#: 注入事件的腿**不是装饰**：「按相关度选的」与「只是最近写的」价值不同，
#: 混起来就等于让三个月后的自己猜 —— 而猜不出来，因为日志里没有。
LOG_LEGS: frozenset[str] = frozenset({"bm25", "tail", "none", "recency"})


@dataclasses.dataclass(frozen=True, slots=True)
class UsageTotals:
    """一层检索在窗口内的总量。

    ``degraded_reason`` 有值表示**没读到**（表缺失 / 查询失败），此时三个
    计数都是 0。不能把「读不到」显示成「没有」—— 那正是留痕要防的那类
    静默（V1.98 的 CLI 测试守着这条）。
    """

    retrievals: int = 0
    injections: int = 0
    last_event_at: datetime | None = None
    degraded_reason: str = ""


def record_retrieval(
    conn: sqlite3.Connection,
    clock: Clock,
    *,
    event: str,
    layer: str,
    entry_ids: Sequence[str] = (),
    query: str = "",
    scope: str = "",
    legs: str = "none",
    consumer: str = "",
    degraded: bool = False,
    degraded_reason: str = "",
) -> bool:
    """记一条留痕（含命中的每条条目一行）。返回是否真的写进去了。

    用 **SAVEPOINT** 而不是裸 commit：日志行与命中行必须同生共死
    （只剩日志行会让 ``hit_count`` 说谎），而回滚又不能波及调用方
    可能挂着的事务。SAVEPOINT 正好把这两件事都做到 —— 它只包住
    本函数自己的几条语句。

    ``entry_ids`` 里重复的 id 会撞 ``(log_id, entry_id)`` 主键 ——
    那是**故意的**：同一次检索里同一条目出现两次本身就是异常，
    静默合并会让计数对不上。
    """
    if event not in LOG_EVENTS:
        raise ValueError(f"留痕事件必须是 {sorted(LOG_EVENTS)} 之一，收到 {event!r}")
    if layer not in LOG_LAYERS:
        raise ValueError(f"留痕层必须是 {sorted(LOG_LAYERS)} 之一，收到 {layer!r}")
    if legs not in LOG_LEGS:
        raise ValueError(f"留痕腿必须是 {sorted(LOG_LEGS)} 之一，收到 {legs!r}")

    from ..domain import new_id

    log_id = new_id()
    now = clock.now().isoformat()
    try:
        conn.execute("SAVEPOINT retrieval_log_write")
        conn.execute(
            "INSERT INTO retrieval_log"
            " (id, event, layer, query, scope, legs, hit_count, consumer,"
            "  degraded, degraded_reason, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                log_id,
                event,
                layer,
                query,
                scope,
                legs,
                len(entry_ids),
                consumer,
                1 if degraded else 0,
                degraded_reason,
                now,
            ),
        )
        for position, entry_id in enumerate(entry_ids):
            conn.execute(
                "INSERT INTO retrieval_hits (log_id, entry_id, position)"
                " VALUES (?, ?, ?)",
                (log_id, entry_id, position),
            )
        conn.execute("RELEASE retrieval_log_write")
    except sqlite3.Error:
        # 只回滚自己那几条：外面若还有别的事务，不能跟着一起赔进去。
        try:
            conn.execute("ROLLBACK TO retrieval_log_write")
            conn.execute("RELEASE retrieval_log_write")
        except sqlite3.Error:
            pass
        return False
    return True


def usage_totals(
    conn: sqlite3.Connection,
    *,
    layer: str,
    since: datetime | None = None,
) -> UsageTotals:
    """窗口内的 ``(检索次数, 注入次数, 最近一次时间)``。窗口外不计。

    读失败**不抛**：留痕是辅助能力，统计命令不该因为它整个崩掉
    （与检索降级是同一条纪律）。但失败必须自报 —— 见
    :class:`UsageTotals` 的 ``degraded_reason``。
    """
    if layer not in LOG_LAYERS:
        raise ValueError(f"留痕层必须是 {sorted(LOG_LAYERS)} 之一，收到 {layer!r}")
    where = "WHERE layer = ?"
    params: list[Any] = [layer]
    if since is not None:
        where += " AND created_at >= ?"
        params.append(since.isoformat())
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN event = 'retrieve' THEN 1 ELSE 0 END), 0),"
            "       COALESCE(SUM(CASE WHEN event = 'inject'  THEN 1 ELSE 0 END), 0),"
            "       MAX(created_at)"
            f"  FROM retrieval_log {where}",
            tuple(params),
        ).fetchone()
    except sqlite3.Error as exc:
        return UsageTotals(degraded_reason=f"留痕读不出来（{exc}）")
    last = row[2]
    return UsageTotals(
        retrievals=int(row[0]),
        injections=int(row[1]),
        last_event_at=datetime.fromisoformat(last) if last else None,
    )
