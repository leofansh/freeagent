"""白名单的多层 ID 匹配（第一性原理那一轮的落地）。

## 为什么有这组测试

白名单是「谁能指挥本机执行代码」的边界。它曾经**只认 open_id**，于是：

- 用户填了 ``user_id``，**静默无效** —— 界面上看不出、日志里也只有一句
  「不在白名单」，而「配了却不灵」只能靠翻代码定位；
- 换飞书应用后 ``open_id`` 变了，白名单静默失效。

这两条的共同根因是「只认一层身份」。修法是**事件带了哪几层就比哪几层**。

## 判据只有一条

身份必须**由事件本身携带、可被验证**。所以：

- ✅ 三层 id 逐一求交集
- ❌ 名字匹配（可重名、可冒用）—— 必须被挡住
"""

from __future__ import annotations

import json

import pytest

from freeagent.feishu.events import IncomingMessage, parse_event
from freeagent.services.channel import ChannelService

OPEN = "ou_alice_app1"
USER = "2d1b7bec"
UNION = "on_8xkz2m"


def _msg(
    *,
    event_id: str = "e1",
    chat_id: str = "oc_1",
    sender_open_id: str = "",
    sender_user_id: str = "",
    sender_union_id: str = "",
    text: str = "hi",
    is_group: bool = False,
    mentioned: bool = True,
) -> IncomingMessage:
    """造一条消息。字段类型逐一标好，不靠 ``**dict`` 合并。

    早先的写法是 ``_DEFAULTS | kw`` 合并：那样 ``sender_open_id`` 会同时出现
    两次（默认值 + 覆盖），运行时直接 ``TypeError: got multiple values``，
    而类型检查也只能整体挂一个 ``type: ignore`` —— 显式参数两条都避免。
    """
    return IncomingMessage(
        event_id=event_id, chat_id=chat_id,
        sender_open_id=sender_open_id,
        sender_user_id=sender_user_id,
        sender_union_id=sender_union_id,
        text=text, is_group=is_group, mentioned=mentioned,
    )


# --- 事件侧：三层都要带出来 ---------------------------------------------- #


def _event(sender_id: dict[str, str], text: str = "hi") -> dict[str, object]:
    """造一条真实形状的事件。``parse_event`` 要的就是这个 dict 结构。"""
    inner: dict[str, object] = {
        "chat_id": "oc_1", "chat_type": "p2p", "message_type": "text",
        "content": json.dumps({"text": text}), "mentions": [],
        "message_id": "m1",
    }
    return {
        "header": {"event_id": "e1", "event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": sender_id},
            "message": inner,
        },
    }


def test_all_three_ids_are_captured():
    """**回归**：原先归一化时只抄 open_id，user_id 在那一步就丢了。"""
    got = parse_event(_event({"open_id": OPEN, "user_id": USER, "union_id": UNION}))
    assert isinstance(got, IncomingMessage)
    assert got.sender_open_id == OPEN
    assert got.sender_user_id == USER
    assert got.sender_union_id == UNION


def test_message_with_only_user_id_is_not_dropped():
    """**回归**：原先「缺 open_id」直接 Ignored，整条消息在解析阶段就没了。

    症状极其误导：日志只有一句「缺 open_id」，看起来像「什么都没收到」。
    而 user_id 恰恰是跨应用稳定的那一层 —— 丢掉它等于丢掉最该留的。
    """
    got = parse_event(_event({"user_id": USER}))
    assert isinstance(got, IncomingMessage), "只带 user_id 的消息被整条丢掉了"
    assert got.sender_user_id == USER
    assert got.sender_ids == frozenset({USER})


def test_message_with_no_ids_at_all_is_still_ignored():
    """一个标识都没有 = 无法验证身份，必须拒（而不是放行）。"""
    got = parse_event(_event({}))
    assert not isinstance(got, IncomingMessage)
    assert "没有任何身份标识" in getattr(got, "reason", "")


def test_sender_ids_collects_every_layer():
    assert _msg(sender_open_id=OPEN, sender_user_id=USER).sender_ids == frozenset(
        {OPEN, USER}
    )


def test_sender_ids_skips_empty_layers():
    """空串不能算一个 id —— 否则空值会意外命中白名单里的空条目。"""
    assert _msg(sender_open_id=OPEN).sender_ids == frozenset({OPEN})


def test_sender_label_names_every_layer():
    """日志要能一眼看出「哪一层对不上」，不能只显示一层。"""
    label = _msg(sender_open_id=OPEN, sender_user_id=USER).sender_label
    assert OPEN in label and USER in label
    assert "open_id=" in label and "user_id=" in label


# --- 匹配侧：任一层命中即放行 -------------------------------------------- #


def _svc(allowed: set[str]) -> ChannelService:
    svc = ChannelService.__new__(ChannelService)   # 绕开 App 依赖
    svc.allowed = frozenset(allowed)
    return svc


@pytest.mark.parametrize("allow", [OPEN, USER, UNION])
def test_any_single_layer_grants_access(allow):
    """核心规则：填哪一层都能认。"""
    svc = _svc({allow})
    assert svc.is_allowed(frozenset({OPEN, USER, UNION})) is True


def test_open_id_only_allowlist_still_works():
    """向后兼容：原来只填 open_id 的配置不该失效。"""
    svc = _svc({OPEN})
    assert svc.is_allowed(frozenset({OPEN, USER})) is True


def test_unrelated_ids_are_refused():
    svc = _svc({OPEN})
    assert svc.is_allowed(frozenset({"ou_someone_else", "deadbeef"})) is False


def test_plain_string_still_accepted():
    """``is_allowed`` 也收单个字符串（老调用点不必全改）。"""
    assert _svc({OPEN}).is_allowed(OPEN) is True


def test_empty_candidates_are_refused():
    """空集合不能因为「交集为空」而被当成放行。"""
    assert _svc(set()).is_allowed(frozenset()) is False
    assert _svc({""}).is_allowed(frozenset({"", ""})) is False


def test_a_name_in_the_message_text_never_authorizes():
    """**安全约束，走真实路径**。

    名字不是身份 —— 可重名、可冒用。所以「正文里提到了 Alice，而 Alice 在
    白名单里」绝不能因此放行。

    这里刻意用 ``parse_event`` 走完整路径，而不是直接给 ``is_allowed`` 塞
    一个 ``{"Alice"}``：后者在真实系统里**不可能发生**（``sender_ids`` 只装
    飞书的三层 id），测它等于在验证一个虚构场景。真正的风险是「正文里的
    名字被当成了身份」，而那要从事件解析那一层才测得到。
    """
    got = parse_event(_event(
        {"open_id": "ou_stranger", "user_id": "ffffffff"},
        text="Alice 帮我看下这个",
    ))
    assert isinstance(got, IncomingMessage)

    svc = _svc({"Alice", "Alice 的待办", "Carol"})
    # 白名单里**只有名字**，事件里是真正的 id —— 名字不该放行任何人
    assert svc.is_allowed(got.sender_ids) is False


def test_sender_ids_only_ever_holds_identifier_fields():
    """``sender_ids`` 只能由那三个 id 字段构成 —— 这是「名字进不来」的根本保证。

    上面那条测的是结果，这条测的是**结构**：白名单匹配的数据源里压根没有
    「名字」这个位置。结构对了，结果就不会错。
    """
    got = parse_event(_event(
        {"open_id": OPEN, "user_id": USER}, text="Bob Alice Carol"
    ))
    assert isinstance(got, IncomingMessage)
    assert got.sender_ids == frozenset({OPEN, USER})
    for name in ("Bob", "Alice", "Carol"):
        assert name not in got.sender_ids


# --- 排查信息 ------------------------------------------------------------ #


def test_mismatch_message_shows_both_sides():
    """「不在白名单」四个字是排查死路 —— 要能看出谁变了。"""
    svc = _svc({OPEN})
    detail = svc.allowlist_mismatch(frozenset({"ou_new", "2d1b7bec"}))
    assert "ou_new" in detail and "2d1b7bec" in detail
    assert OPEN in detail


def test_mismatch_message_survives_an_empty_allowlist():
    """白名单为空是「刚装好」的常态，那句话不能因此崩掉。"""
    detail = _svc(set()).allowlist_mismatch(frozenset({OPEN}))
    assert OPEN in detail


def test_mismatch_message_never_contains_message_text():
    """只回答「你是谁」，不碰「你说了什么」。"""
    svc = _svc({OPEN})
    detail = svc.allowlist_mismatch(frozenset({OPEN}))
    assert OPEN in detail
