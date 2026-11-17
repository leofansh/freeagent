"""上下文注入（21.5 / V1.115）：一行背景、三条纪律。

被守的三件事：

1. **只读**：注入不该顺延事务、不该在 ``task_records`` 留记录 ——
   那会让「它自己改了我的库」变成一件无从追溯的事；
2. **读不出来 ≠ 没有**：两件事在界面上必须是两句话；
3. **一行**：多行会被 opencode 当成复杂任务。

另外守「关掉之后说的是另一句话」—— 空串会被显示成「这一版没这个能力」。
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from freeagent.services.workbench_context import ContextBrief, build_context


class _Scored:
    def __init__(self, title: str) -> None:
        self.task = SimpleNamespace(title=title)


class _Memory:
    def __init__(self, contents: list[str], *, degraded: bool = False,
                 reason: str = "") -> None:
        self._contents = contents
        self.degraded = degraded
        self.degraded_reason = reason
        self.queries: list[str] = []

    def retrieve(self, query: str, *, scope=None, limit=3, consumer=""):
        self.queries.append(query)
        hits = [SimpleNamespace(entry=SimpleNamespace(content=c), via="bm25")
                for c in self._contents[:limit]]
        return SimpleNamespace(hits=tuple(hits), degraded=self.degraded,
                               degraded_reason=self.degraded_reason)


def _fake(*, tasks=(), promises=(), memories=(), memory=None,
          today_error: Exception | None = None,
          promise_error: Exception | None = None):
    def scheduled_for(day):  # noqa: ANN001
        if today_error is not None:
            raise today_error
        return [_Scored(t) for t in tasks]

    def open_promises():  # noqa: ANN001
        if promise_error is not None:
            raise promise_error
        return [SimpleNamespace(statement=s) for s in promises]

    return SimpleNamespace(
        today=SimpleNamespace(scheduled_for=scheduled_for),
        clock=SimpleNamespace(today=lambda: SimpleNamespace(
            isoformat=lambda: "2026-11-16")),
        promise=SimpleNamespace(open_promises=open_promises),
        memory=memory or _Memory(list(memories)),
    )


class TestShape:
    def test_nothing_to_carry_says_so(self) -> None:
        brief = build_context(_fake(), "改一下 app.py")
        assert isinstance(brief, ContextBrief)
        assert brief.text == ""
        assert brief.note == "这一轮没有可带的上下文"
        assert brief.ok is True

    def test_it_is_always_one_line(self) -> None:
        # 多行会让 opencode 升级成它自己的复杂任务 —— 这是硬约束。
        brief = build_context(_fake(tasks=("写周报", "回王工邮件"),
                                    promises=("周四给甲方发样件",),
                                    memories=("回复先给结论",)), "做点事")
        assert "\n" not in brief.text
        assert "\n" not in brief.note

    def test_counts_are_reported_next_to_the_text(self) -> None:
        brief = build_context(_fake(tasks=("写周报",), memories=("回复先给结论",)),
                              "做点事")
        assert brief.text.startswith("今天的活：写周报")
        assert "今天的活 1 条" in brief.note
        assert "记忆 1 条" in brief.note

    def test_project_note_carried_first_and_counted(self) -> None:
        # 备注是站立偏好，排在今天的活前面；换行 / 多段空格压成单行。
        brief = build_context(_fake(tasks=("写周报",)), "做点事",
                              project_note="先给结论\n再展开　不写废话")
        assert brief.text.startswith("项目备注：先给结论 再展开 不写废话")
        assert "项目备注 1 段" in brief.note

    def test_empty_project_note_is_absent(self) -> None:
        brief = build_context(_fake(), "做点事", project_note="   ")
        assert brief.text == ""
        assert "项目备注" not in brief.note

    def test_limits_are_respected(self) -> None:
        brief = build_context(
            _fake(tasks=tuple(f"事{i}" for i in range(6)),
                  promises=tuple(f"承诺{i}" for i in range(5))),
            "做点事")
        assert "事3" not in brief.text, "每类都有上限，不能把一整天的活都塞进去"
        assert "承诺3" not in brief.text

    def test_overlong_context_says_it_was_truncated(self) -> None:
        brief = build_context(_fake(tasks=("很长很长" * 60,)), "做点事",
                              limit_chars=50)
        assert len(brief.text) <= 50
        assert "已截断" in brief.note, "静默截断等于隐瞒"


class TestFailureIsNotEmptiness:
    def test_a_broken_section_is_named_not_swallowed(self) -> None:
        brief = build_context(_fake(today_error=RuntimeError("库锁住了")),
                              "做点事")
        assert brief.ok is False
        assert "没读到：今天的活" in brief.note
        assert "库锁住了" in brief.note
        assert brief.text == "", "一段读不出来，别把空说成「今天没事」"

    def test_other_sections_still_go_through(self) -> None:
        brief = build_context(
            _fake(today_error=RuntimeError("坏了"), memories=("回复先给结论",)),
            "做点事")
        assert "相关记忆" in brief.text
        assert "今天的活" in brief.note  # 报的是「没读到」，不是「没有」

    def test_degraded_retrieval_is_not_reported_as_no_memory(self) -> None:
        memory = _Memory([], degraded=True, reason="FTS5 表缺失")
        brief = build_context(_fake(memory=memory), "做点事")
        assert "检索不可用" in brief.note
        assert brief.text == ""

    def test_memories_are_searched_with_the_users_sentence(self) -> None:
        memory = _Memory(["回复先给结论"])
        build_context(_fake(memory=memory), "帮我写个周报")
        assert memory.queries == ["帮我写个周报"]


class TestRealApp:
    def test_it_reads_todays_tasks_without_writing(self, app) -> None:  # noqa: ANN001
        # 只读纪律：注入一遍之后，task_records 不该多出任何一行
        # （TodayService.view() 会顺延并留记录，所以这里不许调它）。
        role = app.roles.create("工作", note="")
        app.tasks.create("写周报", [role.id],
                         scheduled_for=app.clock.today())
        before = app.conn.execute("SELECT COUNT(*) FROM task_records").fetchone()[0]
        brief = build_context(app, "改一下 app.py")
        after = app.conn.execute("SELECT COUNT(*) FROM task_records").fetchone()[0]
        assert before == after, "注入上下文不许偷偷往库里写记录"
        assert "写周报" in brief.text
