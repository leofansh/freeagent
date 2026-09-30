"""角色知识：可检索的脉络知识 + **自报来源**的检索结果。

## 治的是哪个缺口

设计文档 13.2 与第十六章都记着：「角色级记忆只有 ``Role.note`` 自由文本，
**没有可检索的脉络知识**」。这一层就是那个缺口。

## 自报来源为什么是重点（而不是附带功能）

与本文档第七章的立场同构：排序信号必须附理由、必须标注「启发式提示，不是评分」。
**检索同理** —— 回答「你凭什么这么说」时只列结果不列来源，等于要求用户盲信。

所以 :class:`RetrievalResult` **不只返回命中项**，还返回：

- 每一项**怎么找到的**（``via``）
- 命中来源的分解（``bm25_hits`` / ``tail_hits`` / ``vector_hits``）
- **是否被截断**（``truncated``）—— 静默截断等于隐瞒
- 哪一条路**没走成**、为什么（``vector_unavailable_reason``）

## 「混合」实际是两条腿，不是三条

qlh 那套是 BM25 + 向量 + tail 三路。本项目只有两路：

- **BM25** —— SQLite FTS5，**零依赖**（见 :func:`index_text` 的分词说明）
- **tail** —— BM25 落空时回落到该角色最近若干条

**向量那一腿刻意没有**，且在返回值里**如实说明原因**，而不是留一个空的
``used_vector=False`` 让人以为「没搜到」。原因是实的：要 embedding 就得联网，
而本项目的硬性质之一是**断网照常能用**（README「无任何外部资源」）。
为了让检索质量上一个台阶而破坏这条，是坏的交易。

所以 :attr:`RetrievalResult.vector_unavailable_reason` 永远有值 ——
「本项目刻意不做向量检索」比「向量检索没命中」有用得多。

## 分词：为什么必须自己切

SQLite 的 FTS5 内置分词器对中文**不可用**，实测：

- ``unicode61``：整串中文当**一个 token** → ``MATCH '周报'`` **0 命中**
- ``trigram``：``销售周报``（3 字）能命中，``周报`` / ``王工``（2 字）**0 命中**
  —— 而中文查询大量是 2 字词

所以写入前把 CJK 切成**重叠二元组**（``交付销售周报`` → ``交付 付销 销售 售周 周报``），
查询用同样形态。实测 2 字词全部命中。

FTS5 里存的是 bigram、**不是原文**，所以原文另存一列（``content``），
FTS5 用 ``content='role_knowledge'`` 外部内容表模式，索引不会与真源漂移。

## 空格分隔 = AND（实测，与直觉相反）

FTS5 对空格分隔的词按 **AND** 处理：所有二元组都必须出现才命中。实测：

======================================  ==========  ==========================
查询                                    二元组数    结果
======================================  ==========  ==========================
``周报``                                1          命中所有含「周报」的
                                                    —— 命中范围**很宽**
``销售周报初稿``                        5          只有全含者命中 —— **很严**
``周报报告``                            2          0 命中（没有同时含两者的）
======================================  ==========  ==========================

所以**长查询比短查询严格**，粗的是短查询。2 字词能搜到是刻意换来的
（内置分词器做不到），代价就是精度更松。

这一条我一开始写反了（以为「bigram 重叠会让长查询更宽松」），是实测纠正的：
写在这里是为了让下一个改这块的人不必重新推一遍。

## 命中数要一起呈现

单二元组命中可能只是**碰巧**（比如「周报」命中了任何含这两个字的条目）。
所以界面不该只说「命中了」，而该给出命中数与内容让用户自己判断 ——
与设计文档第七章「弱信号必须可解释可反驳」是同一条。
"""

from __future__ import annotations

import dataclasses
import re
import sqlite3
from typing import Any, Literal

from ..domain import RecordType, new_id
from ..storage.repos import RoleRepo
from .clock import Clock

__all__ = [
    "KnowledgeKind",
    "KnowledgeEntry",
    "KnowledgeHit",
    "RetrievalResult",
    "KnowledgeService",
    "index_text",
    "VECTOR_UNAVAILABLE",
    "DEFAULT_LIMIT",
]

#: CJK 统一表意文字（含扩展 A 与兼容区）。
_CJK = r"一-鿿㐀-䶿"
#: 先切出连续 CJK 段，再切出非 CJK 的词（数字/拉丁/标点）。
_TOKEN_RE = re.compile(rf"[{_CJK}]+|[^{_CJK}\s]+")

#: 结果条数上限。刻意小 —— 角色知识是给人看的，不是给检索器看的；
#: 要更多就该去 :meth:`KnowledgeService.list_for_role`。
DEFAULT_LIMIT = 5

#: BM25 落空时回落到「最近若干条」。
TAIL_LIMIT = 3

#: 向量那一腿为什么缺席。**永远有值**，且不是「没命中」。
VECTOR_UNAVAILABLE = (
    "本项目刻意不做向量检索：embedding 需要联网，而本项目要求断网可用"
    "（README「无任何外部资源」）。当前只有 BM25（FTS5）+ 近期回落两条腿。"
)

KnowledgeKind = Literal["偏好", "口径", "历史决策"]

#: 允许的类别。用 :class:`frozenset` 而不是 Literal 单独存在 ——
#: 校验发生在**写入时**，读取时不再判断（那是 parse-don't-validate）。
KINDS: frozenset[str] = frozenset({"偏好", "口径", "历史决策"})

#: 命中来源。自报来源的取值 —— **不许加「其他」**，那会让「说不清怎么找到的」
#: 这件事变得可表达，而它正是不该可表达的东西。
HitVia = Literal["bm25", "tail"]


def index_text(text: str) -> str:
    """把中文切成**重叠二元组**，供 FTS5 的 ``unicode61`` 索引。

    非 CJK 片段（数字、拉丁词）整体保留 —— 它们本来就有词边界，
    切开反而查不准。单个 CJK 字（长度 1）原样保留，没有二元组可用。

    这是**纯函数**且无 IO：分词规则要能被单独测，而它是最容易悄悄坏掉的一环
    —— 坏了的表现是「搜不到」，不是报错。
    """
    out: list[str] = []
    for chunk in _TOKEN_RE.findall(text):
        if re.match(rf"[{_CJK}]", chunk):
            if len(chunk) == 1:
                out.append(chunk)
                continue
            out.extend(chunk[i : i + 2] for i in range(len(chunk) - 1))
        else:
            out.append(chunk)
    return " ".join(out)


@dataclasses.dataclass(frozen=True, slots=True)
class KnowledgeEntry:
    """一条角色知识。``content`` 是原文，**唯一真源**。"""

    id: str
    role_id: str
    kind: str
    content: str
    created_at: Any          # datetime
    source_ref: str | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class KnowledgeHit:
    """一条命中，**带它是怎么被找到的**。"""

    entry: KnowledgeEntry
    via: HitVia
    #: BM25 分数（越小越相关，SQLite 的约定）。**只有 BM25 命中时为 None** ——
    #: tail 回落没有分数，给一个假的 0.0 会让人以为「它不相关」。
    score: float | None = None

    @property
    def via_label(self) -> str:
        return "关键词命中" if self.via == "bm25" else "近期条目（关键词没命中）"


@dataclasses.dataclass(frozen=True, slots=True)
class RetrievalResult:
    """检索结果。**自报来源**是这个类型存在的理由。"""

    hits: tuple[KnowledgeHit, ...] = ()
    #: 结果是否被截断。静默截断等于隐瞒。
    truncated: bool = False
    bm25_hits: int = 0
    tail_hits: int = 0
    #: 恒为 0。见 :data:`VECTOR_UNAVAILABLE`。
    vector_hits: int = 0
    #: 恒有值 —— 「刻意不做」与「没命中」是两件事，必须能区分。
    vector_unavailable_reason: str = VECTOR_UNAVAILABLE
    #: 检索整体不可用（表缺失 / 查询非法）时为 ``True``，此时 hits 为空。
    degraded: bool = False
    degraded_reason: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.hits

    def describe_sources(self) -> str:
        """一句给用户看的来源说明。**空结果也要有** —— 那是「搜过、没找到」。"""
        if self.degraded:
            return f"检索不可用：{self.degraded_reason}"
        if not self.hits:
            return "搜过，没找到"
        parts = []
        if self.bm25_hits:
            parts.append(f"关键词命中 {self.bm25_hits} 条")
        if self.tail_hits:
            parts.append(f"近期条目回落 {self.tail_hits} 条")
        text = "；".join(parts)
        if self.truncated:
            text += "（已截断，还有更多）"
        return text


class KnowledgeService:
    """角色脉络知识的写入与检索。"""

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
        role_id: str,
        kind: str,
        content: str,
        *,
        source_ref: str | None = None,
    ) -> KnowledgeEntry:
        """记一条角色知识。

        ``kind`` **只接受** :data:`KINDS` 里的三类别，非法直接抛 ——
        知识分类是用来检索和呈现的，混进第四类会让两者都失去意义。
        """
        if kind not in KINDS:
            raise ValueError(
                f"角色知识类别必须是 {sorted(KINDS)} 之一，收到 {kind!r}"
            )
        stripped = content.strip()
        if not stripped:
            raise ValueError("角色知识不能为空")
        # 角色必须存在：否则记下一条永远不会被检索到、也永远不会被看见的孤儿知识。
        self._roles.get(role_id)

        now = self._clock.now()
        entry = KnowledgeEntry(
            id=new_id(),
            role_id=role_id,
            kind=kind,
            content=stripped,
            created_at=now,
            source_ref=source_ref,
        )
        self._conn.execute(
            "INSERT INTO role_knowledge"
            " (id, role_id, kind, content, search_text, source_ref, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                entry.id,
                entry.role_id,
                entry.kind,
                entry.content,
                index_text(entry.content),
                entry.source_ref,
                entry.created_at.isoformat(),
            ),
        )
        return entry

    def delete(self, entry_id: str) -> bool:
        """删一条。返回是否真的删掉了。

        FTS 索引由**触发器**同步（见 ``storage/db.py`` 的 ``_KNOWLEDGE_SCHEMA``），
        所以这里不碰索引 —— 碰了就等于两处维护，然后某天漂移。
        """
        cur = self._conn.execute(
            "DELETE FROM role_knowledge WHERE id=?", (entry_id,)
        )
        return cur.rowcount > 0

    # -- 读 ------------------------------------------------------------------ #
    def list_for_role(self, role_id: str, limit: int = 50) -> list[KnowledgeEntry]:
        """按时间倒序列出该角色的全部知识。

        刻意提供「列出全部」而不只给检索 —— 用户要整理角色脉络时需要看全貌，
        检索只在他**已经知道要找什么**时才有用。
        """
        rows = self._conn.execute(
            "SELECT id, role_id, kind, content, source_ref, created_at"
            " FROM role_knowledge WHERE role_id=?"
            " ORDER BY created_at DESC, id DESC LIMIT ?",
            (role_id, int(limit)),
        ).fetchall()
        return [self._row_to_entry(r) for r in rows]

    def retrieve(
        self,
        role_id: str,
        query: str,
        *,
        limit: int = DEFAULT_LIMIT,
    ) -> RetrievalResult:
        """检索该角色的知识。**返回值自带来源说明。**

        两条腿：BM25 优先，落空则回落到最近若干条。回落是**如实标出来**的
        （``via='tail'``），因为「凭相关度找到的」和「只是最近写的」价值不同，
        混起来就等于让用户自己猜。

        任何一步出错都**降级而不是抛**：检索是辅助能力，不该让整个命令失败。
        但降级**必须自报**（``degraded`` + 原因），否则「搜不到」会被误读成
        「没有这条知识」。
        """
        limit = max(1, int(limit))
        try:
            return self._retrieve(role_id, query, limit)
        except sqlite3.OperationalError as exc:
            # 典型触发：库是从 v7 升上来但知识域没建成（不该发生，schema 会建），
            # 或 FTS5 不可用（某些 Python 构建没编译）。
            return RetrievalResult(
                degraded=True,
                degraded_reason=f"检索不可用（{exc}）",
            )

    def _retrieve(
        self, role_id: str, query: str, limit: int
    ) -> RetrievalResult:
        indexed = index_text(query)
        rows: list[tuple[Any, ...]] = []
        if indexed.strip():
            # ``bm25()`` 返回负数，越小越相关（SQLite 的约定）。
            # 外部内容表不能 ``SELECT *`` 虚拟表，所以显式列名。
            rows = self._conn.execute(
                "SELECT k.id, k.role_id, k.kind, k.content, k.source_ref,"
                "       k.created_at, bm25(role_knowledge_fts) AS score"
                "  FROM role_knowledge_fts f"
                "  JOIN role_knowledge k ON k.rowid = f.rowid"
                " WHERE role_knowledge_fts MATCH ? AND k.role_id = ?"
                " ORDER BY score LIMIT ?",
                (indexed, role_id, limit + 1),
            ).fetchall()

        hits: list[KnowledgeHit] = []
        truncated = False
        if rows:
            # 多取一条用来判断截断 —— 静默截断等于隐瞒。
            truncated = len(rows) > limit
            hits = [
                KnowledgeHit(
                    entry=self._row_to_entry(r),
                    via="bm25",
                    score=float(r[6]),
                )
                for r in rows[:limit]
            ]
        else:
            # BM25 落空 -> 回落近期。**标成 tail**，不假装是关键词命中。
            tail = self._conn.execute(
                "SELECT id, role_id, kind, content, source_ref, created_at"
                " FROM role_knowledge WHERE role_id=?"
                " ORDER BY created_at DESC, id DESC LIMIT ?",
                (role_id, TAIL_LIMIT),
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

    @staticmethod
    def _row_to_entry(row: Any) -> KnowledgeEntry:
        from datetime import datetime

        return KnowledgeEntry(
            id=row[0],
            role_id=row[1],
            kind=row[2],
            content=row[3],
            source_ref=row[4],
            created_at=datetime.fromisoformat(row[5]),
        )
