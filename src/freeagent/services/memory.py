"""记忆（memory，设计文档 3.7）：跨角色的 **core 记忆层** + 自报来源的检索。

## 它与 ``knowledge.py`` 是什么关系

`knowledge.py` 是**按角色**的脉络知识（`role_knowledge`，外键到 roles）；
这一层是**跨角色的 core 记忆**（`memory` 表，作用域用 ``scope`` 文本表达）：
关于「你这个人」和「助手自己」的偏好 / 约定 / 修正 / 目标。

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

## 注入集怎么选：最新的 ≠ 最相关的（V1.99 起）

V1.97 把 :meth:`MemoryService.core_block` 接进 ``/ask`` 时，注入集是
**按时间倒序取最新 N 条**。记到三十条之后，与问题相关的那条可能早在预算
之外，而执行器拿到的是 N 条 newest-but-irrelevant。

所以传了 ``query`` 时改按 **BM25 相关度**选（``or`` 模式，见
:meth:`MemoryService._retrieve` 的 ``match`` 说明），并且**不补填**：
只选中两条就是两条，不用近期条目凑满预算 —— 「常驻层要小才有效」，
凑满正是要治的毛病。相关度落空时回落近期（取满预算）：注入不该因为
一个没匹配上的问题整个消失。

## 「用过什么」也要能回答（V1.98 起）

存储 / 可搜索 / 已加载之外还有第四种状态：**被用**。检索与注入都往
``retrieval_log`` / ``retrieval_hits`` 留一条痕（见
:mod:`freeagent.services.retrieval_log`），于是「哪条记忆 90 天没被用上」
从观点变成可数的事实 —— 没有它，框架层的凝练（从记录里现推规律）缺的
正是这批原料，而 V1.96 的教训是：**没有计数的规律不可反驳**。

## 整理也要有入口：review 队列（V1.100 起）

core 要「小才有效」（设计文档 3.7.2），而小不会自己发生 —— 记下来的
东西只会变多。留痕让「好久没用」成为事实，但**事实不产生动作**：没有人
会主动去翻 ``/memory stats``。于是有 review 队列（3.7.8）：
:meth:`MemoryService.review_candidates` 把「一个季度什么都没发生过的
记忆」与「字面相近的对子」摆成候选，动作只有两个，**都由人发起** ——
:meth:`MemoryService.confirm`（还留着）或既有的 :meth:`delete`。

**绝不自动删、绝不自动 consolidation**（3.7.5 的纪律在此原样成立）：
用量低不等于错，字面像不等于冲突。这个队列的全部输出就是「摆出来」。
"""

from __future__ import annotations

import dataclasses
import sqlite3
from datetime import datetime, timedelta
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
from .retrieval_log import record_retrieval, usage_totals

__all__ = [
    "MemoryKind",
    "MemoryEntry",
    "MemoryService",
    "MemoryHit",
    "MemoryUsageRow",
    "MemoryUsageReport",
    "MemoryReviewRow",
    "MemoryNearDuplicate",
    "MemoryReviewReport",
    "CoreBlock",
    "GLOBAL_SCOPE",
    "MEMORY_KINDS",
    "role_scope",
    "scope_label",
    "CORE_BUDGET",
    "DEFAULT_USAGE_DAYS",
    "DEFAULT_REVIEW_DAYS",
    "NEAR_DUPLICATE_THRESHOLD",
]

#: 全局作用域的 ``scope`` 取值。
GLOBAL_SCOPE = "global"

#: 角色作用域的前缀。``role_scope(rid)`` -> ``'role:<rid>'``。
_ROLE_PREFIX = "role:"

MemoryKind = Literal["偏好", "约定", "修正", "目标"]

#: 允许的记忆类别。写时校验（parse-don't-validate）。
#:
#: 与 ``knowledge.KINDS``（偏好 / 口径 / 历史决策）**刻意不同**：
#: 那是「某个角色这件事的口径」，这是「关于我和助手自己的东西」。
#: 混用会让两边的检索都失去意义 —— 「口径」问的是工作事实，
#: 「约定」问的是你我之间怎么协作，它们回答的不是同一类问题。
#:
#: ``目标`` 是 **前瞻、自我期许**（「我在追什么」），与 ``约定``（你我之间
#: 说好的协作方式）正交：一句「今年把英语练到能开会」是目标，一句
#: 「回复先给结论」是约定。它们被 /model 的「承诺与目标」分面并排呈现，
#: 但语义不混（设计文档 3.7 / V1.59）。
MEMORY_KINDS: frozenset[str] = frozenset({"偏好", "约定", "修正", "目标"})

#: core 注入的预算：最多注入多少条。超预算**不静默截断**，见 :meth:`core_block`。
CORE_BUDGET = 12

#: 留痕统计的默认窗口（天）。90 天是「一个季度」的量级 —— 短了会把
#: 「一季度才用一次」的记忆误判成没人要，长了又稀释「最近还用不用」。
#: 窗口可由 ``/memory stats <天数>`` 改，改的只是看法，不是数据。
DEFAULT_USAGE_DAYS = 90

#: review 队列的默认窗口（天）。与 :data:`DEFAULT_USAGE_DAYS` 同一个
#: 量级：**一个季度什么都没发生过**，是「这条还在不在用」的合理追问间隔。
#: 窗口可由 ``/memory review <天数>`` 改，改的只是看法，不是数据。
DEFAULT_REVIEW_DAYS = 90

#: 「内容相近」的判定阈值：``search_text`` 的 bigram 集合 **Jaccard 系数**。
#:
#: 0.5 是「一半以上的二元组重合」—— 到这个程度的两条短句几乎不可能只是
#: 碰巧：不相关的中文短句共享一两个二元组是常态（「回复」「先给」到处都有），
#: 共享一半不是。阈值**刻意偏高**，用的是 V1.96 那条纪律（宁可漏判、不要
#: 错判）：漏掉的相近对还能被 ``/memory search`` 找到，错判会浪费人的一次
#: 查看 —— 而队列的信誉正是由「摆出来的基本都对」撑着的。
#:
#: 代价说在明处（3.7.8 登记）：**前缀同、结论反**的对子（「回复先给结论」
#: vs「回复先给细节」，Jaccard 0.43）会漏 —— 那正是最像矛盾的一类，
#: 而不引模型轮就分不出「矛盾」与「无关」（3.7.5 的理由在此同样成立）。
NEAR_DUPLICATE_THRESHOLD = 0.5


@dataclasses.dataclass(frozen=True, slots=True)
class MemoryUsageRow:
    """一条记忆在窗口内的使用计数。

    ``last_event_at`` 为 ``None`` 表示**全时段**没有任何留痕 —— 与
    「有一次很早的留痕」是两件事，不能用一个假时间糊过去。
    """

    entry: MemoryEntry
    retrievals: int = 0
    injections: int = 0
    last_event_at: Any = None


@dataclasses.dataclass(frozen=True, slots=True)
class MemoryUsageReport:
    """``/memory stats`` 的全部事实：总数 + 逐条计数（含计数 0 的）。"""

    days: int
    retrievals: int = 0
    injections: int = 0
    last_event_at: Any = None
    rows: tuple[MemoryUsageRow, ...] = ()
    #: 留痕**读不出来**时的原因（表缺失 / 查询失败）。有值时计数全是 0，
    #: 呈现层必须把它说出来 —— 把「读不到」显示成「没有」是撒谎。
    degraded_reason: str = ""

    @property
    def used(self) -> tuple[MemoryUsageRow, ...]:
        """窗口内被检索或注入过的。"""
        return tuple(r for r in self.rows if r.retrievals or r.injections)

    @property
    def stale(self) -> tuple[MemoryUsageRow, ...]:
        """窗口内没用过的 —— 含「从未用过」（计数 0 且无时间）。"""
        return tuple(r for r in self.rows if not (r.retrievals or r.injections))


@dataclasses.dataclass(frozen=True, slots=True)
class MemoryReviewRow:
    """review 队列里的一条记忆，带着「为什么它在这里」的全部事实。

    与 :class:`MemoryUsageRow` 的分工：那个给 ``/memory stats`` 报计数，
    这个给 ``/memory review`` 判候选 —— 判据不是计数，是
    :attr:`last_attention_at`（绝对时刻）。
    """

    entry: MemoryEntry
    #: 窗口内被检索 / 被注入的次数（与 ``usage_report`` 同口径，原样带上，
    #: 让「候选」不只是一个判定，还有可核对的事实）。
    retrievals: int = 0
    injections: int = 0
    #: **全时段**最后一次被检索/注入 —— 与 ``usage_report`` 同一条纪律：
    # 把窗口套在时间上，会把「三个月前用过」显示成「从未用过」。
    last_event_at: Any = None
    #: **全时段**最后一次被人确认（``/memory review confirm``）。
    last_confirmed_at: Any = None

    @property
    def last_attention_at(self) -> Any:
        """这条记忆最后一次「被搭理」的时刻：写下来 / 被用过 / 被确认过。

        三者取最大（``created_at`` 按 schema 恒有值，所以这个属性不会为空）。
        候选资格看的正是它 —— 刚记下的记忆没有罪（不因「没用过」进队列），
        而什么都发生过的条目也不该被说成「没人管」（哪怕是很早以前）。
        """
        stamps = [
            s
            for s in (self.entry.created_at, self.last_event_at,
                      self.last_confirmed_at)
            if s is not None
        ]
        return max(stamps) if stamps else None


@dataclasses.dataclass(frozen=True, slots=True)
class MemoryNearDuplicate:
    """一对内容相近的记忆。

    **只说相近，不说冲突**：两个字符串字面上像，可能是记重了两次，
    也可能一条改了另一条没改（那是矛盾），还可能一条是另一条的细化。
    不引模型轮就分不出这三种（3.7.5 的自动 consolidation 不做，理由在
    这里同样成立），所以只把「像」和相近度摆出来，判断留给人。
    """

    left: MemoryEntry
    right: MemoryEntry
    #: 相近度（0..1）：``search_text`` bigram 集合的 Jaccard。
    similarity: float


@dataclasses.dataclass(frozen=True, slots=True)
class MemoryReviewReport:
    """``/memory review`` 的全部事实。

    ``candidates`` 已按「最久没搭理」排好（``last_attention_at`` 升序）——
    整理要从最被忽视的开始，而不是从最新鲜的开始。
    """

    days: int = DEFAULT_REVIEW_DAYS
    #: 候选：窗口内没被检索/注入/确认过，且写下来也超过窗口。
    candidates: tuple[MemoryReviewRow, ...] = ()
    #: 内容相近的对子（与候选资格无关 —— 刚记的记忆也可能记重了）。
    near_duplicates: tuple[MemoryNearDuplicate, ...] = ()
    #: 全部记忆条数（给「共 N 条」用）。
    total: int = 0
    #: 留痕或确认记录**读不出来**时的原因。有值时 ``candidates`` 一定是
    #: 空的 —— 判断不了「好久没动静」就不能摆候选：把故障显示成
    #: 「没有候选」比崩溃更糟（用户会以为刚整理过）。
    degraded_reason: str = ""


@dataclasses.dataclass(frozen=True, slots=True)
class CoreBlock:
    """一次注入选出来的集合。``text`` 为空串表示「还没记过」。

    ``leg`` 记录**这一批是怎么选出来的**（与留痕的 ``legs`` 列同取值，
    于是「怎么选的」在日志里可复核，而不只在一句问候里）：

    - ``bm25`` —— 传了查询且相关度命中（V1.99 起）；
    - ``tail`` —— 传了查询但相关度落空，回落「最近的若干条」；
    - ``recency`` —— 没传查询（或查询切不出 token / 相关度不可用），
      按时间倒序取。

    ``degraded_reason`` 有值表示**相关度那条腿没走成**（FTS 不可用、
    查询串非法）。此时注入仍然发生（按时间倒序），但呈现层必须把它
    说出来 —— 静默回落等于把「按相关度选的」说成真的，而它只是回落。
    """

    text: str = ""
    #: 有条目**没被带上**。超预算、或相关度没选中，都算。
    truncated: bool = False
    #: 没被带上的条数（真实总数来自单独的 COUNT，不是「多取一行」）。
    extra: int = 0
    leg: str = "recency"
    #: 实际带上的条数。
    selected: int = 0
    degraded_reason: str = ""


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
        # 必须提交：连接的 isolation_level 是默认值（隐式事务），
        # 而 ``App.close()`` 只 ``conn.close()`` —— 不提交等于**回滚**。
        # V1.98 实测：只 ``/remember`` 一句就退出，重启后记忆全丢
        # （同一会话里碰巧有过别的仓储写，会把这条 INSERT 一起捎带提交，
        # 所以这个 bug 一直没被发现 —— 症状是「有时记得住」）。
        self._conn.commit()
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
        self._conn.commit()      # 同 add：不提交 = 回滚（V1.98）
        return self.get(entry_id)

    def delete(self, entry_id: str) -> bool:
        """删一条。返回是否真的删掉了。

        FTS 索引由**触发器**同步（见 ``storage/db.py`` 的 ``_MEMORY_SCHEMA``），
        所以这里不碰索引 —— 碰了就等于两处维护，然后某天漂移。
        """
        cur = self._conn.execute("DELETE FROM memory WHERE id=?", (entry_id,))
        deleted = cur.rowcount > 0
        if deleted:
            self._conn.commit()      # 同 add：不提交 = 回滚（V1.98）
        return deleted

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

    def conventions(
        self, role_ids: tuple[str, ...] = (), *, limit: int = 3,
        consumer: str = "",
    ) -> list[MemoryEntry]:
        """约定类记忆（``kind='约定'``）—— 进委派简报的那一份。

        作用域口径与 :meth:`core_block` 相同：global + 各角色。类别过滤
        在 SQL 里做而不是取回来再筛 —— 偏好/目标这类记忆描述的是**人**，
        混进简报就成了拿人的事占执行器的上下文。

        只查不判新旧：记忆可能过期，这条定性由读侧（简报）带出来，
        见 ``conventions_line``。

        :param consumer: 谁在取（委派简报传 ``delegate_brief``）。同
            :meth:`retrieve`：不知道就传空串，不猜。
        """
        scopes = [GLOBAL_SCOPE] + [role_scope(r) for r in role_ids]
        placeholders = ",".join("?" for _ in scopes)
        rows = self._conn.execute(
            "SELECT id, scope, kind, content, source_ref, created_at, updated_at"
            f" FROM memory WHERE kind='约定' AND scope IN ({placeholders})"
            " ORDER BY created_at DESC, id DESC LIMIT ?",
            (*scopes, int(limit)),
        ).fetchall()
        entries = [self._row_to_entry(r) for r in rows]
        if entries:
            # 注入留痕：被拼进上下文的那一刻，正是「被用」的定义（V1.98）。
            record_retrieval(
                self._conn,
                self._clock,
                event="inject",
                layer="memory",
                entry_ids=[e.id for e in entries],
                scope=",".join(scopes),
                legs="recency",
                consumer=consumer,
            )
        return entries

    def retrieve(
        self,
        query: str,
        *,
        scope: str | None = None,
        limit: int = DEFAULT_LIMIT,
        consumer: str = "",
    ) -> RetrievalResult:
        """检索记忆。**返回值自带来源说明。**

        两条腿（BM25 优先、落空回落近期）与 knowledge 层同一套实现，
        ``via='tail'`` 如实标出。任何一步出错都**降级而不是抛**（检索是辅助能力，
        不该让整个命令失败），但降级**必须自报** —— 否则「搜不到」会被误读成
        「没有这条记忆」。

        ``scope`` 只影响**候选集合**；召回时若要「全局 + 某角色」两面一起搜，
        调用方传 ``scope=None`` 并在结果里自行分辨（一期注入走 :meth:`core_block`，
        检索命令默认搜全部）。

        :param consumer: 谁在检索（``/memory search`` 之类）。**不知道就传空串**
            —— 猜一个消费方比不记更糟：留痕的用途之一正是分清「人主动搜」
            与「机器自己用」，猜的那一条会一直污染这个分辨。
        """
        limit = max(1, int(limit))
        try:
            result = self._retrieve(
                query, None if scope is None else (scope,), limit
            )
        except sqlite3.OperationalError as exc:
            result = RetrievalResult(
                degraded=True,
                degraded_reason=f"检索不可用（{exc}）",
            )
        self._log_retrieval(
            query=query, scope=scope or "", result=result, consumer=consumer
        )
        return result

    def _log_retrieval(
        self,
        *,
        query: str,
        scope: str,
        result: RetrievalResult,
        consumer: str,
    ) -> None:
        """给一次检索留痕（V1.98）。写不进去**不影响检索本身** —— 记账失败
        不是检索失败；但它由 ``/memory stats`` 的「最近一次留痕」自报。
        """
        if result.degraded:
            legs, degraded, reason = "none", True, result.degraded_reason
        elif result.bm25_hits:
            legs, degraded, reason = "bm25", False, ""
        elif result.tail_hits:
            legs, degraded, reason = "tail", False, ""
        else:
            legs, degraded, reason = "none", False, ""
        record_retrieval(
            self._conn,
            self._clock,
            event="retrieve",
            layer="memory",
            entry_ids=[h.entry.id for h in result.hits],
            query=query,
            scope=scope,
            legs=legs,
            consumer=consumer,
            degraded=degraded,
            degraded_reason=reason,
        )

    def _retrieve(
        self,
        query: str,
        scopes: tuple[str, ...] | None,
        limit: int,
        *,
        match: str = "and",
    ) -> RetrievalResult:
        """按**作用域集合**检索。``scopes`` 为 ``None`` 表示不限作用域。

        :param match: 布尔式。``and``（默认）= 查询切出的**每个** token
            都必须命中 —— FTS5 空格分隔的既定语义（见 ``knowledge`` 的
            分词实测：长查询比短查询严格）。检索命令用这个：精度优先，
            结果由人自己看、自己判断。
            ``or`` = 任一枚 token 命中即算，仍按 BM25 分数排序。
            注入选择用这个（:meth:`core_block`）：**召回优先** ——
            自然语言问题按 and 几乎永远匹配不上短记忆（问题切出七八个
            二元组，条目只含其中一两个），而注入有条目数预算帽着、
            块自带「可能过期」定性，赌错的代价小，漏掉相关条目的代价大
            （宁可漏判，不要错判在这里的方向是反的：漏掉相关的 = 没带上，
            赌错只多占几行上下文）。
            两种模式走**同一条 SQL、同一个排序**，只是布尔式不同 ——
            这不是两个检索实现（12.1.1），是一个实现的两种问法。
        """
        indexed = index_text(query)
        rows: list[tuple[Any, ...]] = []
        if indexed.strip():
            if match == "or":
                # 逐 token 加引号：token 本身可能是 FTS5 关键字（OR / NOT）
                # 或含引号等特殊字符，加引号才是「按字面」问。引号内若切不出
                # token（纯标点），FTS5 会报语法错 —— 由调用方降级处理，
                # 与 and 模式同一条纪律（检索不可用 ≠ 没有记忆）。
                tokens = indexed.split()
                match_expr = " OR ".join(f'"{t}"' for t in tokens)
            else:
                match_expr = indexed
            where = "WHERE memory_fts MATCH ?"
            params: list[Any] = [match_expr]
            if scopes:
                where += f" AND m.scope IN ({','.join('?' for _ in scopes)})"
                params.extend(scopes)
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
            tail_where = ""
            tail_params: list[Any] = [TAIL_LIMIT]
            if scopes:
                tail_where = f" WHERE scope IN ({','.join('?' for _ in scopes)})"
                tail_params = [*scopes, TAIL_LIMIT]
            tail = self._conn.execute(
                "SELECT id, scope, kind, content, source_ref, created_at, updated_at"
                " FROM memory"
                f"{tail_where}"
                " ORDER BY created_at DESC, id DESC LIMIT ?",
                tuple(tail_params),
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
        query: str = "",
        consumer: str = "",
    ) -> CoreBlock:
        """拼一段可注入的 core 记忆文本。V1.99 起可按相关度选注入集。

        返回 :class:`CoreBlock`：``text`` 为空串表示「还没记过」。

        选择方式（``leg``，与留痕的 ``legs`` 列同取值，于是「怎么选的」
        在日志里可复核）：

        - 传了 ``query`` 且切得出 token：按 **BM25 相关度**选（``or``
          模式，理由见 :meth:`_retrieve`），最多 ``budget`` 条；
        - 相关度落空：回落「最近的 ``budget`` 条」—— 注入不该因为一个
          没匹配上的问题整个消失（V1.97 的行为在此保留）；
        - 没传 ``query``、或查询切不出 token、或相关度不可用：按时间
          倒序取最新的 ``budget`` 条。

        **不补填**：相关度只选中 2 条时就是 2 条，不用近期条目凑满预算
        —— 「常驻层要小才有效」，凑满正是要治的毛病。

        **超预算不静默截断**：真实总数来自单独的 COUNT 而不是「多取一行」
        —— 多取一行只能告诉我们「有没有超出」，报不出「超出多少」，
        而 3.7.6 要求明说「还有 M 条未注入」。

        每一条前面带**口语化定性**（「这是之前记下的」），整段末尾统一说明
        「可能过期、以当前为准」—— 记忆不等于事实，注入必须带这条定性，
        这是 3.7.1 的第三条第一性原理在代码里的落点。

        :param query: 选注入集的依据（``/ask`` 传问题原文）。不知道就传空串。
        :param consumer: 谁在注入（``/ask`` 之类）。同 :meth:`retrieve`：
            不知道就传空串，不猜。
        """
        scopes = [GLOBAL_SCOPE] + [role_scope(r) for r in role_ids]
        placeholders = ",".join("?" for _ in scopes)
        total = int(
            self._conn.execute(
                f"SELECT COUNT(*) FROM memory WHERE scope IN ({placeholders})",
                tuple(scopes),
            ).fetchone()[0]
        )
        shown, leg, degraded_reason = self._select_for_injection(
            query, tuple(scopes), int(budget)
        )
        if not shown:
            return CoreBlock()
        extra = max(0, total - len(shown))
        lines = [
            f"- [{e.kind}] {e.content}" for e in shown
        ]
        body = "\n".join(lines)
        text = (
            "以下是你之前让我记住的东西（可能过期，与当前状态冲突时以当前为准）：\n"
            + body
        )
        # 注入留痕（V1.98）：**注入**才叫「被用」—— 拼好了但没发出去的块
        # 不计，因为那一次没有任何上下文真的读过它。``legs`` 与 ``query``
        # 让「这批是怎么选出来的」在日志里可复核（V1.99）。
        record_retrieval(
            self._conn,
            self._clock,
            event="inject",
            layer="memory",
            entry_ids=[e.id for e in shown],
            query=(query or "").strip(),
            scope=",".join(scopes),
            legs=leg,
            consumer=consumer,
            degraded=bool(degraded_reason),
            degraded_reason=degraded_reason,
        )
        return CoreBlock(
            text=text,
            truncated=extra > 0,
            extra=extra,
            leg=leg,
            selected=len(shown),
            degraded_reason=degraded_reason,
        )

    def _select_for_injection(
        self, query: str, scopes: tuple[str, ...], budget: int
    ) -> tuple[list[MemoryEntry], str, str]:
        """选注入集。返回 ``(条目, leg, 降级原因)``。

        相关度那条腿**任何一步不可用都回落近期**（FTS5 没编译进某些
        Python 构建、查询串非法），但回落必须留下痕迹
        （``degraded_reason``）—— 静默回落等于把「按相关度选的」说成真的。
        """
        q = (query or "").strip()
        if not q or not index_text(q).strip():
            # 查询切不出 token（空白）：没有可问的相关度。这不是
            # 「搜过没命中」，所以 leg 记 recency 而不是 tail，
            # 日志里也不留一个查询词假装搜过。
            return self._recent_for_injection(scopes, budget), "recency", ""
        try:
            result = self._retrieve(q, scopes, budget, match="or")
        except sqlite3.OperationalError as exc:
            return (
                self._recent_for_injection(scopes, budget),
                "recency",
                f"相关度检索不可用（{exc}），按时间倒序取",
            )
        if result.bm25_hits:
            return [h.entry for h in result.hits], "bm25", ""
        # 相关度落空 -> 回落近期。取满预算而不是 ``TAIL_LIMIT``：
        # 那个上限是**检索界面**的选择（给人看最近几条），不是注入政策。
        return self._recent_for_injection(scopes, budget), "tail", ""

    def _recent_for_injection(
        self, scopes: tuple[str, ...], budget: int
    ) -> list[MemoryEntry]:
        """按时间倒序取注入集（无相关度可用的那条腿）。"""
        placeholders = ",".join("?" for _ in scopes)
        rows = self._conn.execute(
            "SELECT id, scope, kind, content, source_ref, created_at, updated_at"
            f" FROM memory WHERE scope IN ({placeholders})"
            " ORDER BY created_at DESC, id DESC LIMIT ?",
            (*scopes, int(budget)),
        ).fetchall()
        return [self._row_to_entry(r) for r in rows]

    # -- 留痕统计 -------------------------------------------------------------- #
    def usage_report(
        self, *, days: int = DEFAULT_USAGE_DAYS
    ) -> MemoryUsageReport:
        """窗口内每条记忆的「被检索 / 被注入」计数（V1.98）。

        一条 **LEFT JOIN** 出全貌：没有留痕的记忆也在结果里（计数 0）。
        若只查有留痕的条目，「从未用过」与「窗口内没用过」会因为这颗
        行不存在而混为一谈 —— 而这两件事对「要不要删掉它」的回答不同。

        **窗口只管计数，不管时间**：``last_event_at`` 取的是**全时段**最后一次。
        把窗口也套在时间上，就会把「三个月前用过」显示成「从未用过」——
        那是撒谎，而且恰好扔掉了删不删最需要的那个数。

        计数 0 **不自动建议删除**：记忆是「当时这么认为」，用量低不等于
        错（3.7.5 的不自动 consolidation 在这里同样成立）。这个报告只摆事实。
        """
        window = max(1, int(days))
        since = (self._clock.now() - timedelta(days=window)).isoformat()
        totals = usage_totals(
            self._conn, layer="memory", since=datetime.fromisoformat(since)
        )
        try:
            rows = self._conn.execute(
                "SELECT m.id, m.scope, m.kind, m.content, m.source_ref,"
                "       m.created_at, m.updated_at,"
                "       COALESCE(SUM(CASE WHEN l.event = 'retrieve'"
                "                          AND l.created_at >= ? THEN 1 ELSE 0 END), 0),"
                "       COALESCE(SUM(CASE WHEN l.event = 'inject'"
                "                          AND l.created_at >= ? THEN 1 ELSE 0 END), 0),"
                "       MAX(l.created_at)"
                "  FROM memory m"
                "  LEFT JOIN retrieval_hits h ON h.entry_id = m.id"
                "  LEFT JOIN retrieval_log l ON l.id = h.log_id"
                " GROUP BY m.id"
                # 最近用过的排前面；从未用过的（MAX 为 NULL）沉底。
                # 同刻兜底用 id（同 created_at 的既定纪律），不新增 seq 列。
                " ORDER BY MAX(l.created_at) DESC, m.created_at DESC, m.id DESC",
                (since, since),
            ).fetchall()
        except sqlite3.Error as exc:
            # 留痕读不出来 ≠ 没有留痕，更 ≠ 没有记忆。条目照列（计数全 0），
            # degraded_reason 必须让用户看见 —— 否则「0 次」会被当真相，
            # 而 early-return 空 rows 会把故障伪装成「还没记过任何记忆」。
            # 这条查询不碰留痕表，所以它自己不会再失败；memory 表本身
            # 就是小的（core 记忆），不加 LIMIT。
            rows = self._conn.execute(
                "SELECT id, scope, kind, content, source_ref, created_at, updated_at"
                " FROM memory ORDER BY created_at DESC, id DESC"
            ).fetchall()
            return MemoryUsageReport(
                days=window,
                degraded_reason=f"留痕读不出来（{exc}）",
                rows=tuple(
                    MemoryUsageRow(entry=self._row_to_entry(r))
                    for r in rows
                ),
            )
        return MemoryUsageReport(
            days=window,
            retrievals=totals.retrievals,
            injections=totals.injections,
            last_event_at=totals.last_event_at,
            degraded_reason=totals.degraded_reason,
            rows=tuple(
                MemoryUsageRow(
                    entry=self._row_to_entry(r),
                    retrievals=int(r[7]),
                    injections=int(r[8]),
                    last_event_at=datetime.fromisoformat(r[9]) if r[9] else None,
                )
                for r in rows
            ),
        )

    # -- review 队列（V1.100 / 3.7.8）----------------------------------------- #
    def review_candidates(
        self, *, days: int = DEFAULT_REVIEW_DAYS
    ) -> MemoryReviewReport:
        """列 review 候选。**只摆不动** —— 没有自动删、没有自动 consolidation。

        候选两条口径，独立计算：

        - **好久没动静**：``last_attention_at`` 早于窗口起点。该时刻取
          **全时段** ``max(created_at, 最后一次留痕, 最后一次确认)`` ——
          写下来、被用过、被确认过都算「动静」（与 ``usage_report``
          同一条纪律：窗口只管计数，时间不设窗）。
        - **内容相近**：bigram 集合 Jaccard ≥ :data:`NEAR_DUPLICATE_THRESHOLD`
          的对子，不限类别。**只说相近不说冲突**，理由见
          :class:`MemoryNearDuplicate`。

        动作只有两个且都由人发起：:meth:`confirm`（还留着）与既有的
        :meth:`delete`。3.7.5 的不自动 consolidation 在此原样成立 ——
        用量低不等于错，字面像不等于冲突。

        与设计文档 3.7.2 划的界：那一节说「review 层（自动 consolidation
        的审计面）本仓不做」，本方法**不是**那一层 —— 这里没有模型轮，
        候选全部由既有事实（留痕、确认记录、字面相近度）算出。
        """
        window = max(1, int(days))
        usage = self.usage_report(days=window)   # 自己不会抛（降级自报）
        if usage.degraded_reason:
            # 留痕读不出来 → 判断不了「好久没动静」。此时**不能**把全部
            # 条目当候选摆出来（那会把故障显示成「全都好久没用」），也不
            # 能显示成「没有候选」（那等于说整理干净了）。只报原因。
            return MemoryReviewReport(
                days=window, degraded_reason=usage.degraded_reason
            )
        try:
            confirmed = self._last_confirmed_map()
        except sqlite3.Error as exc:
            # 确认记录读不出来 → 「确认过」会被显示成「没确认过」，
            # 候选因此偏大。同一条纪律：读不出来要自报，不硬摆。
            return MemoryReviewReport(
                days=window, degraded_reason=f"确认记录读不出来（{exc}）"
            )
        since = self._clock.now() - timedelta(days=window)
        rows = tuple(
            MemoryReviewRow(
                entry=r.entry,
                retrievals=r.retrievals,
                injections=r.injections,
                last_event_at=r.last_event_at,
                last_confirmed_at=confirmed.get(r.entry.id),
            )
            for r in usage.rows
        )
        # ``last_attention_at`` 不会为 None：``created_at`` 按 schema 恒有值
        # （见该属性的说明），三者取最大必有结果。
        candidates = sorted(
            (r for r in rows if r.last_attention_at < since),
            # 最久没搭理在前；同刻兜底用 id（同 created_at 的既定纪律），
            # 不新增 seq 列。
            key=lambda r: (r.last_attention_at, r.entry.id),
        )
        return MemoryReviewReport(
            days=window,
            candidates=tuple(candidates),
            near_duplicates=self._near_duplicate_pairs([r.entry for r in rows]),
            total=len(rows),
        )

    def confirm(self, entry_id: str) -> MemoryEntry | None:
        """确认这条记忆还有效 —— review 队列的「留」。

        写一条 ``memory_reviews`` 行，该条退出**一个窗口**的候选。返回
        确认后的条目；id 不存在返回 ``None``（调用方报错）。

        **不碰内容、不碰 ``updated_at``**：「我看过它，它还作数」与
        「我改了它」是两件事 —— 混在一起，「这条什么时候变的」就再也
        答不出来。

        **确认不是永久静音**：窗口过后仍没被用过，它还会再进候选。
        静默地永远不再出现，正是本仓反复避开的那类事（只进不出的解法
        是 review 队列，不是悄悄扔）。
        """
        row = self._conn.execute(
            "SELECT id FROM memory WHERE id=?", (entry_id,)
        ).fetchone()
        if row is None:
            return None
        self._conn.execute(
            "INSERT INTO memory_reviews (id, entry_id, created_at)"
            " VALUES (?, ?, ?)",
            (new_id(), entry_id, self._clock.now().isoformat()),
        )
        # 必须提交：连接的 isolation_level 是默认值（隐式事务），而
        # ``App.close()`` 只 ``conn.close()`` —— 不提交等于**回滚**。
        # V1.98 实测过同一个坑（「只 /remember 一句就退出」必现丢记忆），
        # 每个新写入口都要重新付一次学费才不会忘。
        self._conn.commit()
        return self.get(entry_id)

    def _last_confirmed_map(self) -> dict[str, datetime]:
        """每条记忆**全时段**最后一次被确认的时刻（没确认过的不在表里）。

        **读失败抛**（由 ``review_candidates`` 转成降级报告）：确认记录
        读不出来时静默当成「没确认过」，会让候选集合偏大 —— 把故障
        显示成「待整理」与显示成「没有候选」一样是撒谎。
        """
        rows = self._conn.execute(
            "SELECT entry_id, MAX(created_at) FROM memory_reviews"
            " GROUP BY entry_id"
        ).fetchall()
        return {r[0]: datetime.fromisoformat(r[1]) for r in rows if r[1]}

    def _near_duplicate_pairs(
        self, entries: list[MemoryEntry]
    ) -> tuple[MemoryNearDuplicate, ...]:
        """字面相近的对子（bigram 集合的 Jaccard，阈值见常量）。

        为什么从 ``content`` 现算而不读 ``search_text`` 列：那一列是写入
        时算的（FTS 的产物），而 **content 才是唯一真源** —— 从真源现算
        保证判定与正文永远一致（哪怕索引曾经漂过）。``index_text`` 是纯
        函数，代价可忽略。

        **不判冲突**（见 :class:`MemoryNearDuplicate`）；单字条目切不出
        二元组，**不硬判**（空集合的 Jaccard 没有定义，跳过而不是除零）。
        """
        grams: dict[str, frozenset[str]] = {}
        for e in entries:
            tokens = index_text(e.content).split()
            # 只留长度 ≥2 的 token：单个汉字、单个标点在两句里同时出现
            # 不代表什么（「，」几乎无处不在），却会凭空抬高相近度。
            grams[e.id] = frozenset(t for t in tokens if len(t) >= 2)
        pairs: list[MemoryNearDuplicate] = []
        for i, left in enumerate(entries):
            for right in entries[i + 1 :]:
                a, b = grams[left.id], grams[right.id]
                if not a or not b:
                    continue
                similarity = len(a & b) / len(a | b)
                if similarity >= NEAR_DUPLICATE_THRESHOLD:
                    pairs.append(
                        MemoryNearDuplicate(
                            left=left, right=right, similarity=similarity
                        )
                    )
        # 最像的排前面；同分按 id 兜底（同刻定序的既定纪律）。
        pairs.sort(key=lambda p: (-p.similarity, p.left.id, p.right.id))
        return tuple(pairs)

    @staticmethod
    def _row_to_entry(row: Any) -> MemoryEntry:
        return MemoryEntry(
            id=row[0],
            scope=row[1],
            kind=row[2],
            content=row[3],
            source_ref=row[4],
            created_at=datetime.fromisoformat(row[5]),
            updated_at=datetime.fromisoformat(row[6]),
        )
