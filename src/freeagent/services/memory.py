"""记忆（memory，设计文档 3.7）：跨角色的 **core 记忆层** + 自报来源的检索。

## 它与 ``knowledge.py`` 是什么关系

`knowledge.py` 是**按角色**的脉络知识（`role_knowledge`，外键到 roles）；
这一层是**跨角色的 core 记忆**（`memory` 表，作用域用 ``scope`` 文本表达）：
关于「你这个人」和「助手自己」的偏好 / 约定 / 修正。

两者**共用同一套检索机制与来源自报**（:class:`~freeagent.services.knowledge.RetrievalResult`、
``index_text`` 的 CJK bigram 分词、BM25 + tail 两条腿）—— 这不是复用代码的洁癖，
而是本仓「一份能力一份实现」的硬要求（12.1.1）：**第二个检索实现迟早与第一个分叉**，
而分叉后「为什么搜不到」会变成两个各说各话的答案。

## 为什么存储 / 可搜索 / 已加载是三种状态

设计文档 3.7.1 的第二条第一性原理。本模块只管前两者（存进去、搜出来）；
「加载进上下文」是消费方的事（注入点见 3.7.6）。把三种状态混起来，就会出现
「我明明记过它却装不知道」这种信任崩塌 —— 所以：

- :meth:`MemoryService.retrieve` 的返回值**自带来源**（每条怎么找到的、有没有截断、
  哪条腿没走、为什么）—— 空结果也给「搜过，没找到」。
- :meth:`MemoryService.list_all` 是真的列全部（用户整理时要看全貌，
  检索只在他**已经知道要找什么**时才有用）。

## 记忆不等于事实（注入纪律的根）

这一层存的是「当时这么认为」，不是「现在就是如此」。与当前真源（事务状态、
配置、代码）冲突时**以真源为准**。所以 :meth:`core_block` 拼出来的每一条都
**带口语化定性**，且整段以「可能过期」收尾 —— 消费方直接用它、不要自己再拼。
"""

from __future__ import annotations

import dataclasses
import sqlite3
from typing import Any, Literal

from ..domain import new_id
from ..storage.repos import RoleRepo
from .clock import Clock
from .knowledge import (
    DEFAULT_LIMIT,
    TAIL_LIMIT,
    KnowledgeHit,
    RetrievalResult,
    index_text,
)

__all__ = [
    "MemoryKind",
    "MemoryEntry",
    "MemoryService",
    "MemoryHit",
    "GLOBAL_SCOPE",
    "MEMORY_KINDS",
    "role_scope",
    "scope_label",
    "CORE_BUDGET",
]

#: 全局作用域的 ``scope`` 取值。
GLOBAL_SCOPE = "global"

#: 角色作用域的前缀。``role_scope(rid)`` -> ``'role:<rid>'``。
_ROLE_PREFIX = "role:"

MemoryKind = Literal["偏好", "约定", "修正"]

#: 允许的记忆类别。写时校验（parse-don't-validate）。
#:
#: 与 ``knowledge.KINDS``（偏好 / 口径 / 历史决策）**刻意不同**：
#: 那是「某个角色这件事的口径」，这是「关于我和助手自己的东西」。
#: 混用会让两边的检索都失去意义 —— 「口径」问的是工作事实，
#: 「约定」问的是你我之间怎么协作，它们回答的不是同一类问题。
MEMORY_KINDS: frozenset[str] = frozenset({"偏好", "约定", "修正"})

#: core 注入的预算：最多注入多少条。超预算**不静默截断**，见 :meth:`core_block`。
CORE_BUDGET = 12


def role_scope(role_id: str) -> str:
    """角色作用域的构造器。**唯一**拼 ``role:`` 前缀的地方。"""
    return f"{_ROLE_PREFIX}{role_id}"


def scope_label(scope: str) -> str:
    """给用户看的范围说明。``'role:<id>'`` 的 id 部分交给调用方补名字。"""
    if scope == GLOBAL_SCOPE:
        return "全局"
    if scope.startswith(_ROLE_PREFIX):
        return scope[len(_ROLE_PREFIX) :]
    return scope


@dataclasses.dataclass(frozen=True, slots=True)
class MemoryEntry:
    """一条记忆。``content`` 是原文，**唯一真源**。"""

    id: str
    scope: str
    kind: str
    content: str
    created_at: Any          # datetime
    updated_at: Any          # datetime
    source_ref: str | None = None

    @property
    def is_global(self) -> bool:
        return self.scope == GLOBAL_SCOPE


#: 命中的形状与 knowledge 的 :class:`KnowledgeHit` 同构 —— 复用同一套
#: ``via`` 语义（bm25 / tail）。单独别名是为了让类型名读起来是对的东西。
MemoryHit = KnowledgeHit


class MemoryService:
    """core 记忆的写入、检索、编辑与删除。"""

    def __init__(
        self,
        conn: sqlite3.Connection,
        roles: RoleRepo,
        clock: Clock,
    ) -> None:
        self._conn = conn
        self._roles = roles
        self._clock = clock

    # -- 写 ------------------------------------------------------------------ #
    def add(
        self,
        content: str,
        *,
        kind: str,
        scope: str = GLOBAL_SCOPE,
        source_ref: str | None = None,
    ) -> MemoryEntry:
        """记一条。``kind`` 只接受 :data:`MEMORY_KINDS`，非法直接抛。

        ``scope`` 为 ``role:<id>`` 时 **role 必须存在** —— 否则记下一条
        永远不会被检索到、也永远不会被看见的孤儿记忆（与 knowledge.add
        同一条理由）。这是 ``scope`` 用文本而非外键的代价，在这一处偿还。
        """
        if kind not in MEMORY_KINDS:
            raise ValueError(
                f"记忆类别必须是 {sorted(MEMORY_KINDS)} 之一，收到 {kind!r}"
            )
        stripped = content.strip()
        if not stripped:
            raise ValueError("记忆内容不能为空")
        if scope != GLOBAL_SCOPE:
            if not scope.startswith(_ROLE_PREFIX):
                raise ValueError(
                    f"记忆范围必须是 '{GLOBAL_SCOPE}' 或 'role:<角色id>'，收到 {scope!r}"
                )
            # 角色必须存在：孤儿记忆的问题同 knowledge.add。
            self._roles.get(scope[len(_ROLE_PREFIX) :])

        now = self._clock.now()
        entry = MemoryEntry(
            id=new_id(),
            scope=scope,
            kind=kind,
            content=stripped,
            created_at=now,
            updated_at=now,
            source_ref=source_ref,
        )
        self._conn.execute(
            "INSERT INTO memory"
            " (id, scope, kind, content, search_text, source_ref,"
            "  created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entry.id,
                entry.scope,
                entry.kind,
                entry.content,
                index_text(entry.content),
                entry.source_ref,
                entry.created_at.isoformat(),
                entry.updated_at.isoformat(),
            ),
        )
        return entry

    def edit(self, entry_id: str, content: str) -> MemoryEntry | None:
        """改内容。返回改后的条目；id 不存在返回 ``None``。

        ``kind`` 与 ``scope`` **不可改** —— 改类别/归属等于把这条记到别处，
        那应该是「删掉重记」，而不是一个悄悄换了身份的条目（与
        knowledge 层不提供 edit 的克制一致：不变的东西就别给改的口子）。
        """
        stripped = content.strip()
        if not stripped:
            raise ValueError("记忆内容不能为空")
        row = self._conn.execute(
            "SELECT id FROM memory WHERE id=?", (entry_id,)
        ).fetchone()
        if row is None:
            return None
        self._conn.execute(
            "UPDATE memory SET content=?, search_text=?, updated_at=?"
            " WHERE id=?",
            (
                stripped,
                index_text(stripped),
                self._clock.now().isoformat(),
                entry_id,
            ),
        )
        return self.get(entry_id)

    def delete(self, entry_id: str) -> bool:
        """删一条。返回是否真的删掉了。

        FTS 索引由**触发器**同步（见 ``storage/db.py`` 的 ``_MEMORY_SCHEMA``），
        所以这里不碰索引 —— 碰了就等于两处维护，然后某天漂移。
        """
        cur = self._conn.execute("DELETE FROM memory WHERE id=?", (entry_id,))
        return cur.rowcount > 0

    # -- 读 ------------------------------------------------------------------ #
    def get(self, entry_id: str) -> MemoryEntry | None:
        row = self._conn.execute(
            "SELECT id, scope, kind, content, source_ref, created_at, updated_at"
            " FROM memory WHERE id=?",
            (entry_id,),
        ).fetchone()
        return self._row_to_entry(row) if row is not None else None

    def list_all(
        self,
        *,
        scope: str | None = None,
        limit: int = 100,
    ) -> list[MemoryEntry]:
        """按时间倒序列出记忆（可限定作用域）。

        刻意提供「列出全部」而不只给检索 —— 与 knowledge.list_for_role 同一条：
        用户要整理自己记过什么时需要看全貌，检索只在他**已经知道要找什么**时才有用。
        """
        if scope is None:
            rows = self._conn.execute(
                "SELECT id, scope, kind, content, source_ref, created_at, updated_at"
                " FROM memory ORDER BY created_at DESC, id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT id, scope, kind, content, source_ref, created_at, updated_at"
                " FROM memory WHERE scope=?"
                " ORDER BY created_at DESC, id DESC LIMIT ?",
                (scope, int(limit)),
            ).fetchall()
        return [self._row_to_entry(r) for r in rows]

    def retrieve(
        self,
        query: str,
        *,
        scope: str | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> RetrievalResult:
        """检索记忆。**返回值自带来源说明。**

        两条腿（BM25 优先、落空回落近期）与 knowledge 层同一套实现，
        ``via='tail'`` 如实标出。任何一步出错都**降级而不是抛**（检索是辅助能力，
        不该让整个命令失败），但降级**必须自报** —— 否则「搜不到」会被误读成
        「没有这条记忆」。

        ``scope`` 只影响**候选集合**；召回时若要「全局 + 某角色」两面一起搜，
        调用方传 ``scope=None`` 并在结果里自行分辨（一期注入走 :meth:`core_block`，
        检索命令默认搜全部）。
        """
        limit = max(1, int(limit))
        try:
            return self._retrieve(query, scope, limit)
        except sqlite3.OperationalError as exc:
            return RetrievalResult(
                degraded=True,
                degraded_reason=f"检索不可用（{exc}）",
            )

    def _retrieve(
        self, query: str, scope: str | None, limit: int
    ) -> RetrievalResult:
        indexed = index_text(query)
        rows: list[tuple[Any, ...]] = []
        if indexed.strip():
            where = "WHERE memory_fts MATCH ?"
            params: list[Any] = [indexed]
            if scope is not None:
                where += " AND m.scope = ?"
                params.append(scope)
            params.append(limit + 1)
            rows = self._conn.execute(
                "SELECT m.id, m.scope, m.kind, m.content, m.source_ref,"
                "       m.created_at, m.updated_at, bm25(memory_fts) AS score"
                "  FROM memory_fts f"
                "  JOIN memory m ON m.rowid = f.rowid"
                f"  {where}"
                " ORDER BY score LIMIT ?",
                tuple(params),
            ).fetchall()

        hits: list[KnowledgeHit] = []
        truncated = False
        if rows:
            truncated = len(rows) > limit
            hits = [
                KnowledgeHit(
                    entry=self._row_to_entry(r),
                    via="bm25",
                    score=float(r[7]),
                )
                for r in rows[:limit]
            ]
        else:
            if scope is None:
                tail = self._conn.execute(
                    "SELECT id, scope, kind, content, source_ref, created_at, updated_at"
                    " FROM memory ORDER BY created_at DESC, id DESC LIMIT ?",
                    (TAIL_LIMIT,),
                ).fetchall()
            else:
                tail = self._conn.execute(
                    "SELECT id, scope, kind, content, source_ref, created_at, updated_at"
                    " FROM memory WHERE scope=?"
                    " ORDER BY created_at DESC, id DESC LIMIT ?",
                    (scope, TAIL_LIMIT),
                ).fetchall()
            hits = [
                KnowledgeHit(entry=self._row_to_entry(r), via="tail", score=None)
                for r in tail[:limit]
            ]

        return RetrievalResult(
            hits=tuple(hits),
            truncated=truncated,
            bm25_hits=sum(1 for h in hits if h.via == "bm25"),
            tail_hits=sum(1 for h in hits if h.via == "tail"),
        )

    # -- 注入 ------------------------------------------------------------------ #
    def core_block(
        self,
        role_ids: tuple[str, ...] = (),
        *,
        budget: int = CORE_BUDGET,
    ) -> tuple[str, bool, int]:
        """拼一段可注入的 core 记忆文本。

        返回 ``(text, truncated, extra)``：``text`` 为空串表示「还没记过」；
        ``truncated`` 表示有条目因超预算**没被注入**；``extra`` 是没注入的条数。

        取 ``scope='global'`` 加上 ``role_ids`` 里每个角色的记忆，按时间倒序，
        取最新 ``budget`` 条。**超预算不静默截断**：调用方拿 ``truncated``/``extra``
        去明说「还有 M 条未注入，用 /memory list 看全部」（设计文档 3.7.6）。

        每一条前面带**口语化定性**（「这是之前记下的」），整段末尾统一说明
        「可能过期、以当前为准」—— 记忆不等于事实，注入必须带这条定性，
        这是 3.7.1 的第三条第一性原理在代码里的落点。
        """
        scopes = [GLOBAL_SCOPE] + [role_scope(r) for r in role_ids]
        placeholders = ",".join("?" for _ in scopes)
        total = int(
            self._conn.execute(
                f"SELECT COUNT(*) FROM memory WHERE scope IN ({placeholders})",
                tuple(scopes),
            ).fetchone()[0]
        )
        rows = self._conn.execute(
            "SELECT id, scope, kind, content, source_ref, created_at, updated_at"
            f" FROM memory WHERE scope IN ({placeholders})"
            " ORDER BY created_at DESC, id DESC LIMIT ?",
            (*scopes, int(budget)),
        ).fetchall()
        shown = [self._row_to_entry(r) for r in rows]
        if not shown:
            return "", False, 0
        # 超预算**不静默**：真实总数来自单独的 COUNT，而不是「多取一行」——
        # 多取一行只能告诉我们「有没有超出」，报不出「超出多少」，
        # 而 3.7.6 要求明说「还有 M 条未注入」。
        extra = max(0, total - len(shown))
        lines = [
            f"- [{e.kind}] {e.content}" for e in shown
        ]
        body = "\n".join(lines)
        text = (
            "以下是你之前让我记住的东西（可能过期，与当前状态冲突时以当前为准）：\n"
            + body
        )
        return text, extra > 0, extra

    @staticmethod
    def _row_to_entry(row: Any) -> MemoryEntry:
        from datetime import datetime

        return MemoryEntry(
            id=row[0],
            scope=row[1],
            kind=row[2],
            content=row[3],
            source_ref=row[4],
            created_at=datetime.fromisoformat(row[5]),
            updated_at=datetime.fromisoformat(row[6]),
        )
