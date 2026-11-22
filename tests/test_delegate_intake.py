"""一句话起手的服务层守卫（V1.141）。

守的是「一句话 → 认项目 + 要做的事 → 追问补缺」这条纯函数。关键不变量：

1. **首次解析走模型，追问补缺不走模型** —— 追问再调模型既多花计费，又把
   「人的回答」交给模型自由发挥，那是把安全交互交给不确定性。
2. **认不出的项目名一律不预填** —— 猜错项目的代价是在错误的仓库里动手。
3. **空字段是要追问，不是失败** —— 没有一句话起手能把半句话变出完整简报。
"""

from __future__ import annotations

from freeagent.services import delegate_intake
from freeagent.services.capability import CapabilityView
from freeagent.services.llm.provider import DelegationIntent


class _LLM:
    """假模型：返回预设的意图，或抛错。记下被调用几次。"""

    def __init__(self, intent=None, *, error=None):
        self.intent = intent
        self.error = error
        self.calls = 0

    def parse_delegation(self, text, known_projects):
        self.calls += 1
        if self.error:
            raise self.error("boom")
        return self.intent


def _view():
    return CapabilityView(
        projects=("D:/alpha", "D:/beta"),
        project_names=("Alpha", "Beta"),
    )


class TestFirstParse:
    def test_all_present_needs_nothing(self):
        llm = _LLM(DelegationIntent("Alpha", "把按钮改成蓝色", True))
        p = delegate_intake.plan("让它改 Alpha 把按钮改成蓝色", _view(), llm)
        assert p.recognized and not p.need and not p.question
        assert p.project == "Alpha"
        assert p.directory == "D:/alpha"
        assert p.brief == "把按钮改成蓝色"
        assert llm.calls == 1

    def test_missing_project_asks_for_it_first(self):
        llm = _LLM(DelegationIntent("", "把按钮改成蓝色", True))
        p = delegate_intake.plan("把按钮改成蓝色", _view(), llm)
        assert p.recognized and p.need == "project"
        assert "Alpha" in p.question and "哪个项目" in p.question

    def test_missing_brief_asks_for_it(self):
        llm = _LLM(DelegationIntent("Beta", "", True))
        p = delegate_intake.plan("改一下 Beta", _view(), llm)
        assert p.recognized and p.need == "brief"
        assert "Beta" in p.question and "做什么" in p.question

    def test_project_not_authorized_is_dropped_then_asked(self):
        # 模型抽出一个不在白名单里的名字：绝不能拿它去预填目录。
        llm = _LLM(DelegationIntent("Gamma", "改点东西", True))
        p = delegate_intake.plan("让它改 Gamma 改点东西", _view(), llm)
        assert p.need == "project" and not p.project and not p.directory

    def test_brief_is_stripped(self):
        llm = _LLM(DelegationIntent("Alpha", "  改点东西  ", True))
        p = delegate_intake.plan("x", _view(), llm)
        assert p.brief == "改点东西"

    def test_not_a_delegation(self):
        llm = _LLM(DelegationIntent("", "", False))
        p = delegate_intake.plan("今天天气怎么样", _view(), llm)
        assert p.reason == "not_delegation" and not p.recognized
        assert "不像是让我改代码" in p.question

    def test_falsy_intent_is_not_delegation(self):
        # DelegationIntent.__bool__ = is_delegation，但拿 None 也要兜住。
        p = delegate_intake.plan("x", _view(), _LLM(None))
        assert p.reason == "not_delegation"

    def test_no_projects_does_not_call_model(self):
        llm = _LLM(DelegationIntent("Alpha", "做点事", True))
        p = delegate_intake.plan("x", CapabilityView(), llm)
        assert p.reason == "no_projects" and not p.recognized
        assert llm.calls == 0

    def test_none_view_does_not_call_model(self):
        llm = _LLM(DelegationIntent("Alpha", "做点事", True))
        p = delegate_intake.plan("x", None, llm)
        assert p.reason == "no_projects" and llm.calls == 0

    def test_no_model_says_so(self):
        p = delegate_intake.plan("让它改 Alpha 做点事", _view(), None)
        assert p.reason == "no_model" and not p.recognized
        assert "没连模型" in p.question

    def test_model_error_is_reported_not_swallowed(self):
        p = delegate_intake.plan("让它改 Alpha 做点事", _view(), _LLM(error=RuntimeError))
        assert p.reason == "model_error" and not p.recognized
        assert "没能认出" in p.question

    def test_empty_text(self):
        p = delegate_intake.plan("   ", _view(), _LLM())
        assert p.reason == "empty" and "先说一句" in p.question


class TestCarryAnswers:
    """追问的下一句 = 确定规则消费，绝不再调模型。"""

    def test_brief_answer_is_taken_verbatim_without_model(self):
        llm = _LLM()
        p = delegate_intake.plan(
            "把登录页按钮改成蓝色并加阴影", _view(), llm,
            carry={"need": "brief", "project": "Alpha", "brief": ""},
        )
        assert p.brief == "把登录页按钮改成蓝色并加阴影"
        assert p.project == "Alpha" and p.directory == "D:/alpha"
        assert not p.need and p.recognized
        assert llm.calls == 0  # 追问不花钱

    def test_project_answer_matches_case_insensitively(self):
        llm = _LLM()
        p = delegate_intake.plan(
            "alpha", _view(), llm,
            carry={"need": "project", "project": "", "brief": "改点东西"},
        )
        assert p.project == "Alpha" and p.directory == "D:/alpha"
        assert p.brief == "改点东西" and not p.need
        assert llm.calls == 0

    def test_unmatched_project_asks_again_without_guessing(self):
        llm = _LLM()
        p = delegate_intake.plan(
            "那个我不知道的项目", _view(), llm,
            carry={"need": "project", "project": "", "brief": "改点东西"},
        )
        assert p.need == "project" and p.reason == "unmatched_project"
        assert not p.project and not p.directory
        assert llm.calls == 0

    def test_carry_directory_is_resolved_by_index(self):
        # Beta 是第二个项目，必须回查到 D:/beta 而不是第一个。
        p = delegate_intake.plan(
            "Beta", _view(), _LLM(),
            carry={"need": "project", "project": "", "brief": "做点事"},
        )
        assert p.directory == "D:/beta"


class RuleBasedProvider:
    """假规则层：名字与真实现一致，readiness 必须认出它不是可用模型。"""

    def parse_delegation(self, text, known_projects):
        raise AssertionError("规则层不该被当成可用模型调用")


class TestReadiness:
    def test_ready_when_projects_and_model_present(self):
        r = delegate_intake.readiness(_view(), _LLM())
        assert r.ready and r.projects and r.model
        assert not r.reason and not r.question

    def test_no_projects_blocks_even_with_model(self):
        r = delegate_intake.readiness(CapabilityView(), _LLM())
        assert not r.ready and not r.projects and r.model
        assert r.reason == "no_projects"

    def test_rule_based_provider_is_not_a_model(self):
        r = delegate_intake.readiness(_view(), RuleBasedProvider())
        assert not r.ready and r.projects and not r.model
        assert r.reason == "no_model"

    def test_probe_and_parse_agree_on_the_message(self):
        # 卡面与实际解析必须说同一句话，否则「卡上说没授权、点下去说没模型」。
        r = delegate_intake.readiness(CapabilityView(), _LLM())
        p = delegate_intake.plan("让它改 Alpha 做事", CapabilityView(), _LLM())
        assert r.reason == p.reason and r.question == p.question
        r2 = delegate_intake.readiness(_view(), RuleBasedProvider())
        p2 = delegate_intake.plan("让它改 Alpha 做事", _view(), RuleBasedProvider())
        assert r2.reason == p2.reason and r2.question == p2.question
