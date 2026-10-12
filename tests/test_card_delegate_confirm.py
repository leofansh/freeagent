"""委派简报的提示卡（第 5 步）与确认卡。

## 为什么专门测「载荷里没有项目 / 简报」

按钮 ``value`` 是飞书客户端回传的，**用户可以伪造**（转发卡片、手改 JSON）。
一旦把项目路径、模型或简报原文塞进载荷，任何人构造一个 ``value`` 就能建一条
指向任意目录的委派 —— 那正好绕过项目白名单这唯一一道闸门。
所以设计是：载荷**只说点了哪个按钮**，具体要委派什么由桥接从内存状态里取。
:func:`test_payload_has_exactly_three_keys` 与
:func:`test_payload_never_carries_project_or_brief` 就是在守这条纪律。

## 为什么结构也要测

这两张卡与选卡同族，必须都是 JSON 1.0：官方错误码 200830 规定 2.0 卡不能
更新成 1.0，反之亦然。版本写错时**发出去完全正常**，点一下才失败 ——
所以只有结构断言能提前抓住它。
"""

from __future__ import annotations

import json

import pytest

from freeagent.feishu.sender import (
    BRIEF_CONFIRM_ACTION,
    COMMAND_ACTION,
    MENU_ACTION,
    PLAN_CONFIRM_ACTION,
    SELECT_ACTION,
    VIEW_CHOICE_ACTION,
    BriefPreview,
    FeishuError,
    brief_confirm_card,
    brief_prompt_card,
    send_brief_confirm_card,
    send_brief_prompt_card,
)

#: 载荷键集合 —— **恰好**这三个。多一个都是设计事故。
_PAYLOAD_KEYS = {"action", "choice", "chat"}

#: 绝不许出现在载荷里的词（大小写无关）。
#: ``project`` / ``brief`` / ``model`` / ``role`` 这几个就是「内容进了载荷」
#: 的同义词，钉住它们比只断言键集合更能表达意图。
_BANNED_IN_PAYLOAD = ("project", "brief", "model", "role", "agent", "variant")


def _preview() -> BriefPreview:
    """各项**刻意互不相同**，免得断言把「回显了项目」误判成「回显了模型」。"""
    return BriefPreview(
        project="D:/p/freeagent",
        agent="Sisyphus - ultraworker",
        model="opencode/big-pickle",
        variant="high",
        role="工作项目A",
        brief="修一下登录页在窄屏下的按钮错位",
    )


def _buttons(card: dict) -> list[dict]:
    """取卡里所有按钮。不写死 ``elements[i]`` —— 正文行数一变下标就错。"""
    return [btn for el in card["elements"] if el.get("tag") == "action"
            for btn in el["actions"]]


def _confirm_body(card: dict) -> str:
    return "\n".join(el["text"]["content"] for el in card["elements"]
                     if "text" in el)


class _FakeSender:
    """只记下发出去的卡，不碰网络。"""

    def __init__(self, response: bytes = b'{"code":0,"data":'
                                           b'{"message_id":"om_brief"}}') -> None:
        self.cards: list[dict] = []
        self._response = response

    def _receive_id_type(self, open_id: str) -> str:
        return "open_id"

    class config:
        base_url = "https://open.feishu.cn"

    timeout = 5

    def token(self) -> str:
        return "t"

    def _transport(self, url, payload, headers, timeout):
        self.cards.append(json.loads(payload["content"]))
        return self._response


# ── 结构：两张卡都必须是 JSON 1.0 ──────────────────────────────────────── #

def test_prompt_card_is_json_1_0():
    card = brief_prompt_card("交个简报（5/5）", chat_id="oc_1")
    assert card["config"]["wide_screen_mode"] is True
    assert "elements" in card
    assert "body" not in card and "schema" not in card


def test_confirm_card_is_json_1_0():
    card = brief_confirm_card("确认", _preview(), chat_id="oc_1")
    assert card["config"]["wide_screen_mode"] is True
    assert "elements" in card
    assert "body" not in card and "schema" not in card


# ── 载荷纪律：恰好三个键，且不含内容、不含凭据 ─────────────────────────── #

def test_payload_has_exactly_three_keys():
    """载荷恰好 ``{action, choice, chat}`` —— 这是安全边界，不是风格。"""
    cards = [
        brief_prompt_card("t", chat_id="oc_1"),
        brief_confirm_card("t", _preview(), chat_id="oc_1"),
    ]
    for card in cards:
        for btn in _buttons(card):
            assert set(btn["value"]) == _PAYLOAD_KEYS, (
                f"载荷键集合不是恰好三个：{btn['value']!r}"
            )


def test_payload_never_carries_project_or_brief():
    """项目 / 简报 / 模型 / 角色这些词**一个都不许当载荷的键**。

    载荷可伪造 —— 放这些字段就等于把「委派去哪、做什么」交给客户端。
    （``action`` 的*值* ``brief_confirm`` 里含 ``brief`` 是常量名，不是字段，
    所以下面查的是**键**，而内容则用真值是否泄进来单独钉。）
    """
    preview = _preview()
    cards = [
        brief_prompt_card("t", chat_id="oc_1"),
        brief_confirm_card("t", preview, chat_id="oc_1"),
    ]
    for card in cards:
        for btn in _buttons(card):
            keys = {k.lower() for k in btn["value"]}
            for banned in _BANNED_IN_PAYLOAD:
                assert banned not in keys, (
                    f"载荷里出现了 {banned!r} 字段：{btn['value']!r}"
                )
            # 内容真值也不许泄进载荷（键换了名字也不行）。
            content = json.dumps(btn["value"], ensure_ascii=False)
            for real in (preview.project, preview.model, preview.role,
                         preview.brief):
                assert real not in content, (
                    f"内容 {real!r} 泄进了载荷：{content}"
                )


def test_payload_has_no_credential_id():
    """确认卡**不是**凭据型卡片 —— 载荷里没有 ``id``（那是批准卡的凭据字段）。"""
    card = brief_confirm_card("t", _preview(), chat_id="oc_1")
    for btn in _buttons(card):
        assert "id" not in btn["value"], "简报确认卡不该带凭据 id"


def test_payload_carries_action_choice_and_chat():
    """三个键的值都对得上：动作是简报 action，chat 是传入的会话。"""
    card = brief_confirm_card("t", _preview(), chat_id="oc_77")
    choices = {btn["value"]["choice"] for btn in _buttons(card)}
    assert choices == {"create", "retry"}
    for btn in _buttons(card):
        assert btn["value"]["action"] == BRIEF_CONFIRM_ACTION
        assert btn["value"]["chat"] == "oc_77"

    prompt = brief_prompt_card("t", chat_id="oc_77")
    assert _buttons(prompt)[0]["value"]["choice"] == "cancel"


# ── 常量：必须与既有五个都不同值 ──────────────────────────────────────── #

def test_brief_action_is_distinct_from_existing_actions():
    """两个 action 同名会让「哪个 choice 能当命令执行」失去单一判据。"""
    existing = {
        MENU_ACTION,
        SELECT_ACTION,
        PLAN_CONFIRM_ACTION,
        COMMAND_ACTION,
        VIEW_CHOICE_ACTION,
    }
    assert len(existing) == 5, "既有五个常量本身就重名了"
    assert BRIEF_CONFIRM_ACTION not in existing


# ── 按钮文案长度上限 ──────────────────────────────────────────────────── #

def test_button_labels_are_within_limit():
    """飞书按钮文案上限 40 字，超了被截成看不懂的东西。"""
    cards = [
        brief_prompt_card("t", chat_id="oc_1"),
        brief_confirm_card("t", _preview(), chat_id="oc_1"),
    ]
    for card in cards:
        for btn in _buttons(card):
            content = btn["text"]["content"]
            assert len(content) <= 40, f"按钮文案过长：{content!r}"


# ── 回显：确认卡真的把内容印出来了 ────────────────────────────────────── #

def test_confirm_card_echoes_project_model_and_brief():
    preview = _preview()
    body = _confirm_body(brief_confirm_card("t", preview, chat_id="oc_1"))
    assert preview.project in body, "确认卡没回显项目名"
    assert preview.model in body, "确认卡没回显模型名"
    assert preview.brief in body, "确认卡没回显简报原文"


def test_role_line_is_a_preview_not_a_verdict():
    """脉络那行必须是「按现有规则会归到」这种**预览**措辞，不是确定归属。"""
    body = _confirm_body(brief_confirm_card("t", _preview(), chat_id="oc_1"))
    assert "按现有规则会归到" in body, "脉络被写成了确定归属，而不是预览"
    assert _preview().role in body


# ── 发送函数：照 send_select_card 的口径 ──────────────────────────────── #

def test_send_prompt_card_returns_message_id():
    sender = _FakeSender()
    got = send_brief_prompt_card(
        sender, open_id="ou_x", subject="t", chat_id="oc_1")
    assert got == "om_brief"
    assert len(sender.cards) == 1, "没发出卡"


def test_send_confirm_card_returns_message_id():
    sender = _FakeSender()
    got = send_brief_confirm_card(
        sender, open_id="ou_x", subject="t", preview=_preview(), chat_id="oc_1")
    assert got == "om_brief"
    assert len(sender.cards) == 1


def test_send_requires_recipient():
    """没有收件人标识就直接拒绝 —— 发出去也会被飞书拒。"""
    sender = _FakeSender()
    with pytest.raises(ValueError):
        send_brief_prompt_card(sender, open_id="", subject="t", chat_id="oc_1")
    with pytest.raises(ValueError):
        send_brief_confirm_card(
            sender, open_id="", subject="t", preview=_preview(), chat_id="oc_1")


def test_send_raises_on_nonzero_business_code():
    """飞书 HTTP 200 不代表成功 —— 业务码非 0 必须抛。"""
    sender = _FakeSender(response=b'{"code":230002,"msg":"boom"}')
    with pytest.raises(FeishuError):
        send_brief_confirm_card(
            sender, open_id="ou_x", subject="t", preview=_preview(), chat_id="oc_1")
