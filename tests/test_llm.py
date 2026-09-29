"""LLM 抽象层与规则实现测试。"""

from __future__ import annotations

import pytest

from freeagent.services.llm import (
    ClassificationResult,
    LLMProvider,
    RoleHint,
    RuleBasedProvider,
    SignalRef,
    TaskRef,
    default_provider,
)
from freeagent.services.llm.rules import ROLE_MATCH_THRESHOLD

HINTS = [
    RoleHint("工作项目A", "销售 周报 客户"),
    RoleHint("家庭", "孩子 学校 材料"),
    RoleHint("跑腿杂项", "窗户 螺丝 修"),
]


@pytest.fixture()
def p() -> RuleBasedProvider:
    return RuleBasedProvider()


def test_default_provider_satisfies_protocol():
    assert isinstance(default_provider(), LLMProvider)


# ------------------------------------------------------------------- kind
@pytest.mark.parametrize(
    "text,expected",
    [
        ("帮我盯一下下周五要交的销售周报初稿", "action"),
        ("下周二要交的销售周报初稿，先理一版", "action"),
        ("写一份季度总结", "action"),
        ("别忘了提醒我下午三点修窗户螺丝", "reminder"),
        ("记得提醒我交物业费", "reminder"),
        ("等对方回复合同条款", "wait"),
        ("等客户的结果", "wait"),
        ("盯着审批", "wait"),
    ],
)
def test_kind_inference(p, text, expected):
    assert p.classify(text, HINTS).kind == expected


def test_wait_beats_reminder(p):
    """「别忘了等对方回复」本质是「在等」，按 WAIT 优先。"""
    assert p.classify("别忘了等对方回复", HINTS).kind == "wait"


# ------------------------------------------------------------------- roles
def test_role_match_avoids_clarification(p):
    r = p.classify("帮我盯一下下周五要交的销售周报初稿", HINTS)
    assert r.need_clarification is False
    assert r.role_guesses[0].role_name == "工作项目A"
    assert r.role_guesses[0].confidence >= ROLE_MATCH_THRESHOLD


def test_role_match_by_note_tokens(p):
    r = p.classify("别忘了提醒我下午三点修窗户螺丝", HINTS)
    assert r.need_clarification is False
    assert r.role_guesses[0].role_name == "跑腿杂项"


def test_ambiguous_role_asks(p):
    r = p.classify("随便弄一下", [RoleHint("甲"), RoleHint("乙")])
    assert r.need_clarification is True
    assert r.clarifying_question


def test_no_roles_asks_which_context(p):
    r = p.classify("随便弄一下", [])
    assert r.need_clarification is True
    assert r.clarifying_question == "这是放到哪个脉络里？"


def test_clarifying_question_names_candidates(p):
    r = p.classify("学校要交的材料", HINTS)
    assert r.need_clarification is False
    assert r.role_guesses[0].role_name == "家庭"


def test_guesses_sorted_by_confidence(p):
    r = p.classify("销售周报和孩子的学校材料都要处理", HINTS)
    scores = [g.confidence for g in r.role_guesses]
    assert scores == sorted(scores, reverse=True)
    assert len(r.role_guesses) <= 3


def test_confidence_is_bounded(p):
    r = p.classify("销售 周报 客户 孩子 学校 材料 窗户 螺丝 修", HINTS)
    for g in r.role_guesses:
        assert 0.0 <= g.confidence <= 1.0


# ------------------------------------------------------------------- title
def test_refine_title_strips_fillers_and_tail(p):
    t = p.refine_title("帮我记一下 下周二要交的销售周报初稿，先理一版。")
    assert t == "下周二要交的销售周报初稿"


def test_refine_title_length_cap(p):
    t = p.refine_title("帮我" + "很长的一个标题" * 20)
    assert len(t) <= 41
    assert t.endswith("…")


def test_refine_title_never_empty(p):
    assert p.refine_title("帮我") != ""
    assert p.refine_title("   ") != ""


# ------------------------------------------------------------------- steps
def test_split_steps_splits_connectives(p):
    task = TaskRef(id="x", title="写季度总结",
                   intent="先收集数据，然后写初稿，接着找人review，最后定稿")
    steps = p.split_steps(task)
    assert len(steps) >= 2
    assert len(set(steps)) == len(steps), "不应有重复步骤"


def test_split_steps_falls_back_to_scaffold(p):
    steps = p.split_steps(TaskRef(id="x", title="干活", intent="整一下"))
    assert len(steps) == 3


def test_split_steps_uses_dod_when_no_intent(p):
    task = TaskRef(id="x", title="t", definition_of_done="能给领导看")
    steps = p.split_steps(task)
    assert any("能给领导看" in s for s in steps)


# ------------------------------------------------------------------- draft
def test_draft_marks_unknowns_as_todo(p):
    d = p.draft(TaskRef(id="x", title="写季度总结", kind="action"), "先理一版")
    assert "[TODO]" in d
    assert "本次要求" in d
    assert "本内容由规则引擎生成的结构化草稿" in d


def test_draft_does_not_invent_facts(p):
    d = p.draft(TaskRef(id="x", title="写季度总结", intent=None), "")
    assert "12345" not in d
    assert "%" not in d


def test_draft_asks_waiting_target_for_wait_kind(p):
    d = p.draft(TaskRef(id="x", title="等回复", kind="wait"), "")
    assert "在等谁" in d


# ------------------------------------------------------------------- schedule
def test_suggest_schedule_lists_signals(p):
    text = p.suggest_schedule(
        TaskRef(id="x", title="t"),
        [SignalRef("overdue_wait", 50, "该跟进了"), SignalRef("scheduled_today", 10, "排在今天")],
    )
    assert "该跟进了" in text and "排在今天" in text
    assert "由你决定" in text


def test_suggest_schedule_without_signals(p):
    text = p.suggest_schedule(TaskRef(id="x", title="t"), [])
    assert "由你决定" in text


# ------------------------------------------------------------------- determinism
def test_classify_is_deterministic(p):
    a = p.classify("帮我盯一下下周五要交的销售周报初稿", HINTS)
    b = p.classify("帮我盯一下下周五要交的销售周报初稿", HINTS)
    assert a == b


def test_refine_title_is_deterministic(p):
    text = "帮我记一下 下周二要交的销售周报初稿，先理一版。"
    assert p.refine_title(text) == p.refine_title(text)


def test_result_type(p):
    assert isinstance(p.classify("x", HINTS), ClassificationResult)
