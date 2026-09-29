"""「没把握就反问，不许默认记」—— 守卫（设计文档 12.1.1）。

这条守卫的**来源是一次真实失败**：`你好` 被记成了事务，真库里因此有两条
（`0277e93d` inbox / `0fbbdb13` blocked）——**不是偶发**。

所以这里的判据不能是「我加的词表能挡住」，必须是「**真实的失败句**被挡住」。
:data:`REAL_FAILURES` 就是那三句原话，从截图里抄下来的。
"""
import pytest

from freeagent.services.llm.provider import (
    InputIntent,
    is_confidently_recordable,
    read_intent,
)
from freeagent.services.chat import ChatReplyKind

#: 真实对话里失败过的句子（2026-09-29 截图 + 真库证据）
REAL_FAILURES = [
    "你好",
    "你可以做什么？",
    "如何安排呢？",
    "ok",
    "在吗",
    "收到",
    "嗯",
]


class TestNoGarbageTasks:
    """"没被认出来"绝不能变成一条事务。"""

    @pytest.mark.parametrize("text", REAL_FAILURES[:1] + ["ok", "在吗", "收到", "嗯"])
    def test_not_confidently_recordable(self, text):
        assert is_confidently_recordable(text) is False, (
            f"{text!r} 不该被当成可记的事"
        )

    def test_hello_still_reads_as_record_intent(self):
        """⚠️ 刻意断言这个**仍然成立**。

        ``read_intent`` 的兜底**没有**改（仍是 RECORD）——
        改的是 :func:`is_confidently_recordable` 这道**前置**闸门。
        两条都改会让「意图判定」与「要不要记」混成一件事，
        而 12.1.1 要求分层：判定说「像要记」，闸门说「有把握吗」。

        所以这条测试的作用是**锁住分层**，防止有人「顺手」把 read_intent
        的兜底也改了 —— 那样看不出是哪一层在起作用。
        """
        assert read_intent("你好") is InputIntent.RECORD


class TestRealTasksStillGetRecorded:
    """收紧闸门**不能**把真任务也挡掉（这才是最容易出的错）。"""

    @pytest.mark.parametrize(
        "text",
        [
            "修窗户螺丝",                      # 动词开头（信号 1）
            "交周报",                          # 动词开头
            "下周二要交的销售周报初稿",        # 时间标记（信号 2）
            "别忘了提醒我下午三点修窗户螺丝",   # 时间标记
            "把冰箱里的牛奶喝掉然后买新的",     # 够长（信号 3）
            "整理一下这季度的报销单",           # 够长
            "催一下供应商那边的对账单",         # 动词开头
        ],
    )
    def test_still_recordable(self, text):
        assert is_confidently_recordable(text) is True, (
            f"{text!r} 是真任务，被闸门误挡了"
        )

    def test_min_length_boundary(self):
        """10 字是量出来的分界，两侧各验一次。"""
        assert is_confidently_recordable("一二三四五六七八九") is False    # 9 字
        assert is_confidently_recordable("一二三四五六七八九十") is True   # 10 字

    def test_short_waiting_task_is_deliberately_asked(self):
        """⚠️ **刻意断言它会被反问** —— 锁住一个已知的、被接受的代价。

        「等对方回复合同条款」9 字、动词「等」不在表里、也没有时间标记，
        三条信号都不满足 → 被反问。

        这是设计文档 12.1.1 明写下来的取舍，不是 bug：
        「宁可多问一句，不要静默建一条垃圾事务」。
        那份文档还写明了将来若这类句子变多，该加的是
        「等 / 待 / 得 / 需要」这类**正向**信号 ——
        **不是**把「先问了再说」这条规矩取消掉。

        锁住它的意义：将来有人看到「等对方回复」被问、想「优化」掉这个反问，
        这条测试会提醒他那是**拿垃圾数据换少一次点击**，需要重新论证。
        """
        assert is_confidently_recordable("等对方回复合同条款") is False


class TestReplyActuallyClarifies:
    """端到端：反问里**必须**带选项，且**不能**建事务。"""

    @pytest.fixture()
    def svc(self, tmp_path):
        from freeagent.app import build_app
        from freeagent.services.chat import ChatService

        app = build_app(tmp_path / "c.db")
        return app, app.chat

    def test_hello_does_not_create_a_task(self, svc):
        app, chat = svc
        before = len(app.tasks.list_all())
        reply = chat.respond("你好")
        assert len(app.tasks.list_all()) == before, (
            "「你好」不该建事务 —— 这正是真库 0277e93d/0fbbdb13 的成因"
        )
        assert reply.kind is ChatReplyKind.CLARIFY, (
            f"应当反问，实际 {reply.kind}"
        )
        app.close()

    def test_clarify_offers_both_read_and_write(self, svc):
        """分不清他要读还是要记，就**两个都给**，别替他决定。"""
        app, chat = svc
        reply = chat.respond("你好")
        joined = " ".join(reply.suggestions or ())
        assert "记一件事" in joined, reply.suggestions
        assert any(k in joined for k in ("今天", "全部未结束")), reply.suggestions
        app.close()


class TestHelpStructureInsteadOfWordList:
    """"能不能做什么"判结构，不判词表。"""

    @pytest.fixture()
    def chat(self, tmp_path):
        from freeagent.app import build_app

        app = build_app(tmp_path / "h.db")
        yield app.chat
        app.close()

    @pytest.mark.parametrize(
        "text",
        [
            "你可以做什么？",      # 实测漏掉的那句
            "你能做什么",
            "能做什么",
            "会什么",
            "能干嘛",
            "可以干啥",
            "能帮我做啥",
            "有什么功能",
            "怎么用",
            "帮助",
            "help",
        ],
    )
    def test_recognised(self, chat, text):
        assert chat._is_help_request(text) is True, f"{text!r} 没被认成求助"

    @pytest.mark.parametrize(
        "text",
        [
            "等周报进展如何",      # 含「周报/进展」⇒ 是真问题
            "提醒我一下",          # 含「提醒」
            "我在等谁",            # 含「在等」
        ],
    )
    def test_real_questions_still_not_help(self, chat, text):
        """原守卫不能因为加了结构判断而失效。"""
        assert chat._is_help_request(text) is False, f"{text!r} 被误判成求助"

    def test_help_reply_actually_returned(self, chat):
        reply = chat.respond("你可以做什么？")
        assert reply.kind is ChatReplyKind.HELP, reply.kind
