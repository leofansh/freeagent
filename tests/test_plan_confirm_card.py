"""确认卡（``plan_confirm``）的测试。

三条不能违反：

1. **点击才执行** —— 按钮 payload 里只带 ``choice``，**不带计划内容**
   （计划内容只印给人看）。理由：塞进payload 就等于让卡片成为第二份真源。
2. **版本必须是 JSON 1.0** —— 与选项卡/批准卡同一版本。官方错误码 200830
   规定 2.0 卡不能更新成 1.0，反之亦然。
3. **动作名必须独立** —— 不能复用 ``VIEW_CHOICE_ACTION``：那个点了跑只读
   视图，这个点了**建事务**。混用就说不清「点一下会不会动手」。
"""

from freeagent.feishu.sender import (
    PLAN_CONFIRM_ACTION,
    VIEW_CHOICE_ACTION,
    plan_confirm_card,
)


def _card():
    return plan_confirm_card("确认执行", ["第一件事", "第二件事"], chat_id="oc_x")


def test_action_is_distinct_from_view_choice():
    """确认卡点了**会动手**，只读选项卡不会 —— 动作名不能混。"""
    assert PLAN_CONFIRM_ACTION != VIEW_CHOICE_ACTION
    card = _card()
    values = [b["value"] for e in card["elements"]
              if e.get("tag") == "action" for b in e["actions"]]
    assert values and all(v["action"] == PLAN_CONFIRM_ACTION
                          for v in values), \
        "确认卡的按钮还在用只读视图的动作名"


def test_buttons_do_not_carry_the_plan():
    """按钮 payload **不带计划内容** —— 计划只有Repl._plan 那一份。"""
    card = _card()
    values = [b["value"] for e in card["elements"]
              if e.get("tag") == "action" for b in e["actions"]]
    for v in values:
        assert set(v) == {"action", "choice", "chat"}, \
            f"payload 里混进了计划内容：{sorted(v)}"
        blob = repr(v)
        assert "第一件事" not in blob, "计划内容进了 payload"


def test_two_choices_ok_and_cancel():
    card = _card()
    btns = [b for e in card["elements"] if e.get("tag") == "action"
            for b in e["actions"]]
    assert [b["value"]["choice"] for b in btns] == ["ok", "cancel"]
    # 第一个是主按钮：确认是最可能的意图
    assert btns[0]["type"] == "primary"
    assert btns[1]["type"] == "default"


def test_card_is_json_10():
    """JSON 1.0：顶层有 ``elements``、**没有** ``schema``。

    与其它卡混用版本会让飞书报 200830 —— 而那个错只在**更新**时才暴露，
    首次发送看起来完全正常。
    """
    card = _card()
    assert "elements" in card and isinstance(card["elements"], list)
    assert "schema" not in card, "这是 2.0 卡，会与批准/选项卡冲突"


def test_plan_is_shown_for_human_review():
    """计划**必须印在卡面上** —— 12.7.2 要求用户能核对再批准。"""
    blob = repr(_card())
    assert "第一件事" in blob and "第二件事" in blob
    assert "确认后才会动手" in blob, "要明说「点了会动手」"


def test_empty_plan_is_refused():
    """空计划不该发卡 —— 那等于让用户点一个没有内容的东西。"""
    import pytest
    with pytest.raises(ValueError):
        plan_confirm_card("确认", [], chat_id="oc_x")


def test_button_label_within_feishu_limit():
    """按钮文案有长度上限，超了会被截成看不懂的样子。"""
    card = plan_confirm_card("确认", ["很长" * 60], chat_id="oc_x")
    btns = [b for e in card["elements"] if e.get("tag") == "action"
            for b in e["actions"]]
    for b in btns:
        assert len(b["text"]["content"]) <= 40