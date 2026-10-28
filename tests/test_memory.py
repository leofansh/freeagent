"""记忆（设计文档 3.7 / V1.47）的测试。

分七组：
1. 值语义与写入校验（kind / scope / 空内容 / 孤儿角色）；
2. 检索（两条腿 + 来源自报 + 降级）；
3. 编辑与删除（用户可改可删是硬要求）；
4. core 注入（预算、定性、超预算不静默）；
5. 检索留痕与使用统计（V1.98）；
6. 相关度注入（V1.99：or 模式、回落、不补填、作用域、降级自报）；
7. review 队列（V1.100：候选口径、确认不碰内容且落盘、相近对子、降级不摆候选）。

刻意**不改** knowledge 的测试 —— 两层的检索机制共用，但归属与作用域不同，
断言必须落在本层自己的行为上，而不是「共用就没问题」。
"""

from __future__ import annotations

import io
import sqlite3

import pytest

from freeagent.app import App, build_app
from freeagent.services.memory import (
    CORE_BUDGET,
    DEFAULT_REVIEW_DAYS,
    DEFAULT_USAGE_DAYS,
    GLOBAL_SCOPE,
    MEMORY_KINDS,
    NEAR_DUPLICATE_THRESHOLD,
    MemoryService,
    role_scope,
    scope_label,
)
from freeagent.services.sorting import EnergyWindows


@pytest.fixture()
def mem(app: App, roles: dict) -> MemoryService:
    return app.memory


# -- 1. 值语义与写入校验 ------------------------------------------------------ #

def test_add_global_default(mem: MemoryService):
    e = mem.add("我不喜欢早上开会", kind="偏好")
    assert e.scope == GLOBAL_SCOPE
    assert e.is_global
    assert e.kind == "偏好"
    assert mem.get(e.id) is not None


def test_add_role_scope(mem: MemoryService, roles: dict):
    e = mem.add("周报先给我结论", kind="约定", scope=role_scope(roles["work"].id))
    assert not e.is_global
    assert e.scope == f"role:{roles['work'].id}"


def test_bad_kind_rejected(mem: MemoryService):
    with pytest.raises(ValueError, match="记忆类别"):
        mem.add("x", kind="随便")


def test_empty_content_rejected(mem: MemoryService):
    with pytest.raises(ValueError, match="不能为空"):
        mem.add("   ", kind="偏好")


def test_bad_scope_rejected(mem: MemoryService):
    with pytest.raises(ValueError, match="记忆范围"):
        mem.add("x", kind="偏好", scope="team:1")


def test_orphan_role_scope_rejected(mem: MemoryService):
    with pytest.raises(Exception):
        # 角色不存在 -> 拒绝，而不是记下一条永远不会被看见的孤儿记忆。
        mem.add("x", kind="偏好", scope=role_scope("no-such-role"))


def test_kinds_are_four(mem):
    # V1.59 起新增「目标」类（前瞻自我期许），与「约定」正交。
    assert MEMORY_KINDS == frozenset({"偏好", "约定", "修正", "目标"})


def test_scope_label():
    assert scope_label(GLOBAL_SCOPE) == "全局"
    assert scope_label("role:abc") == "abc"


# -- 2. 检索 ------------------------------------------------------------------ #

def test_retrieve_bm25_hit(mem: MemoryService):
    mem.add("交付销售周报", kind="偏好")
    mem.add("周五下午别排会", kind="约定")
    r = mem.retrieve("周报")
    assert r.bm25_hits >= 1
    assert any("周报" in h.entry.content for h in r.hits)
    assert r.vector_unavailable_reason  # 恒有值
    assert not r.degraded


def test_retrieve_tail_fallback_is_labelled(mem: MemoryService):
    mem.add("甲", kind="偏好")
    r = mem.retrieve("完全不相关的词")
    assert r.tail_hits >= 1
    assert all(h.via == "tail" for h in r.hits)
    assert all(h.score is None for h in r.hits)  # 不假装有分


def test_retrieve_empty_says_searched(mem: MemoryService):
    r = mem.retrieve("什么都没有")
    # 空结果也要有来源说明（「搜过，没找到」），不许留白。
    assert r.is_empty
    assert "搜过" in r.describe_sources()


def test_retrieve_scope_filters(mem: MemoryService, roles: dict):
    mem.add("全局的周报口径", kind="偏好")
    mem.add("角色的周报口径", kind="约定", scope=role_scope(roles["work"].id))
    r = mem.retrieve("周报", scope=GLOBAL_SCOPE)
    assert [h.entry.content for h in r.hits] == ["全局的周报口径"]


def test_truncated_is_reported(mem: MemoryService):
    for i in range(6):
        mem.add(f"周报条目 {i}", kind="偏好")
    r = mem.retrieve("周报", limit=3)
    assert r.truncated
    assert "截断" in r.describe_sources()


# -- 3. 编辑与删除 ------------------------------------------------------------ #

def test_edit_content(mem: MemoryService):
    e = mem.add("旧内容", kind="偏好")
    changed = mem.edit(e.id, "新内容")
    assert changed is not None
    assert changed.content == "新内容"
    assert changed.kind == "偏好"          # 类别不可改
    assert changed.scope == GLOBAL_SCOPE   # 范围不可改


def test_edit_missing_returns_none(mem: MemoryService):
    assert mem.edit("nope", "x") is None


def test_edit_empty_rejected(mem: MemoryService):
    e = mem.add("旧", kind="偏好")
    with pytest.raises(ValueError):
        mem.edit(e.id, "  ")


def test_edit_reflected_in_search(mem: MemoryService):
    e = mem.add("销售数据", kind="偏好")
    mem.edit(e.id, "财务数据")
    assert any("财务" in h.entry.content for h in mem.retrieve("财务").hits)


def test_delete(mem: MemoryService):
    e = mem.add("要删的", kind="偏好")
    assert mem.delete(e.id) is True
    assert mem.get(e.id) is None
    assert mem.delete(e.id) is False


def test_deleted_not_searchable(mem: MemoryService):
    e = mem.add("独一无二的词藻", kind="偏好")
    mem.delete(e.id)
    r = mem.retrieve("词藻")
    assert e.id not in [h.entry.id for h in r.hits]


def test_list_all_lists_everything(mem: MemoryService):
    mem.add("一", kind="偏好")
    mem.add("二", kind="约定")
    assert len(mem.list_all()) == 2


# -- 4. core 注入 ------------------------------------------------------------- #

def test_core_block_empty(mem: MemoryService):
    block = mem.core_block()
    assert block.text == ""
    assert block.truncated is False
    assert block.extra == 0
    assert block.selected == 0


def test_core_block_has_qualifier(mem: MemoryService):
    mem.add("回复先给结论", kind="约定")
    block = mem.core_block()
    assert "回复先给结论" in block.text
    # 定性必须有：记忆不等于事实。
    assert "可能过期" in block.text
    assert "以当前为准" in block.text


def test_core_block_includes_role(mem: MemoryService, roles: dict):
    mem.add("全局的", kind="偏好")
    mem.add("工作的", kind="约定", scope=role_scope(roles["work"].id))
    block = mem.core_block((roles["work"].id,))
    assert "全局的" in block.text
    assert "工作的" in block.text


def test_core_block_excludes_other_role(mem: MemoryService, roles: dict):
    mem.add("家庭的", kind="偏好", scope=role_scope(roles["family"].id))
    block = mem.core_block((roles["work"].id,))
    assert "家庭的" not in block.text


def test_core_block_budget_not_silent(mem: MemoryService):
    for i in range(CORE_BUDGET + 3):
        mem.add(f"条目{i}", kind="偏好")
    block = mem.core_block(budget=CORE_BUDGET)
    assert block.truncated is True
    assert block.extra == 3
    assert block.selected == CORE_BUDGET
    assert len([1 for line in block.text.splitlines() if line.startswith("- ")]) == CORE_BUDGET


# -- 5. 终端命令族 ------------------------------------------------------------ #

def test_cli_remember_and_list(app: App, roles):
    from freeagent.cli.app import Repl

    buf = io.StringIO()
    r = Repl(app, out=buf)
    r.handle("/remember 我不喜欢早上开会")
    out = buf.getvalue()
    assert "记住了" in out

    buf.seek(0)
    buf.truncate(0)
    r.handle("/memory list")
    out = buf.getvalue()
    assert "我不喜欢早上开会" in out
    assert "偏好" in out


def test_cli_memory_search_reports_source(app: App, roles):
    from freeagent.cli.app import Repl

    buf = io.StringIO()
    r = Repl(app, out=buf)
    r.handle("/memory add 约定 回复先给结论")
    buf.seek(0)
    buf.truncate(0)
    r.handle("/memory search 结论")
    out = buf.getvalue()
    assert "检索：" in out          # 来源说明必须打出来
    assert "回复先给结论" in out


def test_cli_memory_add_role(app: App, roles):
    from freeagent.cli.app import Repl

    buf = io.StringIO()
    r = Repl(app, out=buf)
    r.handle("/memory add-role 工作项目A 约定 周报先给结论")
    assert "工作项目A" in buf.getvalue()


def test_cli_memory_del(app: App, roles):
    from freeagent.cli.app import Repl

    buf = io.StringIO()
    r = Repl(app, out=buf)
    r.handle("/remember 要删掉的")
    buf.seek(0)
    buf.truncate(0)
    # 取 id 前缀不方便，直接从服务层找
    entry = app.memory.list_all()[0]
    r.handle(f"/memory del {entry.id}")
    assert "删掉了" in buf.getvalue()
    assert app.memory.get(entry.id) is None


def test_cli_memory_del_missing_reports_error(app: App, roles):
    from freeagent.cli.app import Repl

    buf = io.StringIO()
    r = Repl(app, out=buf)
    r.handle("/memory del nope")
    # handle() 会把 FreeAgentError 兜成「[出错] …」打出来，不往上抛。
    assert "[出错]" in buf.getvalue()
    assert "没找到" in buf.getvalue()


def test_cli_memory_empty_says_so(app: App, roles):
    from freeagent.cli.app import Repl

    buf = io.StringIO()
    r = Repl(app, out=buf)
    r.handle("/memory list")
    assert "还没记过" in buf.getvalue()


# -- 5. 约定查询（进委派简报的那一份，V1.95 / 12.10.10） -------------------- #

class TestConventions:
    """``conventions()``：只取「约定」类，作用域口径与 core_block 相同。

    偏好/目标描述的是**人**，混进简报就是拿人的事占执行器的上下文 ——
    类别过滤在 SQL 里做，不是取回来再筛。
    """

    def test_kind_filter_and_scopes(self, mem, roles):
        mem.add("本项目禁用 requests，只用标准库", kind="约定")
        mem.add(
            "周报先给结论",
            kind="约定", scope=role_scope(roles["work"].id),
        )
        mem.add("我不喜欢早上开会", kind="偏好")           # 人的事，不进简报
        mem.add("年内跑完半马", kind="目标", scope=role_scope(roles["work"].id))
        got = mem.conventions((roles["work"].id,))
        contents = [e.content for e in got]
        assert "本项目禁用 requests，只用标准库" in contents
        assert "周报先给结论" in contents
        assert len(got) == 2, "偏好/目标必须被类别过滤挡掉"

    def test_other_role_excluded(self, mem, roles):
        mem.add(
            "家庭账单走纸质", kind="约定", scope=role_scope(roles["family"].id)
        )
        assert mem.conventions((roles["work"].id,)) == []

    def test_bounded(self, mem):
        for i in range(5):
            mem.add(f"约定{i}", kind="约定")
        assert len(mem.conventions(())) == 3, "简报有界，默认三条"

    def test_empty_by_default(self, mem):
        assert mem.conventions(()) == []


# -- 5. 检索留痕（3.7 / V1.98）----------------------------------------------- #

class TestRetrievalLog:
    """「用过什么」要能回答，不只「记过什么」。

    治的是框架凝练缺原料：没有留痕，「哪条记忆 90 天没被用上」是观点；
    有了它才是可数的事实（V1.96：没有计数的观点不可反驳）。
    """

    def _log_rows(self, mem: MemoryService) -> list:
        return mem._conn.execute(
            "SELECT event, layer, legs, query, scope, consumer, hit_count,"
            "       degraded, degraded_reason"
            "  FROM retrieval_log ORDER BY created_at"
        ).fetchall()

    def _hit_rows(self, mem: MemoryService) -> list:
        return mem._conn.execute(
            "SELECT entry_id, position FROM retrieval_hits ORDER BY position"
        ).fetchall()

    def test_retrieve_leaves_a_log(self, mem: MemoryService):
        e = mem.add("回复先给结论", kind="约定")
        result = mem.retrieve("结论", consumer="/memory search")
        assert result.hits
        rows = self._log_rows(mem)
        assert len(rows) == 1
        row = rows[0]
        assert row["event"] == "retrieve"
        assert row["layer"] == "memory"
        assert row["legs"] == "bm25"
        assert row["consumer"] == "/memory search"
        assert row["hit_count"] == 1
        hits = self._hit_rows(mem)
        assert [h["entry_id"] for h in hits] == [e.id]

    def test_tail_fallback_logged_as_tail(self, mem: MemoryService):
        mem.add("回复先给结论", kind="约定")
        result = mem.retrieve("完全不相关的词xyz")
        assert result.tail_hits == 1
        assert self._log_rows(mem)[0]["legs"] == "tail"

    def test_empty_result_is_logged_too(self, mem: MemoryService):
        """「搜过、没找到」正是本地够不够用的判据 —— 不留痕就永远不知道。"""
        result = mem.retrieve("还没记过任何东西")
        assert result.is_empty
        rows = self._log_rows(mem)
        assert len(rows) == 1
        assert rows[0]["legs"] == "none"
        assert rows[0]["hit_count"] == 0
        assert self._hit_rows(mem) == []

    def test_degraded_retrieval_is_logged_with_reason(self, mem, monkeypatch):
        def _boom(*a, **kw):
            raise sqlite3.OperationalError("no such table: memory_fts")

        monkeypatch.setattr(mem, "_retrieve", _boom)
        result = mem.retrieve("结论")
        assert result.degraded
        row = self._log_rows(mem)[0]
        assert row["degraded"] == 1
        assert row["legs"] == "none"
        assert "memory_fts" in row["degraded_reason"]

    def test_injection_is_logged(self, mem: MemoryService):
        e = mem.add("回复先给结论", kind="约定")
        mem.core_block(consumer="/ask")
        row = self._log_rows(mem)[0]
        assert row["event"] == "inject"
        assert row["legs"] == "recency"
        assert row["consumer"] == "/ask"
        assert [h["entry_id"] for h in self._hit_rows(mem)] == [e.id]

    def test_empty_memory_logs_nothing(self, mem: MemoryService):
        block = mem.core_block()
        assert block.text == ""
        assert self._log_rows(mem) == []

    def test_conventions_injection_is_logged(self, mem: MemoryService, roles):
        e = mem.add(
            "本项目禁用 requests", kind="约定",
            scope=role_scope(roles["work"].id),
        )
        got = mem.conventions((roles["work"].id,), consumer="delegate_brief")
        assert [x.id for x in got] == [e.id]
        row = self._log_rows(mem)[0]
        assert row["event"] == "inject"
        assert row["consumer"] == "delegate_brief"
        assert row["scope"] == f"global,role:{roles['work'].id}"

    def test_log_and_hits_are_atomic(self, mem: MemoryService, monkeypatch):
        """命中行写失败时日志行不能留下来 —— hit_count 会从此说谎。"""
        mem.add("回复先给结论", kind="约定")
        real = mem._conn

        class _FlakyConn:
            """只在写 retrieval_hits 时炸，别的原样透传。

            不能 monkeypatch ``conn.execute``（C 扩展的属性只读），
            所以包一层代理换掉整个连接对象。
            """

            def execute(self, sql, *args):
                if "INSERT INTO retrieval_hits" in sql:
                    raise sqlite3.OperationalError("disk I/O error")
                return real.execute(sql, *args)

        monkeypatch.setattr(mem, "_conn", _FlakyConn())
        assert mem.retrieve("结论").hits        # 检索本身不受影响
        assert self._log_rows(mem) == [], "只剩日志行 = hit_count 说谎"

    def test_bad_event_rejected(self, mem: MemoryService):
        from freeagent.services.retrieval_log import record_retrieval

        with pytest.raises(ValueError):
            record_retrieval(
                mem._conn, mem._clock, event="别的", layer="memory"
            )


class TestUsageReport:
    """/memory stats 的读侧：逐条计数 + 「好久没用」分面。"""

    def test_counts_retrievals_and_injections(self, mem: MemoryService):
        e = mem.add("回复先给结论", kind="约定")
        mem.retrieve("结论", consumer="/memory search")
        mem.retrieve("结论", consumer="/memory search")
        mem.core_block(consumer="/ask")
        report = mem.usage_report()
        assert report.days == 90
        assert report.retrievals == 2
        assert report.injections == 1
        row = report.rows[0]
        assert row.entry.id == e.id
        assert row.retrievals == 2
        assert row.injections == 1
        assert row.last_event_at is not None
        assert report.used == (row,)
        assert report.stale == ()

    def test_never_used_shows_up_as_stale(self, mem: MemoryService):
        mem.add("回复先给结论", kind="约定")
        report = mem.usage_report()
        assert report.rows
        assert report.used == ()
        row = report.stale[0]
        assert row.retrievals == 0
        assert row.last_event_at is None, "从未用过不能给一个假时间"

    def test_window_excludes_older_events(self, mem: MemoryService, clock):
        mem.add("回复先给结论", kind="约定")
        mem.retrieve("结论")
        clock.advance(days=91)
        report = mem.usage_report(days=90)
        assert report.retrievals == 0, "窗口外的事件不该算进「最近 90 天」"
        assert report.stale, "用过但超出窗口 = 与从未用过同一面（时间不同）"
        assert report.stale[0].last_event_at is not None

    def test_window_is_adjustable(self, mem: MemoryService, clock):
        mem.add("回复先给结论", kind="约定")
        mem.retrieve("结论")
        clock.advance(days=31)
        assert mem.usage_report(days=30).retrievals == 0
        assert mem.usage_report(days=60).retrievals == 1

    def test_empty_memory_reports_nothing(self, mem: MemoryService):
        report = mem.usage_report()
        assert report.rows == ()
        assert report.retrievals == 0
        assert report.last_event_at is None


class TestWritesSurviveRestart:
    """V1.98 修的真 bug：写方法从不 commit，``App.close()`` 一关就回滚。

    症状极隐蔽 —— 同一会话里只要碰巧有过别的仓储写（autocommit=True，
    每次写都提交），这条 INSERT 会被一起捎带提交，于是「有时记得住」。
    只有「只 /remember 一句就退出」必现，而那正是最常见的用法。
    """

    def test_remember_survives_restart(self, tmp_path):
        from freeagent.services.clock import SystemClock

        app = build_app(
            tmp_path / "agent.db", clock=SystemClock(),
            energy_windows=EnergyWindows(),
        )
        buf = io.StringIO()
        from freeagent.cli.app import Repl

        Repl(app, out=buf).handle("/remember 回复先给结论")
        app.close()

        app2 = build_app(
            tmp_path / "agent.db", clock=SystemClock(),
            energy_windows=EnergyWindows(),
        )
        try:
            contents = [e.content for e in app2.memory.list_all()]
            assert contents == ["回复先给结论"]
        finally:
            app2.close()

    def test_edit_and_delete_survive_restart(self, tmp_path):
        from freeagent.services.clock import SystemClock

        app = build_app(
            tmp_path / "agent.db", clock=SystemClock(),
            energy_windows=EnergyWindows(),
        )
        entry = app.memory.add("回复先给结论", kind="约定")
        app.memory.edit(entry.id, "回复先给结论，再给细节")
        app.memory.add("这条待删", kind="偏好")
        victim = [e for e in app.memory.list_all() if e.content == "这条待删"][0]
        app.memory.delete(victim.id)
        app.close()

        app2 = build_app(
            tmp_path / "agent.db", clock=SystemClock(),
            energy_windows=EnergyWindows(),
        )
        try:
            contents = sorted(e.content for e in app2.memory.list_all())
            assert contents == ["回复先给结论，再给细节"]
        finally:
            app2.close()


# -- 6. 相关度注入（V1.99 / 3.7.6）------------------------------------------- #

class TestRelevanceInjection:
    """``core_block(query=...)``：注入集从「最新的」换成「最相关的」。

    治的是 V1.97 留下的口子：那时注入一律按时间倒序取最新 N 条 ——
    记到三十条之后，与问题相关的那条可能早在预算之外，而执行器拿到的是
    N 条 newest-but-irrelevant。

    三条边界（每条都有实测撑着，见 ``knowledge`` 的分词文档）：

    - 相关度用 **or** 模式：自然语言问题按 and 永远匹配不上短记忆
      （问题切出五七个二元组，条目只含其中一两个）；
    - 落空回落近期且**取满预算**：注入不该因为一个没匹配上的问题整个消失；
    - **不补填**：只选中两条就是两条，不用近期条目凑满预算 ——
      「常驻层要小才有效」，凑满正是要治的毛病。
    """

    def _last_log(self, mem: MemoryService) -> dict:
        row = mem._conn.execute(
            "SELECT event, legs, query, hit_count, degraded, degraded_reason"
            "  FROM retrieval_log ORDER BY created_at DESC, id DESC LIMIT 1"
        ).fetchone()
        return {
            "event": row[0], "legs": row[1], "query": row[2],
            "hit_count": row[3], "degraded": row[4],
            "degraded_reason": row[5],
        }

    def test_relevant_beats_recent(self, mem: MemoryService):
        """旧的相关条目要赢过一打新的不相关条目。"""
        mem.add("本项目禁用 requests", kind="约定")
        for i in range(CORE_BUDGET + 3):
            mem.add(f"第 {i} 条无关内容", kind="偏好")
        block = mem.core_block(query="requests 能用吗")
        assert block.leg == "bm25"
        assert block.selected == 1
        assert "本项目禁用 requests" in block.text
        assert "第 0 条无关内容" not in block.text
        # 没带上的条数照实报（recency 模式下这会是 3，这里是不相关的那些）
        assert block.extra == CORE_BUDGET + 3
        assert block.truncated is True

    def test_or_mode_matches_partial_overlap(self, mem: MemoryService):
        """and 模式问不出「结论是什么意思」，or 模式问得出 —— 注入要召回。"""
        mem.add("回复先给结论", kind="约定")
        block = mem.core_block(query="结论是什么意思")
        assert block.leg == "bm25"
        assert "回复先给结论" in block.text

    def test_miss_falls_back_to_recent_full_budget(self, mem: MemoryService):
        for i in range(CORE_BUDGET + 2):
            mem.add(f"条目{i}", kind="偏好")
        block = mem.core_block(query="你好")
        assert block.leg == "tail"
        assert block.selected == CORE_BUDGET
        assert block.extra == 2
        assert block.truncated is True

    def test_no_backfill_when_relevance_is_thin(self, mem: MemoryService):
        """只选中 1 条就注入 1 条 —— 不用近期条目凑满预算。"""
        mem.add("本项目禁用 requests", kind="约定")
        for i in range(5):
            mem.add(f"第 {i} 条无关内容", kind="偏好")
        block = mem.core_block(query="requests 能用吗", budget=CORE_BUDGET)
        assert block.selected == 1
        assert len([ln for ln in block.text.splitlines()
                    if ln.startswith("- ")]) == 1

    def test_role_scope_only_enters_with_role_ids(self, mem: MemoryService, roles: dict):
        mem.add(
            "本项目禁用 requests", kind="约定",
            scope=role_scope(roles["work"].id),
        )
        # 没给 role_ids：/ask 没有事务上下文，判不出命中角色
        block = mem.core_block(query="requests 能用吗")
        assert block.text == "", "没给 role_ids 却把角色作用域的注进来了"
        # 给了角色才进得去
        block2 = mem.core_block((roles["work"].id,), query="requests 能用吗")
        assert "本项目禁用 requests" in block2.text
        assert block2.leg == "bm25"

    def test_query_is_logged_with_its_leg(self, mem: MemoryService):
        mem.add("本项目禁用 requests", kind="约定")
        mem.core_block(query="requests 能用吗", consumer="/ask")
        row = self._last_log(mem)
        assert row["event"] == "inject"
        assert row["legs"] == "bm25"
        assert row["query"] == "requests 能用吗"
        assert row["hit_count"] == 1
        assert row["degraded"] == 0

    def test_miss_is_logged_as_tail_with_query(self, mem: MemoryService):
        mem.add("回复先给结论", kind="约定")
        mem.core_block(query="你好", consumer="/ask")
        row = self._last_log(mem)
        assert row["legs"] == "tail"
        assert row["query"] == "你好"

    def test_no_query_is_logged_as_recency(self, mem: MemoryService):
        mem.add("回复先给结论", kind="约定")
        mem.core_block(consumer="/ask")
        row = self._last_log(mem)
        assert row["legs"] == "recency"
        assert row["query"] == ""

    def test_blank_query_leaves_no_fake_search(self, mem: MemoryService):
        """切不出 token 的查询（空白）不是「搜过没命中」—— 别假装搜过。"""
        mem.add("回复先给结论", kind="约定")
        block = mem.core_block(query="   ")
        assert block.leg == "recency"
        row = self._last_log(mem)
        assert row["query"] == ""
        assert row["legs"] == "recency"

    def test_degraded_relevance_falls_back_and_says_so(
        self, mem: MemoryService, monkeypatch
    ):
        """FTS 不可用时：注入照常（按时间倒序），但痕迹必须留下。"""
        mem.add("回复先给结论", kind="约定")

        def _boom(*a, **kw):
            raise sqlite3.OperationalError("no such table: memory_fts")

        monkeypatch.setattr(mem, "_retrieve", _boom)
        block = mem.core_block(query="结论", consumer="/ask")
        assert block.leg == "recency"
        assert "回复先给结论" in block.text, "相关度坏了不该让注入消失"
        assert "相关度检索不可用" in block.degraded_reason
        row = self._last_log(mem)
        assert row["degraded"] == 1
        assert "memory_fts" in row["degraded_reason"]
        assert row["query"] == "结论"

    def test_budget_caps_relevance_selection(self, mem: MemoryService):
        for i in range(CORE_BUDGET + 3):
            mem.add(f"结论第 {i} 条", kind="约定")
        block = mem.core_block(query="结论", budget=CORE_BUDGET)
        assert block.leg == "bm25"
        assert block.selected == CORE_BUDGET
        assert block.extra == 3


# -- 7. review 队列（V1.100 / 3.7.8）------------------------------------------- #

class TestMemoryReview:
    """``/memory review``：候选口径、确认动作、相近对子。

    四条纪律（每条都有实测撑着）：

    - 候选 = 窗口内**什么事都没发生过**（没检索 / 没注入 / 没确认，
      连写下来都超过窗口）—— 刚记下的记忆没有罪；
    - **只摆不动**：没有自动删、没有自动 consolidation，动作只有人发起的
      confirm 与 del；
    - 确认**不碰内容**（「我看过它」≠「我改了它」），且必须落盘
      （V1.98 的 commit 教训，每个新写入口都要再付一次学费）；
    - 读不出来（留痕 / 确认记录）时**不摆候选**—— 把故障显示成
      「没有候选」比崩溃更糟（用户会以为刚整理过）。
    """

    def test_stale_entry_is_candidate(self, mem: MemoryService, clock):
        mem.add("我不喜欢早上开会", kind="偏好")
        clock.advance(days=91)
        report = mem.review_candidates(days=90)
        assert [r.entry.content for r in report.candidates] == ["我不喜欢早上开会"]
        assert report.total == 1
        assert report.near_duplicates == ()

    def test_fresh_entry_is_not_candidate(self, mem: MemoryService):
        """刚记下的记忆没有罪 —— 「没用过」不是进队列的理由。"""
        mem.add("我不喜欢早上开会", kind="偏好")
        assert mem.review_candidates(days=90).candidates == ()

    def test_used_entry_is_not_candidate(self, mem: MemoryService, clock):
        mem.add("回复先给结论", kind="约定")
        clock.advance(days=91)
        mem.retrieve("结论")     # 留痕发生在「现在」
        report = mem.review_candidates(days=90)
        assert report.candidates == ()
        assert report.total == 1

    def test_stale_row_remembers_last_use(self, mem: MemoryService, clock):
        """用过又超窗口 ≠ 从未用过 —— 事实要摆在候选里。"""
        mem.add("回复先给结论", kind="约定")
        mem.retrieve("结论")
        clock.advance(days=91)
        row = mem.review_candidates(days=90).candidates[0]
        assert row.retrievals == 0, "窗口外的检索不计进窗口计数"
        assert row.last_event_at is not None, "时间取全时段，不随窗口消失"
        assert row.last_attention_at == row.last_event_at

    def test_confirm_removes_candidate(self, mem: MemoryService, clock):
        e = mem.add("我不喜欢早上开会", kind="偏好")
        clock.advance(days=91)
        mem.confirm(e.id)
        report = mem.review_candidates(days=90)
        assert report.candidates == ()
        assert report.total == 1

    def test_confirm_is_not_permanent_silence(self, mem: MemoryService, clock):
        """确认只是退出一个窗口 —— 静默地永远不再出现正是要避开的事。"""
        e = mem.add("我不喜欢早上开会", kind="偏好")
        clock.advance(days=91)
        mem.confirm(e.id)
        assert mem.review_candidates(days=90).candidates == ()
        clock.advance(days=91)
        assert [r.entry.id for r in mem.review_candidates(days=90).candidates] == [e.id]

    def test_confirm_missing_entry_returns_none(self, mem: MemoryService):
        assert mem.confirm("no-such-id") is None

    def test_confirm_does_not_touch_updated_at(self, mem: MemoryService):
        """「我看过它，它还作数」≠「我改了它」—— 两件事不能记在同一列上。"""
        e = mem.add("我不喜欢早上开会", kind="偏好")
        mem.confirm(e.id)
        again = mem.get(e.id)
        assert again is not None
        assert again.updated_at == e.created_at
        assert again.content == e.content

    def test_confirm_survives_restart(self, tmp_path):
        """新写入口必须 commit —— V1.98 的教训原样适用。"""
        from freeagent.services.clock import SystemClock

        app = build_app(
            tmp_path / "agent.db", clock=SystemClock(),
            energy_windows=EnergyWindows(),
        )
        entry = app.memory.add("我不喜欢早上开会", kind="偏好")
        app.memory.confirm(entry.id)
        app.close()

        app2 = build_app(
            tmp_path / "agent.db", clock=SystemClock(),
            energy_windows=EnergyWindows(),
        )
        try:
            n = app2.memory._conn.execute(
                "SELECT COUNT(*) FROM memory_reviews"
            ).fetchone()[0]
            assert n == 1, "confirm 没落盘 —— 新写入口忘了 commit（V1.98 同款 bug）"
        finally:
            app2.close()

    def test_near_duplicate_pair_is_surfaced(self, mem: MemoryService):
        mem.add("回复先给结论", kind="约定")
        mem.add("回复先给结论，再给细节", kind="约定")
        pairs = mem.review_candidates().near_duplicates
        assert len(pairs) == 1
        pair = pairs[0]
        assert {pair.left.content, pair.right.content} == {
            "回复先给结论", "回复先给结论，再给细节"
        }
        assert pair.similarity >= NEAR_DUPLICATE_THRESHOLD

    def test_exact_duplicate_scores_one(self, mem: MemoryService):
        mem.add("我不吃香菜", kind="偏好")
        mem.add("我不吃香菜", kind="约定")
        pairs = mem.review_candidates().near_duplicates
        assert len(pairs) == 1
        assert pairs[0].similarity == 1.0

    def test_same_prefix_opposite_conclusion_is_not_paired(self, mem: MemoryService):
        """「回复先给结论」vs「回复先给细节」：Jaccard 0.43，低于阈值。

        **宁可漏判**（V1.96 同一条纪律）—— 这恰恰是最像矛盾的一类，
        而不引模型轮就分不出「矛盾」与「无关」（3.7.5 的理由）。漏判的
        代价是没摆出来，人还能 /memory search 找到；错判的代价是队列里
        出现一对其实无关的记忆 —— 队列的信誉由「摆出来的基本都对」撑着。
        """
        mem.add("回复先给结论", kind="约定")
        mem.add("回复先给细节", kind="约定")
        assert mem.review_candidates().near_duplicates == ()

    def test_unrelated_entries_are_not_paired(self, mem: MemoryService):
        mem.add("我不喜欢早上开会", kind="偏好")
        mem.add("本项目禁用 requests", kind="约定")
        assert mem.review_candidates().near_duplicates == ()

    def test_single_char_entries_do_not_crash(self, mem: MemoryService):
        """单字条目切不出二元组 —— 判不了相近，不硬判也不崩。"""
        mem.add("甲", kind="偏好")
        mem.add("乙", kind="偏好")
        assert mem.review_candidates().near_duplicates == ()

    def test_candidates_oldest_first(self, mem: MemoryService, clock):
        """整理从最被忽视的开始。"""
        older = mem.add("更旧的一条", kind="偏好")
        clock.advance(days=30)
        newer = mem.add("旧的一条", kind="偏好")
        clock.advance(days=100)
        report = mem.review_candidates(days=90)
        assert [r.entry.id for r in report.candidates] == [older.id, newer.id]

    def test_window_is_configurable(self, mem: MemoryService, clock):
        mem.add("我不喜欢早上开会", kind="偏好")
        clock.advance(days=31)
        assert mem.review_candidates(days=60).candidates == ()
        assert len(mem.review_candidates(days=30).candidates) == 1

    def test_default_window_matches_usage_stats(self, mem: MemoryService):
        """两个默认窗口同一个量级 —— 都是「一个季度」的追问间隔。"""
        assert DEFAULT_REVIEW_DAYS == DEFAULT_USAGE_DAYS

    def test_unreadable_log_reports_degraded_no_candidates(
        self, mem: MemoryService, clock
    ):
        """留痕读不出来时不摆候选 —— 把故障显示成「没有候选」比崩溃更糟。"""
        mem.add("我不喜欢早上开会", kind="偏好")
        clock.advance(days=91)
        conn = mem._conn
        conn.execute("ALTER TABLE retrieval_log RENAME TO retrieval_log_bak")
        conn.commit()
        try:
            report = mem.review_candidates(days=90)
            assert report.candidates == ()
            assert report.degraded_reason
        finally:
            conn.execute("DROP TABLE IF EXISTS retrieval_log_bak")
            conn.commit()

    def test_unreadable_confirmations_report_degraded(
        self, mem: MemoryService, clock
    ):
        """确认记录读不出来 → 「确认过」会被显示成「没确认过」，必须自报。"""
        mem.add("我不喜欢早上开会", kind="偏好")
        clock.advance(days=91)
        conn = mem._conn
        conn.execute("ALTER TABLE memory_reviews RENAME TO memory_reviews_bak")
        conn.commit()
        try:
            report = mem.review_candidates(days=90)
            assert report.candidates == ()
            assert "确认记录" in report.degraded_reason
        finally:
            conn.execute("DROP TABLE IF EXISTS memory_reviews_bak")
            conn.commit()
