"""对话入口：**能问**关于你自己数据的问题。

为什么这组测试存在
------------------
上一轮把问句一律拒答了，理由是防数据污染。但那样用户就没法「问」，
而「用了才知道好不好用」恰恰要求能问。这组测试锁住新边界：

* 问 → **读你的数据并回答**（不建任何东西）
* 记事 → 建一条
* 要改已有事务 → **如实说做不到**，并指出该用哪个命令

最后一条是刻意的：会改数据的入口必须有确定的边界，
所以这轮**不**加自然语言改操作。
"""

from __future__ import annotations

import pytest

from freeagent.app import build_app
from freeagent.domain import TaskKind, WaitingKind, WaitingOn
from freeagent.services.chat import CAN_DO, ChatReplyKind
from freeagent.services.clock import FrozenClock

#: 2026-09-26 是周六，本周一是 09-21。
NOW = "2026-09-26 14:00"


@pytest.fixture
def chat(tmp_path):
    clock = FrozenClock.__new__(FrozenClock)
    from datetime import datetime

    clock = FrozenClock(datetime(2026, 9, 26, 14, 0))
    app = build_app(tmp_path / "a.db", clock=clock)

    work = app.roles.create(
        "工作项目A", note="销售 周报 客户",
        default_definition_of_done="先给我能用的就行",
    )
    family = app.roles.create("家庭", note="孩子 学校 材料")

    report = app.tasks.create("销售周报初稿", [work.id], intent="给老板看的一页纸")
    app.tasks.schedule(report.id, clock.today())
    app.tasks.start(report.id)
    app.tasks.note(report.id, "数据拉完了")

    waiting = app.tasks.create(
        "等客户法务回复合同条款", [work.id], kind=TaskKind.WAIT,
        waiting_on=WaitingOn(
            WaitingKind.PERSON, "客户法务", datetime(2026, 9, 22, 9, 0)
        ),
    )
    app.tasks.schedule(waiting.id, clock.today())
    app.tasks.set_followup(waiting.id, datetime(2026, 9, 24, 10, 0))

    fix = app.tasks.create(
        "修窗户螺丝", [family.id], kind=TaskKind.REMINDER,
        reminder_time=datetime(2026, 9, 26, 18, 0),
    )
    app.tasks.schedule(fix.id, clock.today())

    # 上周完成一条
    clock.set(datetime(2026, 9, 18, 12, 0))
    done = app.tasks.create("报销打车费", [work.id])
    app.tasks.complete(done.id)
    clock.set(datetime(2026, 9, 26, 14, 0))

    # 给周报加一版草稿并采纳。
    # 这条**必须在这里**：之前测试数据里没有草稿，导致「问带稿事务的进展」
    # 那条分支从未被执行 —— 直到演示库里有稿才 500。
    app.artifacts.create_draft(report.id, "销售周报初稿 v1", "# 周报\n[TODO]")
    app.artifacts.accept(
        app.artifacts.create_draft(
            report.id, "销售周报初稿 v2", "# 周报\n结论：[TODO]"
        ).id
    )

    return app


# =============================================================================
# 提问：全部要答出真数据
# =============================================================================
class TestQuestionsGetRealAnswers:
    """这些是我**真实问过**的话，修复前它们全都建出了垃圾数据。"""

    @pytest.mark.parametrize("text", [
        "今天要做什么",
        "我现在该先做哪件",
        "这条为什么排第一",
        "我上周完成了什么",
        "我在等什么",
        "有什么提醒",
        "周报进展怎么样",
    ])
    def test_never_refused_and_never_creates(self, chat, text):
        before_tasks = len(chat.tasks.list_all())
        before_roles = len(chat.roles.list_roles(include_inactive=True))

        reply = chat.chat.respond(text)

        assert reply.kind is ChatReplyKind.ANSWER, f"被拒绝了：{reply.kind.value} {reply.text[:40]}"
        assert reply.text.strip(), "回复不能是空的"
        assert len(chat.tasks.list_all()) == before_tasks, "提问建出了事务"
        assert len(chat.roles.list_roles(include_inactive=True)) == before_roles, (
            "提问建出了角色"
        )

    def test_today_lists_things_with_reasons(self, chat):
        reply = chat.chat.respond("今天要做什么")
        assert "等客户法务回复合同条款" in reply.text
        assert reply.items, "应带结构化事务供界面渲染"
        assert reply.items[0].reasons, "每条都要带信号理由（设计契约）"
        assert "启发式提示" in reply.text, "必须保留免责标记"

    def test_why_explains_signals(self, chat):
        reply = chat.chat.respond("这条为什么排第一")
        assert "该催" in reply.text or "跟进日已过" in reply.text
        assert "启发式提示" in reply.text

    def test_progress_finds_task_by_fuzzy_title(self, chat):
        reply = chat.chat.respond("周报进展怎么样")
        assert reply.kind is ChatReplyKind.ANSWER
        assert "销售周报初稿" in reply.text
        assert reply.items and reply.items[0].task_id

    def test_progress_includes_draft_and_dod(self, chat):
        """恢复契约的要素在对话回答里也要齐。

        回归两处：
        1. ``ArtifactStatus`` 没有 ``.label``，之前这里直接 500，
           而测试数据里没有草稿所以从没跑到。
        2. 事务自己没有完成标准，要能回落到**角色**的默认完成标准。
        """
        reply = chat.chat.respond("周报进展怎么样")
        assert "当前稿" in reply.text
        assert "已采纳" in reply.text, "要给出草稿状态的中文标签"
        assert "先给我能用的就行" in reply.text, "应回落到角色级完成标准"
        assert "下一步" in reply.text

    def test_last_week_completed(self, chat):
        reply = chat.chat.respond("我上周完成了什么")
        assert "报销打车费" in reply.text

    def test_this_week_does_not_leak_last_week(self, chat):
        """时间范围不能糊：本周的完成不该被算进「上周」。"""
        reply = chat.chat.respond("我本周完成了什么")
        assert "报销打车费" not in reply.text

    def test_waiting_reasons_mention_followup(self, chat):
        reply = chat.chat.respond("我在等什么")
        assert "客户法务" in reply.text
        assert "该催" in reply.text, "跟进日已过就该说明"

    def test_role_scoped_answer(self, chat):
        reply = chat.chat.respond("家庭那边有什么")
        assert reply.kind is ChatReplyKind.ANSWER, "示例句同时是真问题，不该回使用说明"
        assert "修窗户螺丝" in reply.text
        assert "周报" not in reply.text, "角色范围要真的生效"

    def test_suggestions_are_clickable(self, chat):
        reply = chat.chat.respond("今天要做什么")
        assert reply.suggestions, "应给可点的后续问题"


# =============================================================================
# 认不准就反问，不猜
# =============================================================================
class TestDoesNotGuess:
    def test_ambiguous_progress_asks_which(self, chat):
        """两条都含「周报」，只说「周报」时必须反问 —— 挑一条答是错的。"""
        work = chat.roles.get_by_name("工作项目A")
        chat.tasks.create("整理周报素材", [work.id])
        chat.tasks.create("整理周报数据", [work.id])
        before = len(chat.tasks.list_all())
        reply = chat.chat.respond("周报进展")
        assert "哪一条" in reply.text or "今天排着的是" in reply.text
        assert len(chat.tasks.list_all()) == before, "反问不该建东西"

    def test_uniquely_named_one_is_answered(self, chat):
        """说得出唯一一条就该答，不必反问 —— 反问太多同样没用。"""
        work = chat.roles.get_by_name("工作项目A")
        chat.tasks.create("整理周报素材", [work.id])
        chat.tasks.create("整理周报数据", [work.id])
        reply = chat.chat.respond("周报素材进展")
        assert reply.kind is ChatReplyKind.ANSWER
        assert "整理周报素材" in reply.text
        assert "整理周报数据" not in reply.text, "不该顺带把另一条也答出来"

    def test_unknown_topic_asks_which(self, chat):
        before = len(chat.tasks.list_all())
        reply = chat.chat.respond("那个项目进展怎么样")
        assert "？" in reply.text or "哪一条" in reply.text
        assert len(chat.tasks.list_all()) == before


# =============================================================================
# 记事
# =============================================================================
class TestRecording:
    def test_records_plain_sentence(self, chat):
        before = len(chat.tasks.list_all())
        reply = chat.chat.respond("下周二要交的材料清单")
        assert reply.kind is ChatReplyKind.RECORDED
        assert len(chat.tasks.list_all()) == before + 1
        task = chat.tasks.list_all()[-1]
        assert "材料清单" in task.title
        assert task.scheduled_for is not None, "「下周二」应被解析"

    def test_asks_which_role_when_unclear(self, chat):
        before = len(chat.tasks.list_all())
        reply = chat.chat.respond("qqqqzzz完全不相关的一件事")
        assert reply.kind is ChatReplyKind.CLARIFY
        assert len(chat.tasks.list_all()) == before, "追问时不该建事务"
        assert reply.suggestions, "应给出可选的脉络"


class TestMultiTurn:
    """多轮：**上一轮给了什么，这一轮就能接着问哪一条**。

    服务端不存对话状态，由客户端回传上一轮的 id 列表（``context_ids``）。
    这样做的好处是服务端无状态 —— 重启不丢、好测、也不会因为两个窗口
    互相污染而答错对象。
    """

    def _first_answer_ids(self, chat):
        reply = chat.chat.respond("今天要做什么")
        assert reply.items, "第一轮得给出结构化条目才谈得上接上下文"
        return [i.task_id for i in reply.items]

    def test_ordinal_picks_the_right_one(self, chat):
        ids = self._first_answer_ids(chat)
        reply = chat.chat.respond("第二个", context_ids=ids)
        assert reply.kind is ChatReplyKind.ANSWER
        assert reply.items and reply.items[0].task_id == ids[1]

    def test_ordinal_supports_chinese_numerals(self, chat):
        ids = self._first_answer_ids(chat)
        for text, index in (("第一条", 0), ("第三条", 2), ("第2条", 1)):
            reply = chat.chat.respond(text, context_ids=ids)
            assert reply.items[0].task_id == ids[index], text

    def test_pronoun_refers_to_first(self, chat):
        ids = self._first_answer_ids(chat)
        for text in ("它", "这条", "那个"):
            reply = chat.chat.respond(text, context_ids=ids)
            assert reply.items[0].task_id == ids[0], text

    def test_ordinal_out_of_range_does_not_guess(self, chat):
        ids = self._first_answer_ids(chat)
        reply = chat.chat.respond("第99个", context_ids=ids)
        assert not (reply.items and reply.items[0].task_id in ids[10:]), (
            "越界时不能瞎指一条"
        )

    def test_pronoun_without_context_is_not_a_reference(self, chat):
        """没有上文时「它」是胡说 —— 不能硬指一条。"""
        before = len(chat.tasks.list_all())
        reply = chat.chat.respond("它", context_ids=[])
        assert reply.kind is not ChatReplyKind.RECORDED, "不能把「它」当成一件事记下"
        assert len(chat.tasks.list_all()) == before

    def test_ordinal_reaches_progress(self, chat):
        """「第二个」后面接进展类追问要能接上。"""
        ids = self._first_answer_ids(chat)
        reply = chat.chat.respond("第二个呢", context_ids=ids)
        assert reply.kind is ChatReplyKind.ANSWER
        assert "状态" in reply.text or "进度" in reply.text

    def test_why_after_ordinal(self, chat):
        ids = self._first_answer_ids(chat)
        reply = chat.chat.respond("为什么这条排第一", context_ids=ids)
        assert "启发式提示" in reply.text

    def test_unknown_id_in_context_is_ignored(self, chat):
        """上下文里的 id 可能已被删除 —— 不能因此崩。"""
        reply = chat.chat.respond("第二个", context_ids=["deadbeef", "cafe1234"])
        assert reply.kind is not None


class TestMultiTurnKeepsSafetyBoundary:
    """多轮**不能**变成越权的入口。

    这是最该盯住的一条：有了上下文之后，用户很容易顺势说
    「把它标完成」「把它删了」——如果这时真改了数据，
    上一轮堵住的坑就白堵了。
    """

    @pytest.mark.parametrize("text", [
        "把它标完成",
        "把它删了",
        "把它改到下周三",
        "第一个改个时间",
    ])
    def test_mutate_after_listing_is_refused(self, chat, text):
        ids = [i.task_id for i in chat.chat.respond("今天要做什么").items]
        before = chat.tasks.list_all()
        snapshot = [(t.id, t.state, t.scheduled_for) for t in before]

        reply = chat.chat.respond(text, context_ids=ids)

        after = [(t.id, t.state, t.scheduled_for) for t in chat.tasks.list_all()]
        assert after == snapshot, f"多轮下真的改了数据：{text}"
        assert reply.kind in (ChatReplyKind.CANNOT, ChatReplyKind.ANSWER), (
            f"应拒绝或转成只读回答，拿到 {reply.kind.value}"
        )

    def test_recording_after_listing_still_works(self, chat):
        """别把正常记事也堵了 —— 有上下文不代表不能记新的。"""
        chat.chat.respond("今天要做什么")
        before = len(chat.tasks.list_all())
        reply = chat.chat.respond("下周二要交的材料清单")
        assert reply.kind in (ChatReplyKind.RECORDED, ChatReplyKind.CLARIFY)
        if reply.kind is ChatReplyKind.RECORDED:
            assert len(chat.tasks.list_all()) == before + 1


# =============================================================================
# 边界：不改已有事务
# =============================================================================
class TestRefusesToMutate:
    @pytest.mark.parametrize("text", [
        "把周报改到下周三",
        "删掉那条旧提醒",
        "把会议推迟到明天",
    ])
    def test_mutate_request_changes_nothing(self, chat, text):
        before = chat.tasks.list_all()
        reply = chat.chat.respond(text)
        assert reply.kind is ChatReplyKind.CANNOT
        assert "做不到" in reply.text
        after = chat.tasks.list_all()
        assert len(after) == len(before)
        for a, b in zip(before, after):
            assert a.state == b.state, "状态被改了"
            assert a.scheduled_for == b.scheduled_for, "排期被改了"


# =============================================================================
# 开场白：告诉用户能问什么
# =============================================================================
class TestOpening:
    def test_help_lists_can_do(self, chat):
        reply = chat.chat.respond("你能做什么")
        assert reply.kind is ChatReplyKind.HELP
        for question in CAN_DO:
            assert question in reply.text

    def test_help_states_the_boundary(self, chat):
        """必须说清「不改已有事务」，否则用户会以为助手全能。"""
        assert "不改已有事务" in chat.chat.respond("你能做什么").text

    def test_blank_input_is_polite(self, chat):
        reply = chat.chat.respond("   ")
        assert reply.kind is ChatReplyKind.HELP
        assert reply.suggestions


class TestNoMarkdownLeaksToUser:
    """气泡是**纯文本**渲染的，markdown 记号会原样显示出来。

    真出过这个 bug：帮助文案里写了 ``但**不改已有事务**``，
    用户在界面上看到的就是带星号的字面量。docstring 里的 ``**`` 是
    正常的（不显示给用户），所以这里只查回复文本。
    """

    @pytest.mark.parametrize("text", [
        "你能做什么", "今天该做什么", "我在等什么", "有什么提醒",
        "我上周完成了什么", "家庭那边有什么", "周报进展怎么样",
        "为什么这条排第一", "把周报改到下周三", "下周二交周报",
    ])
    def test_reply_text_has_no_markdown(self, chat, text):
        reply = chat.chat.respond(text)
        for marker in ("**", "##", "](", "`"):
            assert marker not in reply.text, (
                f"回复里有 markdown 记号 {marker!r}，会被原样显示：{reply.text[:60]}"
            )

    def test_openings_have_no_markdown(self):
        from freeagent.web.server import WebRequestHandler  # noqa: F401
        from freeagent.services.chat import CAN_DO

        for question in CAN_DO:
            assert "**" not in question, f"示例问题带星号：{question}"
