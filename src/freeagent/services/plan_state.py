"""Plan 模式的会话状态 —— 落盘存储（设计文档 12.7.2）。

为什么必须落 SQLite 而不是只放内存
----------------------------------
``ChannelService._repls`` 是 :class:`~collections.OrderedDict` 做的 **LRU**，
到上限时 ``popitem(last=False)`` 丢最久没用的那个会话 —— 丢的是**整个
:class:`~freeagent.cli.app.Repl`**，于是 ``_plan`` 与 ``_mode`` 一并消失。

实测症状（不是报错）：
    A 进入 plan 说了两件事 → B 插进来挤掉 A → A 再说一句
    → 新 Repl 的 ``_plan`` 是 ``[]``、模式退回 ``build``

也就是说用户以为还在规划，实际内容已经没了，而且**没有任何提示**。
这比报错糟得多：报错他会知道要重来，静默丢失他只会以为系统记错了。

为什么独立成表，不进 ``tasks``
------------------------------
Plan 期是「**还没决定**要不要成为事务」的对话状态。混进 ``tasks`` 会让
``/today`` 把没批准的草稿也算成承诺 —— 而零副作用正是 Plan 模式要
杜绝的东西（``tests/test_cli_plan_mode.py::test_plan_mode_creates_nothing``）。

过期即拒绝
----------
读回来的 Plan 若超过 :data:`PLAN_TTL_SECONDS`，当作**不存在**。理由与
:mod:`freeagent.services.approval` 一致：一份放了很久的 Plan 很可能
对应的是上周那件事，此刻拿它去动手是危险的。「过期」不是「失败」，
所以这里不抛异常，只当没这条记录。
"""

from __future__ import annotations

import datetime
import json
import sqlite3

__all__ = ["PlanSession", "PlanStore", "PLAN_TTL_SECONDS"]

#: Plan 状态的存活时间。24 小时足够把一件事规划完，而「一周前的那件事」
#: 拿来执行的风险明显大于它的价值。
PLAN_TTL_SECONDS = 24 * 3600


class PlanSession:
    """一个会话的 Plan 状态快照。不可变。"""

    __slots__ = ("chat_id", "mode", "lines", "updated_at")

    def __init__(self, chat_id: str, mode: str, lines: tuple[str, ...],
                 updated_at: str) -> None:
        self.chat_id = chat_id
        self.mode = mode
        self.lines = lines
        self.updated_at = updated_at

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (f"PlanSession(chat_id={self.chat_id!r}, mode={self.mode!r}, "
                f"lines={len(self.lines)})")


class PlanStore:
    """Plan 状态的唯一出入口。

    刻意照 :class:`freeagent.services.approval.ApprovalStore` 的形状：
    同一个连接、同一套「写一个方法、读一个方法」的扁平面，不引仓储层。
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def save(self, chat_id: str, mode: str,
             lines: tuple[str, ...],
                 now: datetime.datetime | None = None) -> None:
        """写当前状态。**同 chat 覆盖**（一个会话只有一份 Plan）。"""
        stamp = (now or datetime.datetime.now()).isoformat(timespec="seconds")
        self._conn.execute(
            "INSERT INTO plan_sessions (chat_id, mode, lines, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET "
            "mode=excluded.mode, lines=excluded.lines, "
            "updated_at=excluded.updated_at",
            (chat_id, mode, json.dumps(list(lines), ensure_ascii=False), stamp),
        )
        self._conn.commit()

    def load(self, chat_id: str,
             now: datetime.datetime | None = None) -> PlanSession | None:
        """读回状态。**没有或已过期都返回 None**，不抛异常。

        刻意不在这里抛：读不到状态对用户来说是「没在规划」这件正常事，
        不是一个错误。而「过期当不存在」比「过期报错」安全 ——
        那份 Plan 描述的很可能是上周那件事。
        """
        row = self._conn.execute(
            "SELECT chat_id, mode, lines, updated_at FROM plan_sessions "
            "WHERE chat_id = ?", (chat_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            updated = datetime.datetime.fromisoformat(row["updated_at"])
        except (TypeError, ValueError):
            # 时间戳坏掉 = 这条记录不可信，当它不存在。
            return None
        now = now or datetime.datetime.now()
        if (now - updated).total_seconds() > PLAN_TTL_SECONDS:
            return None
        try:
            raw = json.loads(row["lines"])
        except (TypeError, ValueError):
            return None
        if not isinstance(raw, list):
            return None
        return PlanSession(row["chat_id"], row["mode"],
                           tuple(str(x) for x in raw), row["updated_at"])

    def drop(self, chat_id: str) -> None:
        """清掉这个会话的 Plan 状态（退出规划、或已执行完）。"""
        self._conn.execute(
            "DELETE FROM plan_sessions WHERE chat_id = ?", (chat_id,))
        self._conn.commit()