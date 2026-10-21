"""记忆（设计文档 3.7 / V1.47）的测试。

分四组：
1. 值语义与写入校验（kind / scope / 空内容 / 孤儿角色）；
2. 检索（两条腿 + 来源自报 + 降级）；
3. 编辑与删除（用户可改可删是硬要求）；
4. core 注入（预算、定性、超预算不静默）。

刻意**不改** knowledge 的测试 —— 两层的检索机制共用，但归属与作用域不同，
断言必须落在本层自己的行为上，而不是「共用就没问题」。
"""

from __future__ import annotations

import io

import pytest

from freeagent.app import App, build_app
from freeagent.services.memory import (
    CORE_BUDGET,
    GLOBAL_SCOPE,
    MEMORY_KINDS,
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


def test_kinds_are_three(mem):
    assert MEMORY_KINDS == frozenset({"偏好", "约定", "修正"})


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
    text, truncated, extra = mem.core_block()
    assert text == ""
    assert truncated is False
    assert extra == 0


def test_core_block_has_qualifier(mem: MemoryService):
    mem.add("回复先给结论", kind="约定")
    text, _, _ = mem.core_block()
    assert "回复先给结论" in text
    # 定性必须有：记忆不等于事实。
    assert "可能过期" in text
    assert "以当前为准" in text


def test_core_block_includes_role(mem: MemoryService, roles: dict):
    mem.add("全局的", kind="偏好")
    mem.add("工作的", kind="约定", scope=role_scope(roles["work"].id))
    text, _, _ = mem.core_block((roles["work"].id,))
    assert "全局的" in text
    assert "工作的" in text


def test_core_block_excludes_other_role(mem: MemoryService, roles: dict):
    mem.add("家庭的", kind="偏好", scope=role_scope(roles["family"].id))
    text, _, _ = mem.core_block((roles["work"].id,))
    assert "家庭的" not in text


def test_core_block_budget_not_silent(mem: MemoryService):
    for i in range(CORE_BUDGET + 3):
        mem.add(f"条目{i}", kind="偏好")
    text, truncated, extra = mem.core_block(budget=CORE_BUDGET)
    assert truncated is True
    assert extra == 3
    assert len([1 for line in text.splitlines() if line.startswith("- ")]) == CORE_BUDGET


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
