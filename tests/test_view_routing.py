"""**LLM 选路 + 规则给数** 的接缝测试。

这条路径换掉的是「怎么听懂这句话」，**不是**「事实从哪来」。
所以这里锁的核心命题只有一个：

    模型只被允许**挑一个名字**；答复里的每一条都来自库。

其余都是它的边界：封闭集能不能被撑破、幻觉名字会不会被当视图渲染、
模型说「不匹配」时会不会被逼着硬选、以及它挂掉时问答还能不能答。

**全程不调真 key。** 真实 LLM 的选路**质量**不在这套测试里测 ——
那需要真凭据且不确定，混进 CI 只会变成随机红灯。质量用
``tools/probe_view_routing.py`` 单独量，数字贴在 README 里。
"""

from __future__ import annotations

import io
from datetime import datetime
from typing import Any, Sequence

import pytest

from freeagent.app import build_app
from freeagent.cli.app import Repl
from freeagent.config import Config
from freeagent.domain import LLMError
from freeagent.services.chat import _VIEWS
from freeagent.services.clock import FrozenClock
from freeagent.services.llm.provider import (
    ClassificationResult,
    RoleHint,
    TaskRef,
    SignalRef,
)

NOW = datetime(2026, 9, 26, 14, 0)


class StubLLM:
    """只对 ``select_view`` 有意见的桩。

    ``can_select_view`` 单独可调 —— 那是「有没有意见」与「没有意见」的
    分界，混起来会让离线时每个问句都被当成「没匹配」。
    """

    def __init__(self, *, pick: Any = None, can: bool = True, boom: bool = False):
        self._pick = pick
        self._can = can
        self._boom = boom
        self.calls: list[str] = []

    @property
    def can_select_view(self) -> bool:
        return self._can

    def select_view(self, text: str, views: Sequence[tuple[str, str]]) -> str | None:
        self.calls.append(text)
        if self._boom:
            raise LLMError("炸了", provider="stub")
        return self._pick

    # -- 其余能力：与本测试无关，但要满足协议（duck typing）--------------
    llm_name = "StubLLM"

    def classify(self, text, role_hints):
        return ClassificationResult(kind="action")

    def refine_title(self, text):
        return text[:30]

    def split_steps(self, task, instruction=None):
        return ()

    def draft(self, task, instruction):
        return ""

    def suggest_schedule(self, task, signals):
        return ""


def _app(tmp_path, llm, *, view_routing: bool = True):
    """建一套带桩模型的环境。

    ``view_routing`` 默认**显式打开**：生产默认是关的（实测没赢，见
    ``Config.llm_view_routing``），若这里跟着默认关，下面每条测试都会
    **空转通过** —— 断言全绿，而被测的那条代码一次都没跑。那正是今天
    已经在「误写率」上栽过一次的那种绿灯。
    """
    app = build_app(
        tmp_path / "a.db", clock=FrozenClock(NOW), llm=llm,
        config=Config(llm_view_routing=view_routing),
    )
    work = app.roles.create("工作项目A", note="销售 周报 客户")
    family = app.roles.create("家庭", note="孩子 学校 材料")
    report = app.tasks.create("销售周报初稿", [work.id], intent="给老板看的")
    app.tasks.schedule(report.id, app.clock.today())
    fix = app.tasks.create("修窗户螺丝", [family.id])
    app.tasks.schedule(fix.id, app.clock.today())
    return app


def _reply(app, text: str):
    return app.chat.respond(text)


# =============================================================================
# 1. 封闭集是边界：模型只能挑名字，撑不破它
# =============================================================================
class TestClosedSet:
    def test_model_sees_exactly_the_declared_views(self, tmp_path):
        llm = StubLLM(pick=None)
        app = _app(tmp_path, llm)
        # 让桩把收到的 views 记下来，才能断言「模型看到的就是这八个」
        seen: list[Any] = []

        def spy(text, views):
            seen.append(views)
            return None

        llm.select_view = spy           # type: ignore[method-assign]
        app.chat.respond("今天该做什么？")
        assert seen, "没有调用 select_view"
        assert tuple(seen[0]) == _VIEWS, f"模型看到的清单与 _VIEWS 不一致：{seen[0]}"

    def test_hallucinated_view_name_is_rejected(self, tmp_path):
        """模型编一个视图名出来时，**绝不能**照着它渲染。

        闭集是这条接口的全部安全性。若编造的名字被当路由，就会去调一个
        不存在的表 —— 轻则崩溃，重则渲染出空壳还标成「答上来了」。
        """
        llm = StubLLM(pick="chart_of_goals")     # 不在 _VIEWS 里
        app = _app(tmp_path, llm)
        reply = app.chat.respond("我这周有什么安排")
        assert reply.route != "chart_of_goals", "幻觉的视图名被当成了路由"
        # 幻觉被丢弃后应回落到关键词表（今天），而不是崩掉或空壳
        assert reply.route in {"today", "closed", None, "all", "progress"}, reply.route

    def test_model_cannot_widen_the_set_by_returning_a_list(self, tmp_path):
        """返回列表/嵌套结构一律当作没意见 —— 类型不是猜测的余地。"""
        llm = StubLLM(pick=["today", "all"])
        app = _app(tmp_path, llm)
        reply = app.chat.respond("今天该做什么？")
        assert reply.route == "today", f"返回列表却改变了路由：{reply.route}"


# =============================================================================
# 2. 事实仍由规则给：模型只挑名字，内容来自库
# =============================================================================
class TestFactsStillComeFromRules:
    def test_reply_content_is_read_from_db_not_from_model(self, tmp_path):
        """模型说「today」→ 渲染的必须是**库里真有的那两条**。"""
        llm = StubLLM(pick="today")
        app = _app(tmp_path, llm)
        reply = app.chat.respond("随便一句没有关键词的话")
        assert reply.route == "today"
        titles = {i.title for i in reply.items}
        assert titles == {"销售周报初稿", "修窗户螺丝"}, f"渲染的不是库里的数据：{titles}"
        assert llm.calls, "应当问过模型"

    def test_prompt_never_asks_for_content(self, tmp_path):
        """结构性保证：交给模型的只有「选哪个视图」这一件事。

        如果哪天有人把「顺便把答案也写了」加进这条路径，这条测试不会红
        —— 它只能证明内容来自库。所以这里额外锁一句：模型返回的**只有名字**，
        渲染函数不接受任何来自模型的文本。这是设计约束，不是巧合。
        """
        llm = StubLLM(pick="all")
        app = _app(tmp_path, llm)
        reply = app.chat.respond("全部未结束的事")
        # all 视图列的是未结束的事，且标题全部来自库
        for item in reply.items:
            assert item.task_id, "每一条都必须绑到真实事务 id"
        assert all(t in {t.title for t in app.tasks.list_all()}
                   for t in (i.title for i in reply.items))


# =============================================================================
# 3. 「没有意见」与「没有意见的能力」必须分得开
# =============================================================================
class TestNoOpinionVsNoCapability:
    def test_rules_provider_is_never_consulted(self, tmp_path):
        """离线（``can_select_view=False``）时**一次调用都不能发生**。

        这条是性能与离线可用性的保证：规则层没有网络，一旦它被问了又答
        「不知道」，每个问句都会白白走一趟无用的判定。
        """
        llm = StubLLM(pick="today", can=False)
        app = _app(tmp_path, llm)
        app.chat.respond("今天该做什么？")
        app.chat.respond("我在等什么")
        assert llm.calls == [], f"规则层被调用了：{llm.calls}"

    def test_null_falls_back_to_keyword_router(self, tmp_path):
        """模型说「不匹配」→ 回落到关键词表，而不是直接说不知道。

        理由：模型说「我不匹配」不等于**系统**没能力答。「今天该做什么」
        对模型可能有点含糊，但关键词表完全答得上来。
        """
        llm = StubLLM(pick=None, can=True)
        app = _app(tmp_path, llm)
        reply = app.chat.respond("今天该做什么？")
        assert reply.route == "today", f"模型没意见时不该丢掉关键词表：{reply.route}"
        assert reply.inferred is False, "关键词表命中了，不该自报「猜的」"

    def test_null_on_a_genuinely_unknown_question_still_gets_fallback_notice(
        self, tmp_path
    ):
        """模型没意见**且**关键词表也没命中 → 兜底 + 自报家门。"""
        llm = StubLLM(pick=None, can=True)
        app = _app(tmp_path, llm)
        reply = app.chat.respond("接下来该关注什么")
        assert reply.inferred is True
        assert "没听懂" in reply.text


# =============================================================================
# 4. 选路是锦上添花：它挂了不能掀翻问答
# =============================================================================
class TestSelectionFailureIsContained:
    def test_exception_falls_back_and_still_answers(self, tmp_path):
        llm = StubLLM(pick=None, can=True, boom=True)
        app = _app(tmp_path, llm)
        reply = app.chat.respond("今天该做什么？")
        assert reply.route == "today", "模型炸了应当回落到关键词表，而不是让问答失败"
        assert "销售周报初稿" in reply.text

    def test_unsatisfiable_route_falls_through(self, tmp_path):
        """模型选了 ``closed``，但句子里没有时间范围 → 落回，不编窗口。

        硬渲染一个空窗口等于**给出一个看起来像答案的假答案** ——
        比答错更坏。
        """
        llm = StubLLM(pick="closed")
        app = _app(tmp_path, llm)
        reply = app.chat.respond("最近情况怎么样")     # 无时间范围词
        assert reply.route != "closed" or "没听懂" in reply.text

    def test_unsatisfiable_role_falls_through(self, tmp_path):
        llm = StubLLM(pick="role")
        app = _app(tmp_path, llm)
        reply = app.chat.respond("随便聊聊")            # 没点名任何脉络
        assert reply.kind is not None
        # 关键不是路由值，而是**没崩、且不是空壳**
        assert reply.text.strip(), "答复不能是空的"


# =============================================================================
# 6. 开关默认关，且这一条是**决策**，不是疏忽
# =============================================================================
class TestViewRoutingIsOffByDefault:
    """把「实测没赢」这个结论钉在测试里。

    这类开关最危险的结局是：某天有人读到代码里有一条完整的 LLM 选路
    实现，觉得「架构上更该这样」，就把它打开 —— 而那次打开会让系统
    **更不诚实**（模型一次都不返回 null，自报防线被拆）。

    所以默认值必须被断言。想打开它，得先改这条测试，并在里面附上新数字。
    """

    def test_config_default_is_off(self):
        assert Config().llm_view_routing is False, (
            "模型选路实测不如关键词表，默认必须是关的"
        )

    def test_off_means_the_model_is_never_consulted(self, tmp_path):
        llm = StubLLM(pick="all", can=True)
        app = _app(tmp_path, llm, view_routing=False)
        app.chat.respond("今天该做什么？")
        app.chat.respond("我在等什么")
        assert llm.calls == [], f"开关关着却仍然调了模型：{llm.calls}"

    def test_on_means_the_model_is_consulted(self, tmp_path):
        """反方向：开关打开时必须真的问，否则接线是死的。"""
        llm = StubLLM(pick="all", can=True)
        app = _app(tmp_path, llm, view_routing=True)
        app.chat.respond("今天该做什么？")
        assert llm.calls, "开关开着却没调模型 —— 接线断了"

    def test_flag_round_trips_through_config_file(self, tmp_path):
        """写进 config.json 又读回来，值不能丢。

        配置项读不回来是最难查的一类 bug：界面改了像是没反应。
        """
        from freeagent.config import save_config
        home = tmp_path / "home"
        home.mkdir()
        cfg = Config(home=str(home), llm_view_routing=True)
        save_config(cfg, home)
        from freeagent.config import load_config
        assert load_config(home).llm_view_routing is True
        # 再确认关掉也能存回去
        save_config(Config(home=str(home), llm_view_routing=False), home)
        assert load_config(home).llm_view_routing is False


# =============================================================================
# 7. 装配路径：``build_app`` 可能拿到 ``config=None``
# =============================================================================
class TestBuildAppWithNoConfig:
    """``build_app`` 允许调用方只给 ``llm=`` 和 ``energy_windows=``。

    那时 ``the_config`` 会**留成 None**（只有「config 没给**且**
    llm/energy_windows 至少缺一个」时才从盘上读）。

    这条路原先是崩的：直接 ``the_config.llm_view_routing`` 会抛
    ``AttributeError``，而**全部 1440 条测试都绿** —— 没有任何一条
    同时传那两个参数。是在 basedpyright 报 ``reportOptionalMemberAccess``
    之后才发现的，不是靠跑测试发现的。

    所以它必须有自己的测试：**类型检查抓到的东西，测试要能钉住**，
    否则下次重构照样漏。
    """

    def test_does_not_crash_when_config_is_none(self, tmp_path):
        from freeagent.services.sorting import EnergyWindows

        app = build_app(
            tmp_path / "a.db",
            clock=FrozenClock(NOW),
            llm=StubLLM(pick="all", can=True),
            energy_windows=EnergyWindows(),          # 同时给两个 → config 留 None
        )                                              # 刻意不给 config=
        assert app.chat is not None

    def test_view_routing_defaults_off_without_config(self, tmp_path):
        """没给 config 时必须走「关」，而不是碰运气。"""
        from freeagent.services.sorting import EnergyWindows

        llm = StubLLM(pick="all", can=True)
        app = build_app(
            tmp_path / "b.db", clock=FrozenClock(NOW), llm=llm,
            energy_windows=EnergyWindows(),
        )
        app.roles.create("工作项目A", note="销售")
        app.chat.respond("今天该做什么？")
        assert llm.calls == [], "没给 config 时不该默认打开模型选路"


# =============================================================================
# 5. 入口层：终端/飞书也走同一条（一份能力一份实现）
# =============================================================================
class TestReplUsesTheSameRouting:
    def test_repl_consults_the_model_too(self, tmp_path):
        """只读优先那条防线对两个入口都成立。

        实测踩过：在 ``respond`` 里加只读优先，而 ``Repl`` 自己先调
        ``read_intent`` 从不调用 ``respond`` —— 防线被完全绕过。
        """
        llm = StubLLM(pick="all")
        app = _app(tmp_path, llm)
        out = io.StringIO()
        r = Repl(app, out=out)
        r._dispatch("今天该做什么？")
        assert llm.calls, "Repl 没有问模型 —— 两个入口又分叉了"

    def test_repl_still_does_not_write_when_routing_says_read(self, tmp_path):
        llm = StubLLM(pick="all")
        app = _app(tmp_path, llm)
        before = [t.title for t in app.tasks.list_all()]
        Repl(app, out=io.StringIO())._dispatch("全部未结束的事")
        assert [t.title for t in app.tasks.list_all()] == before, "选路说「读」却写了"
