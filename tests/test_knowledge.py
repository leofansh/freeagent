"""角色知识：可检索的脉络知识 + 自报来源。

## 这组测试要守住两件事

1. **中文 2 字词能搜到。** 这不是小事：FTS5 的内置分词器对中文**不可用**
   （实测 ``unicode61`` 整串当一个 token，``trigram`` 查不了 2 字词），
   必须自己切二元组。忘了这件事的表现是「搜不到」，而不是报错 ——
   建表成功、写入成功、英文查询正常，只有中文短查询静默失效。
2. **检索结果自带来源。** 只列结果不列来源，等于要求用户盲信；静默截断
   等于隐瞒；把「近期回落」混进「关键词命中」等于让用户自己猜。
"""

from __future__ import annotations

import pytest

from freeagent.app import App
from freeagent.services.knowledge import (
    DEFAULT_LIMIT,
    KINDS,
    TAIL_LIMIT,
    VECTOR_UNAVAILABLE,
    index_text,
)


class TestIndexText:
    """分词是纯函数且无 IO —— 它坏了不会报错，只会让搜索静默失效。"""

    def test_cjk_becomes_overlapping_bigrams(self):
        assert index_text("周报") == "周报"
        assert index_text("周报口径") == "周报 报口 口径"

    def test_two_char_query_is_a_single_bigram(self):
        """中文查询大量是 2 字词 —— 整串当一个 token 就全都搜不到。"""
        assert index_text("王工") == "王工"
        assert index_text("客户") == "客户"

    def test_single_cjk_char_is_kept_whole(self):
        """长度 1 切不出二元组，原样保留。"""
        assert index_text("修") == "修"

    def test_non_cjk_tokens_are_kept_whole(self):
        """数字与拉丁词本来就有词边界，切开反而查不准。"""
        assert index_text("SLA 99.9") == "SLA 99.9"

    def test_mixed_content_splits_by_script(self):
        got = index_text("修 a1b2 窗户")
        assert "修" in got and "a1b2" in got and "窗户" in got


class TestWrite:
    def test_add_and_read_back(self, app: App, roles: dict):
        role = roles["work"]
        entry = app.knowledge.add(role.id, "口径", "周报口径：先给结论再给数据")
        assert entry.kind == "口径"
        assert entry.content == "周报口径：先给结论再给数据"
        listed = app.knowledge.list_for_role(role.id)
        assert [e.id for e in listed] == [entry.id]

    def test_content_is_trimmed(self, app: App, roles: dict):
        entry = app.knowledge.add(roles["work"].id, "偏好", "  王工只认邮件  ")
        assert entry.content == "王工只认邮件"

    def test_blank_content_is_rejected(self, app: App, roles: dict):
        with pytest.raises(ValueError, match="不能为空"):
            app.knowledge.add(roles["work"].id, "偏好", "   ")

    def test_unknown_kind_is_rejected(self, app: App, roles: dict):
        """类别是给检索和呈现用的 —— 多出第四类会让两者都失去意义。"""
        with pytest.raises(ValueError, match="类别必须是"):
            app.knowledge.add(roles["work"].id, "随便", "x")

    @pytest.mark.parametrize("kind", sorted(KINDS))
    def test_all_three_kinds_are_accepted(self, app: App, roles: dict, kind: str):
        assert app.knowledge.add(roles["work"].id, kind, "内容").kind == kind

    def test_nonexistent_role_is_rejected(self, app: App):
        """否则会记下一条永远不会被检索到、也永远不会被看见的孤儿知识。"""
        from freeagent.domain import NotFoundError

        with pytest.raises(NotFoundError):
            app.knowledge.add("no-such-role", "偏好", "x")

    def test_source_ref_is_optional(self, app: App, roles: dict):
        assert app.knowledge.add(
            roles["work"].id, "历史决策", "x", source_ref="邮件 2026-09-20"
        ).source_ref == "邮件 2026-09-20"
        assert app.knowledge.add(roles["work"].id, "历史决策", "y").source_ref is None


class TestRetrieveChineseShortWords:
    """这一组是整个特性的命门。"""

    @pytest.mark.parametrize(
        "query, text",
        [
            ("周报", "周报口径：先给结论再给数据"),
            ("王工", "王工只认邮件，飞书不收"),
            ("邮件", "客户偏好邮件沟通"),
            ("口径", "周报口径：先给结论再给数据"),
            ("飞书", "王工只认邮件，飞书不收"),
        ],
    )
    def test_two_char_query_finds_its_entry(
        self, app: App, roles: dict, query: str, text: str
    ):
        role = roles["work"]
        app.knowledge.add(role.id, "口径", text)
        result = app.knowledge.retrieve(role.id, query)
        assert result.bm25_hits == 1, f"{query!r} 应当命中（2 字词）"
        assert result.hits[0].entry.content == text
        assert result.hits[0].via == "bm25"
        assert result.hits[0].score is not None

    def test_multi_char_query_works(self, app: App, roles: dict):
        role = roles["work"]
        app.knowledge.add(role.id, "历史决策", "销售周报初稿下周三交")
        assert app.knowledge.retrieve(role.id, "销售周报初稿").bm25_hits == 1

    def test_space_separated_bigrams_are_anded(self, app: App, roles: dict):
        """**实测记录 FTS5 的 AND 语义**，免得日后有人当 bug 改掉。

        ``index_text()`` 把查询切成空格分隔的多个二元组，而 FTS5 对空格
        分隔的词按 **AND** 处理 —— 所有二元组都必须出现才命中。

        实测：``周报报告``（周报 + 报告）在只有「周报口径」和「周报和月度报告」
        的库里 **0 命中**，因为没有哪条同时含这两个二元组。

        推论（与直觉相反，值得写下来）：**长查询比短查询严格**。
        5 个二元组的查询只有全部命中才算，而 1 个二元组的查询（2 字词）
        命中范围很宽。所以「粗」的是短查询，不是长查询 ——
        2 字词能搜到是刻意换来的（内置分词器做不到），代价就是精度更松。
        """
        role = roles["work"]
        app.knowledge.add(role.id, "口径", "周报口径：先给结论再给数据")
        app.knowledge.add(role.id, "口径", "周报和月度报告都要给王工")
        # 两个二元组，没有一条同时含两者
        assert app.knowledge.retrieve(role.id, "周报报告").bm25_hits == 0
        # 单个二元组，两条都命中
        assert app.knowledge.retrieve(role.id, "周报").bm25_hits == 2
        # 全部二元组都在的那条才命中
        app.knowledge.add(role.id, "历史决策", "周报报告下周三交")
        assert app.knowledge.retrieve(role.id, "周报报告").bm25_hits == 1

    def test_search_is_scoped_to_one_role(self, app: App, roles: dict):
        """知识是**角色的** —— 不能跨角色串。"""
        app.knowledge.add(roles["work"].id, "口径", "周报口径：先给结论")
        app.knowledge.add(roles["family"].id, "偏好", "周报要给孩子看")
        assert app.knowledge.retrieve(roles["work"].id, "周报").bm25_hits == 1
        assert app.knowledge.retrieve(roles["family"].id, "周报").bm25_hits == 1


class TestProvenance:
    """**自报来源**是这个类型存在的理由。"""

    def test_bm25_hit_reports_itself_as_keyword(self, app: App, roles: dict):
        app.knowledge.add(roles["work"].id, "口径", "周报口径：先给结论")
        hit = app.knowledge.retrieve(roles["work"].id, "周报").hits[0]
        assert hit.via == "bm25"
        assert hit.via_label == "关键词命中"
        assert hit.score is not None, "BM25 命中必须带分数"

    def test_miss_falls_back_to_tail_and_says_so(self, app: App, roles: dict):
        """「凭相关度找到的」和「只是最近写的」价值不同，混起来等于让用户猜。"""
        role = roles["work"]
        app.knowledge.add(role.id, "口径", "周报口径：先给结论")
        result = app.knowledge.retrieve(role.id, "完全无关的词")
        assert result.bm25_hits == 0
        assert result.tail_hits > 0
        assert all(h.via == "tail" for h in result.hits)
        assert all(h.score is None for h in result.hits), (
            "tail 回落没有分数 —— 给个假的 0.0 会让人以为它不相关"
        )
        assert "关键词没命中" in result.hits[0].via_label

    def test_vector_leg_is_explained_not_merely_absent(self, app: App, roles: dict):
        """"刻意不做" 与 "没命中" 必须能区分。"""
        app.knowledge.add(roles["work"].id, "口径", "周报口径：先给结论")
        result = app.knowledge.retrieve(roles["work"].id, "周报")
        assert result.vector_hits == 0
        assert result.vector_unavailable_reason == VECTOR_UNAVAILABLE
        assert "断网可用" in result.vector_unavailable_reason
        # 没有任何一条命中是「向量来的」
        assert all(h.via in ("bm25", "tail") for h in result.hits)

    def test_truncation_is_reported_not_hidden(self, app: App, roles: dict):
        """静默截断等于隐瞒。"""
        role = roles["work"]
        for i in range(8):
            app.knowledge.add(role.id, "历史决策", f"第{i}个决策的措辞")
        result = app.knowledge.retrieve(role.id, "措辞", limit=3)
        assert len(result.hits) == 3
        assert result.truncated is True
        assert "已截断" in result.describe_sources()

    def test_no_truncation_when_results_fit(self, app: App, roles: dict):
        app.knowledge.add(roles["work"].id, "口径", "周报口径：先给结论")
        assert app.knowledge.retrieve(roles["work"].id, "周报").truncated is False

    def test_describe_sources_always_says_something(self, app: App, roles: dict):
        """空结果也要有说法 —— 「搜过，没找到」与「压根没搜」不同。"""
        role = roles["work"]
        empty = app.knowledge.retrieve(role.id, "任何词")
        assert empty.describe_sources() == "搜过，没找到"

        app.knowledge.add(role.id, "口径", "周报口径：先给结论")
        assert "关键词命中 1 条" in app.knowledge.retrieve(role.id, "周报").describe_sources()

    def test_describe_sources_for_fallback(self, app: App, roles: dict):
        app.knowledge.add(roles["work"].id, "口径", "周报口径：先给结论")
        text = app.knowledge.retrieve(roles["work"].id, "无关").describe_sources()
        assert "近期条目回落" in text
        assert "关键词命中" not in text


class TestEmptyAndDegraded:
    def test_role_with_no_knowledge_is_empty_not_an_error(self, app: App, roles: dict):
        result = app.knowledge.retrieve(roles["errand"].id, "任何词")
        assert result.is_empty
        assert result.degraded is False, "没有知识不是故障"

    def test_blank_query_returns_empty_without_touching_fts(self, app: App, roles: dict):
        """空查询不去查 FTS —— 某些 MATCH 输入会抛，而空结果是合理答案。"""
        app.knowledge.add(roles["work"].id, "口径", "周报口径：先给结论")
        result = app.knowledge.retrieve(roles["work"].id, "   ")
        assert result.bm25_hits == 0
        assert result.degraded is False


class TestIndexStaysInSync:
    """FTS 索引由**触发器**同步 —— 验证时必须查**裸索引**，不能用 JOIN。

    踩过的坑（第一版验证就栽在这）：写成
    ``... FROM role_knowledge_fts f JOIN role_knowledge k ON k.rowid=f.rowid``，
    于是内容行删掉之后 JOIN 必然返回空 —— **触发器好与坏跑出来一模一样**。
    正确姿势是直接 ``SELECT rowid FROM role_knowledge_fts WHERE ... MATCH ?``。
    """

    @staticmethod
    def _raw_hits(app: App, query: str) -> list:
        rows = app.conn.execute(
            "SELECT rowid FROM role_knowledge_fts WHERE role_knowledge_fts MATCH ?",
            (index_text(query),),
        ).fetchall()
        return [r[0] for r in rows]

    def test_insert_syncs_the_index(self, app: App, roles: dict):
        app.knowledge.add(roles["work"].id, "口径", "周报口径：先给结论")
        assert self._raw_hits(app, "周报"), "插入后索引里应有这条"

    def test_delete_syncs_the_index(self, app: App, roles: dict):
        """这是最容易漏的一条：索引里删不掉的话，以后再搜还会命中。"""
        entry = app.knowledge.add(roles["work"].id, "口径", "周报口径：先给结论")
        assert self._raw_hits(app, "周报")
        assert app.knowledge.delete(entry.id) is True
        assert not self._raw_hits(app, "周报"), "删除后索引里不该残留"
        assert app.knowledge.retrieve(roles["work"].id, "周报").is_empty

    def test_delete_of_a_missing_entry_reports_false(self, app: App):
        assert app.knowledge.delete("no-such-id") is False

    def test_integrity_check_passes(self, app: App, roles: dict):
        """FTS5 自带的完整性检查 —— 索引与内容表必须一致。"""
        role = roles["work"]
        keep = app.knowledge.add(role.id, "口径", "保留的周报口径")
        gone = app.knowledge.add(role.id, "偏好", "删除的邮件偏好")
        app.knowledge.delete(gone.id)
        app.conn.execute(
            "INSERT INTO role_knowledge_fts(role_knowledge_fts) VALUES('integrity-check')"
        )
        assert self._raw_hits(app, "周报")
        assert not self._raw_hits(app, "邮件")
        assert keep.id


class TestListForRole:
    def test_lists_newest_first(self, app: App, roles: dict):
        from datetime import timedelta

        role = roles["work"]
        a = app.knowledge.add(role.id, "口径", "第一条")
        app.clock.set(app.clock.now() + timedelta(seconds=10))
        b = app.knowledge.add(role.id, "偏好", "第二条")
        assert [e.id for e in app.knowledge.list_for_role(role.id)] == [b.id, a.id]

    def test_limit_is_respected(self, app: App, roles: dict):
        role = roles["work"]
        for i in range(5):
            app.knowledge.add(role.id, "历史决策", f"第{i}条")
        assert len(app.knowledge.list_for_role(role.id, limit=2)) == 2


class TestTailBound:
    def test_tail_fallback_is_bounded(self, app: App, roles: dict):
        """回落不是「把所有条目都倒出来」 —— 那等于没有过滤。"""
        role = roles["work"]
        for i in range(TAIL_LIMIT + 4):
            app.knowledge.add(role.id, "历史决策", f"第{i}条无关内容")
        result = app.knowledge.retrieve(role.id, "毫不相干")
        assert result.tail_hits <= TAIL_LIMIT

    def test_default_limit_is_small(self):
        """角色知识是给人看的，不是给检索器看的。"""
        assert DEFAULT_LIMIT <= 10
