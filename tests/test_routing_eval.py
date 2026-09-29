"""**路由评测** —— 量「它到底选了哪个视图」。

为什么要有这个文件
------------------
原先的 ``test_question_input.py`` 只做**否定断言**：问句不许写数据。
它守住了「不伤害」，但没守住「做对」。

而这两件事是可以分开失败的。一个把所有问题都路由到「今天」的
实现，能通过当时**全部**测试 —— 因为它从不写数据，否定断言全绿。
于是「路由选错」这件事在测试里根本不可见。

这个文件补的是另一半：每条真实问句标注**期望路由**，然后量准确率。
指标两个：

- **误写率**（必须 0）：问句不许建事务、不许建角色。
- **路由准确率**：选了用户真正问的那个视图吗。

为什么 ``inferred`` 也要量
------------------------
「我按『今天』理解了你这句」是个**自我暴露**，不是修饰。
没有它，未命中时用户拿到的是一份逐字等同于真问「今天」的清单，
无从分辨自己被猜了 —— 那是把「我不知道」伪装成答案。

所以本文件同时盯两件不同的事：*猜得准不准*（route）和
*有没有承认在猜*（inferred）。后者不随前者改善而自动变好。
"""

from __future__ import annotations

import io
from datetime import datetime
from pathlib import Path
import tempfile

import pytest

from freeagent.app import build_app
from freeagent.cli.app import Repl
from freeagent.services.chat import ChatReplyKind
from freeagent.services.clock import FrozenClock

#: 一条评测样本。
#:
#: ``route`` 是**期望**的视图；``inferred`` 是期望它是否该自报「猜的」。
#: 两者独立：一条问句完全可能既选错视图、又没承认在猜。
class Case:
    __slots__ = ("text", "route", "inferred", "why")

    def __init__(self, text: str, route: str, *, inferred: bool = False, why: str = ""):
        self.text = text
        self.route = route
        self.inferred = inferred
        self.why = why

    def __repr__(self) -> str:  # 让报错信息自带解释
        return f"Case({self.text!r}, route={self.route!r}, inferred={self.inferred})"


#: 真实问句语料。**故意包含应当失败的** —— 全是能过的样本等于没测。
#:
#: 后三组是本次评测的真正目标：
#:   1. 措辞变体（同一个视图的不同说法）—— 关键词表的覆盖压力
#:   2. 表外问句 —— 应当走兵底，且**必须**自报家门
#:   3. 「看起来像命中、其实答错」—— 最危险的一类，见 each()
GOLDEN: tuple[Case, ...] = (
    # -- 直白命中 --------------------------------------------------------
    Case("今天该做什么？", "today", why="基线：CAN_DO 第一条"),
    Case("我在等什么", "waiting", why="基线：CAN_DO 第二条"),
    Case("有什么提醒", "reminders", why="基线：CAN_DO 第三条"),
    Case("我上周完成了什么", "closed", why="基线：CAN_DO 第四条"),
    Case("为什么这条排第一", "why", why="基线：CAN_DO 第七条"),
    Case("全部未结束的事", "all", why="未结束 → all"),
    # -- 措辞变体：同一视图，换说法 --------------------------------------
    Case("现在先干哪个？", "today", why="「现在」「先干」都在 _TODAY_WORDS"),
    Case("有什么卡住的", "waiting", why="「卡住」在 _WAITING_WORDS"),
    Case("别忘的事有哪些", "reminders", why="「别忘」在 _REMINDER_WORDS"),
    Case("它凭什么排最前", "why", why="「排最前」在 _WHY_WORDS"),
    Case("这周做完了什么", "closed", why="「这周」命中时间窗"),
    # -- 表外问句：应当兵底，且必须自报 ------------------------------------
    Case("接下来该关注什么", "today", inferred=True,
         why="一个视图词都不含 → 兵底是唯一诚实的选项"),
    Case("手上有什么活", "today", inferred=True,
         why="口语说法，关键词表覆盖不到"),
    # -- 危险类：关键词**误命中**，比兵底更糟 ------------------------------
    # 「安排」在 _TODAY_WORDS 里，于是这句被判成真·今天，inferred=False。
    # 用户问的是「本周」，拿到的是今天 —— 而且**没有任何提示**。
    # 这类比兵底更坏：兵底至少自报家门，误命中连自报的机会都没有。
    Case("我这周有什么安排", "closed", inferred=True,
         why="应问「本周」，但「安排」把它拽进 today 且不自报"),
    # 「怎么样」在 _PROGRESS_WORDS，于是「我最近状态怎么样」被当成问进展。
    Case("我最近状态怎么样", "progress", inferred=True,
         why="无具体事务可问进展，应兵底并自报"),
)


@pytest.fixture
def repl_with_roles():
    home = Path(tempfile.mkdtemp())
    app = build_app(home / "a.db", clock=FrozenClock(datetime(2026, 9, 26, 14, 0)))
    app.roles.create("工作项目A", note="销售 周报 客户")
    app.roles.create("家庭", note="孩子 学校 材料")
    out = io.StringIO()
    return Repl(app, out=out), app, out


def _role_names(app) -> set[str]:
    return {r.name for r in app.roles.list_roles(include_inactive=True)}


# =============================================================================
# 评测查出来的真 bug：名词短语被当成指令，真的建出了垃圾事务。
# =============================================================================
class TestNounPhraseQueryIsNotRecorded:
    """「全部未结束的事」这类**名词短语**原先会建出事务。

    根因不是路由表缺词，是 :func:`read_intent` 判不出它 —— 没有疑问词、
    没有问号，于是落到 RECORD，而 **RECORD 是默认分支**。所以「没被认
    出来」等于「当成要记的事」，这是把读路径的失败转嫁给了写路径。

    修法是让只读视图**先**试：答错只是读错一次，写错是往组织结构里塞
    脏数据。两条能力按后果不对称排序。
    """

    #: 角色备注里故意塞进这些词，让规则分类**命中** ——
    #: 否则 ``_record`` 会停在追问上，看不出它其实已经走错了分支。
    @pytest.fixture
    def repl_matching_roles(self):
        home = Path(tempfile.mkdtemp())
        app = build_app(home / "a.db", clock=FrozenClock(datetime(2026, 9, 26, 14, 0)))
        app.roles.create("工作项目A", note="全部 未结束 事务 清单 一览")
        app.roles.create("家庭", note="孩子 学校")
        return Repl(app, out=io.StringIO()), app

    @pytest.mark.parametrize("text", [
        "全部未结束的事",
        "所有未结束的清单",
        "全部事务一览",
        "未结束的事",
    ])
    def test_does_not_create_task(self, repl_matching_roles, text):
        r, app = repl_matching_roles
        before = [t.title for t in app.tasks.list_all()]
        r._dispatch(text)
        after = [t.title for t in app.tasks.list_all()]
        assert after == before, f"「{text}」建出了事务：{[t for t in after if t not in before]}"

    def test_answers_as_a_view_instead(self, repl_matching_roles):
        r, _app = repl_matching_roles
        reply = r.app.chat.respond("全部未结束的事")
        assert reply.route == "all", f"应当答「全部」，实际 route={reply.route!r}"
        assert reply.inferred is False, "这是真的命中了视图，不该自报「猜的」"

    @pytest.mark.parametrize("text, note", [
        ("把今天的会议纪要整理一下", "含动作词「整理」—— 是指令，不能被视图抢走"),
        ("整理客户名单", "以动作词开头 —— 是指令"),
        ("把周报改到下周三", "改动既有事务 —— 要如实说做不到"),
    ])
    def test_real_instructions_still_reach_the_write_path(self, repl_matching_roles, text, note):
        """反方向守卫：只读优先**不能**把真任务吃掉。

        这是本次改动最大的风险 —— 视图抢走指令的话，任务会**无声无息地
        没发生**，比建错事务更难发现。所以必须双向锁。
        """
        r, _app = repl_matching_roles
        reply = r.app.chat.respond(text)
        assert reply.route != "all", f"{note}：却被当成了视图查询"


# =============================================================================
# 指标一：误写率。必须恒为 0，这条不给「下界」，给了就等于开门放进脏数据。
# =============================================================================
class TestNoWritesFromQuestions:
    def test_miswrite_rate_is_zero(self, repl_with_roles):
        r, app, _out = repl_with_roles
        tasks0 = len(app.tasks.list_all())
        roles0 = _role_names(app)
        wrote: list[str] = []
        for case in GOLDEN:
            r._dispatch(case.text)
            if len(app.tasks.list_all()) != tasks0:
                wrote.append(f"{case.text!r} 建出了事务")
            if _role_names(app) != roles0:
                wrote.append(f"{case.text!r} 建出了角色：{_role_names(app) - roles0}")
        assert not wrote, "问句写进了数据：\n  " + "\n  ".join(wrote)


# =============================================================================
# 指标二：路由准确率 + 有没有承认在猜
# =============================================================================
def _run(repl) -> dict[str, Case]:
    """跑一遍语料，返回 {问句: 实际观测} 的对照。"""
    seen: dict[str, Case] = {}
    for case in GOLDEN:
        reply = repl.app.chat.respond(case.text)
        seen[case.text] = Case(
            case.text, reply.route, inferred=reply.inferred,
        )
    return seen


def test_routing_report(repl_with_roles, capsys):
    """跑评测并**打印**对照表。

    这条测试的价值一半在断言、一半在那张表 —— 改完路由就能立刻看到
    哪些句子被改好了、哪些只是从一种错换成了另一种错。

    下界是**当前基线**：锁住它，防止以后无声退化。数字只许涨不许跌。
    """
    r, _app, _out = repl_with_roles
    actual = _run(r)

    route_ok = inferred_ok = 0
    lines: list[str] = []
    for case in GOLDEN:
        got = actual[case.text]
        r_ok = got.route == case.route
        i_ok = got.inferred == case.inferred
        route_ok += r_ok
        inferred_ok += i_ok
        mark = "OK " if (r_ok and i_ok) else "MISS"
        flag = " (自报)" if got.inferred else ""
        lines.append(
            f"  {mark} {case.text!r:24} 期望 {case.route}{'*' if case.inferred else ''}"
            f" → 实际 {got.route}{flag}"
        )

    total = len(GOLDEN)
    print("\n=== 路由评测 ===")
    print("\n".join(lines))
    print(
        f"  路由准确率 {route_ok}/{total} = {route_ok / total:.0%}"
        f"    自报正确率 {inferred_ok}/{total} = {inferred_ok / total:.0%}"
    )
    print("  (* = 期望自报「猜的」)")

    with capsys.disabled():
        print("\n".join(lines))
        print(f"  ROUTE={route_ok}/{total} INFERRED={inferred_ok}/{total}")

    # 基线下界：锁住当前水平。提高它是好事，但那是**显式**改数字，
    # 不是让某天悄悄滑下去的副作用。
    # 14/15 与 13/15 是修掉「名词短语被当成指令」之后量到的数。
    # 剩下那一条 MISS 是**词表误命中**（「安排」被当成今天），修它要动
    # 路由本身 —— 那正是要先有数字才敢动的那件事。
    assert route_ok >= 14, f"路由准确率跌到 {route_ok}/{total}（基线 14）"
    assert inferred_ok >= 13, f"自报正确率跌到 {inferred_ok}/{total}（基线 13）"


# =============================================================================
# 自报家门的**可见性**：标记在数据上还不够，用户看到的是 text。
# =============================================================================
class TestFallbackIsVisible:
    def test_inferred_reply_says_so_in_text(self, repl_with_roles):
        r, _app, _out = repl_with_roles
        reply = r.app.chat.respond("接下来该关注什么")
        assert reply.inferred is True
        assert "没听懂" in reply.text, (
            f"兵底了却没在正文里说 —— 用户会把它当答案：{reply.text!r}"
        )

    def test_matched_reply_does_not_claim_inference(self, repl_with_roles):
        """反方向也要锁：真·命中的答复不许带自报，否则满屏都是免责声明。"""
        r, _app, _out = repl_with_roles
        reply = r.app.chat.respond("今天该做什么？")
        assert reply.inferred is False
        assert "没听懂" not in reply.text, (
            f"明明命中了却自报没听懂：{reply.text!r}"
        )

    def test_today_by_keyword_is_not_marked_inferred(self, repl_with_roles):
        """「今天」是**词表命中**，所以 ``inferred`` 为假。

        这条是给「误命中」留的基准：将来把路由换成 LLM 选路后，
        「我这周有什么安排」应当变成 inferred=True，而真问「今天」仍是 False。
        没有这条基准，就分不清是路由变准了还是自报被一刀切打开了。
        """
        r, _app, _out = repl_with_roles
        reply = r.app.chat.respond("今天该做什么？")
        assert reply.route == "today"
        assert reply.inferred is False
        assert reply.kind is ChatReplyKind.ANSWER
