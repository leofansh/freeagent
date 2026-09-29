"""**问句不许污染数据**（判定逻辑 + 终端侧）。

这条来自一次真实事故：我口头问了助手几句

    今天要做什么 / 周报进展怎么样 / 把周报改到下周三 / 我上周完成了什么

结果它把「周报进展怎么样」和「这条为什么排第一」**建成了角色**，
把问句建成了事务。角色是整个模型的组织结构，被随口一句话污染代价很大。

根因是两步连锁：

1. 问句没被识别成「问」，被当成「要记的一件事」，于是触发追问；
2. 追问之后，**下一句输入被当成角色名** ``_ensure_role`` 建成角色。

Web 表单那侧的防护在 ``test_web.py::TestCreateRejectsQuestions``。
"""

from __future__ import annotations

import io
from datetime import datetime
from pathlib import Path
import tempfile

import pytest

from freeagent.app import build_app
from freeagent.cli.app import Repl
from freeagent.services.clock import FrozenClock
from freeagent.services.llm import InputIntent, is_question_like, read_intent

#: 我实测时真正问过的话 —— 原样保留当回归输入，别换成温和的样本
REAL_QUESTIONS = [
    "今天要做什么",
    "周报进展怎么样",
    "我现在该先做哪件",
    "这条为什么排第一",
    "我上周完成了什么",
]

#: 我实测时真正说过的一句「改已有事务」—— 同样建出了垃圾事务
REAL_MUTATE_REQUESTS = [
    "把周报改到下周三",
    "把会议推迟到明天",
    "删掉那条旧提醒",
    "换个时间交物业费",
]


# =============================================================================
# 判定本身
# =============================================================================
class TestIsQuestionLike:
    @pytest.mark.parametrize("text", REAL_QUESTIONS + [
        "为什么这条排第一？",
        "这是什么",
        "谁能帮我看下",
        "什么时候到期",
        "值不值得做？",
        "是不是该放弃了",
        "有没有更简单的办法",
    ])
    def test_detects_questions(self, text):
        assert is_question_like(text) is True, f"没认出这是问句：{text}"

    @pytest.mark.parametrize("text", [
        "下周二要交的销售周报初稿",
        "修窗户螺丝",
        "等客户法务回复合同条款",
        "交物业费",
        "提醒我下午三点吃药",
        # 长句里的「什么」是内容，不是疑问 —— 不能误伤
        "给客户解释这个方案为什么这么贵",
        "写清楚需求到底是什么",   # 有「什么」，但以动作动词开头 → 是任务
        "把上次那个材料清单打印出来",  # 有「把」，但不是处置动词 → 是任务
    ])
    def test_allows_real_tasks(self, text):
        assert is_question_like(text) is False, f"误判成问句了：{text}"

    @pytest.mark.parametrize("text", REAL_MUTATE_REQUESTS)
    def test_detects_mutate_requests(self, text):
        assert read_intent(text) is InputIntent.MUTATE, f"没认出这是改已有事务：{text}"

    @pytest.mark.parametrize("text", ["", "   "])
    def test_blank_is_not_question(self, text):
        assert read_intent(text) is InputIntent.RECORD

    def test_long_why_sentence_is_a_task_short_one_is_a_question(self):
        """长句 + 疑问词 = 任务；短句才收紧判定。"""
        assert read_intent("我想知道客户为什么迟迟不回复合同该怎么推进") is InputIntent.RECORD
        assert read_intent("客户为什么不回复") is InputIntent.QUESTION

    def test_verb_prefix_beats_question_word(self):
        """动作动词开头 → 是任务，不管句中有没有疑问词。"""
        assert read_intent("写清楚需求到底是什么") is InputIntent.RECORD
        assert read_intent("整理客户名单") is InputIntent.RECORD


# =============================================================================
# 终端：问句既不建事务，也不建角色
# =============================================================================
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


class TestCliRejectsQuestions:
    def test_question_creates_nothing(self, repl_with_roles):
        r, app, _out = repl_with_roles
        tasks_before = len(app.tasks.list_all())
        roles_before = _role_names(app)
        for q in REAL_QUESTIONS:
            r._dispatch(q)
        assert len(app.tasks.list_all()) == tasks_before, "问句建出了事务"
        assert _role_names(app) == roles_before, f"问句建出了角色：{_role_names(app)}"

    def test_mutate_request_creates_nothing(self, repl_with_roles):
        """「把周报改到下周三」建出垃圾事务 —— 同一个事故的第二种形态。"""
        r, app, _out = repl_with_roles
        before = len(app.tasks.list_all())
        for m in REAL_MUTATE_REQUESTS:
            r._dispatch(m)
        assert len(app.tasks.list_all()) == before, (
            f"「改已有事务」的请求建出了新事务："
            f"{[t.title for t in app.tasks.list_all()]}"
        )

    def test_question_does_not_enter_pending_state(self, repl_with_roles):
        """问句不该留下待回答的追问 —— 否则下一句会被当成角色名。"""
        r, _app, _out = repl_with_roles
        r._dispatch("今天要做什么")
        assert r._pending is None, "问句不该触发追问"

    def test_question_answered_by_question_creates_nothing(self, repl_with_roles):
        """原始 bug 的复现：真问一句 → 被追问 → 再问一句 → 建出垃圾角色。"""
        r, app, _out = repl_with_roles
        before = _role_names(app)
        r._dispatch("今天要做什么")      # 旧代码：这里会追问
        r._dispatch("周报进展怎么样")     # 旧代码：这里建出同名角色
        assert _role_names(app) == before

    def test_task_sentence_never_becomes_a_role(self, repl_with_roles):
        """同一个 bug 的**第二条路径**，问句守卫挡不住。

        任务句同样是 RECORD 意图，所以第一版修复漏了它：
        提醒句没命中角色 → 追问 → 下一句任务句被 ``_ensure_role`` 建成角色。
        """
        r, app, _out = repl_with_roles
        before = _role_names(app)
        r._dispatch("提醒我下午三点修窗户螺丝")   # 不命中角色 → 追问
        assert r._pending is not None, "应当追问"
        r._dispatch("等客户法务回复合同")          # 旧代码：这里建出同名角色
        assert _role_names(app) == before, (
            f"任务句被建成了角色：{_role_names(app) - before}"
        )

    def test_unknown_role_answer_tells_you_how_to_make_one(self, repl_with_roles):
        r, app, out = repl_with_roles
        r._dispatch("提醒我下午三点修窗户螺丝")
        r._dispatch("等客户法务回复合同")
        text = out.getvalue()
        assert "/role-add" in text, "应告诉用户怎么显式建角色"
        assert "/skip" in text, "也该给出跳过方式"

    def test_question_is_answered_not_deflected(self, repl_with_roles):
        """问句要**答上来**，而不是把人打发去敲命令。

        这条来自一次实测（飞书里问「今天该做什么？」）：

            （这句我当问句处理，没有建事务）我只会做两件事：记一件事（说人话）、
            执行命令（/today、/all、/task 等）。你这条像是问句 —— 查看用命令：
            /today 今天要动的，/all 全部，/task <id> 看某一条的上下文。

        答复里**就写着** ``/today``，而且同一句话在 Web 对话框里能正常答 ——
        所以这不是能力缺失，是 Repl 这条路自己另写了一份「一律拒绝」。

        下面两条断言一起锁住它：既不许再打发人，也必须真的答。
        """
        r, _app, out = repl_with_roles
        r._dispatch("今天该做什么？")
        text = out.getvalue()
        assert "我只会做两件事" not in text, "仍在用「我只会做两件事」打发问句"
        assert "查看用命令" not in text, "仍在把人推去敲 /today"
        assert "今天" in text, "应当真的回答今天视图，而不是回避"

    def test_answer_contains_real_task_data(self, repl_with_roles):
        """答复必须带上**库里的真实数据**，不能只是一句空话。"""
        r, app, out = repl_with_roles
        r._dispatch("下周二要交的销售周报初稿")
        # 必须显式排进「今天」：冻结时钟是 2026-09-26，而「下周二」落在
        # 09-29 —— 不排进去的话今天视图本就该是空的，测的就不是数据了。
        for t in app.tasks.list_all():
            app.tasks.schedule(t.id, app.clock.today())
        out.truncate(0)
        out.seek(0)
        r._dispatch("今天该做什么？")
        text = out.getvalue()
        assert "销售周报" in text, f"答复里没有真实事务：{text!r}"
        assert "启发式提示，不是评分" in text, "排序必须标注不是评分"

    def test_followup_reference_uses_last_items(self, repl_with_roles):
        """多轮指代要接得上：「今天该做什么」→「第二个为什么」。

        靠 ``Repl._last_items`` 传递。这条在 Web 那边由客户端回传
        ``last_items``，Repl 是常驻进程、自己有地方放 —— 不存就断链。
        """
        r, app, out = repl_with_roles
        # 两条都必须能自己落到角色上。若某条不命中角色就会触发**追问**，
        # 下一句于是走 _handle_pending 而不是 _natural —— 测的就不是这条路径了。
        r._dispatch("下周二要交的销售周报初稿")
        r._dispatch("整理客户名单")
        assert r._pending is None, "这两句都该直接落角色，不该追问"
        for t in app.tasks.list_all():
            app.tasks.schedule(t.id, app.clock.today())
        r._dispatch("今天该做什么？")
        assert len(r._last_items) == 2, f"答复列了 2 条却没记全：{r._last_items}"
        r._dispatch("第二个为什么")
        assert "我只会做两件事" not in out.getvalue(), (
            "「第二个为什么」不该被当成问句打发掉"
        )

    def test_clarification_answer_still_works(self, repl_with_roles):
        """不能把正常追问也堵死 —— 那是我唯一一次真正的澄清机会。"""
        r, app, _out = repl_with_roles
        r._dispatch("qqqqzzz完全不相关的一件事")   # 不命中任何角色关键词
        assert r._pending is not None, "应当追问角色"
        r._dispatch("工作项目A")
        assert any("qqqqzzz" in t.title for t in app.tasks.list_all())
        assert _role_names(app) == {"工作项目A", "家庭"}, "不该新建角色"

    def test_real_task_still_works_end_to_end(self, repl_with_roles):
        r, app, _out = repl_with_roles
        r._dispatch("下周二要交的销售周报初稿")
        tasks = app.tasks.list_all()
        assert len(tasks) == 1, f"应该正好建一条：{[t.title for t in tasks]}"
        assert "销售周报" in tasks[0].title
        assert tasks[0].scheduled_for is not None, "「下周二」应被解析成排期"

    def test_wait_kind_still_inferred(self, repl_with_roles):
        r, app, _out = repl_with_roles
        r._dispatch("等客户回复")
        assert app.tasks.list_all()[0].kind.value == "wait"
