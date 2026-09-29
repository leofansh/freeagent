"""卡片**视觉反馈**的守卫 —— 你点完之后必须看得见结果。

要锁的不是「返回了什么」而是三件事：

1. **卡片会被替换**（``card`` 字段在）—— 不然点了跟没点一样
2. **按钮消失** —— 留着可点的按钮是骗人
3. **第二次点击明说「没生效」** —— 决策不可翻，但用户必须知道
"""
from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from freeagent.feishu import bridge as bridge_mod
from freeagent.services.approval import ApprovalStore
from freeagent.storage.db import connect, init_schema


class FakeClock:
    def __init__(self, t):
        self.now = t

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += datetime.timedelta(**kw)


@pytest.fixture
def wired(tmp_path):
    conn = connect(Path(tmp_path) / "a.db")
    init_schema(conn)
    old_conn, old_clock = bridge_mod._card_conn, bridge_mod._card_clock
    clock = FakeClock(datetime.datetime(2026, 9, 29, 12, 0))
    bridge_mod._card_conn = conn
    # **必须把同一个 clock 也给处理器。** 它自己建 ApprovalStore，
    # 默认用真钟 —— 于是「新卡」会被判成过期，decided_by 写成 expired，
    # 看起来像「决策被翻掉」，真因只是两个 store 用了两个钟。
    bridge_mod._card_clock = clock
    try:
        yield conn, ApprovalStore(conn, clock=clock), clock
    finally:
        bridge_mod._card_conn, bridge_mod._card_clock = old_conn, old_clock


def payload(cred: str, choice: str, who: str = "ou_张三") -> dict:
    return {
        "header": {"event_id": "ev-1"},
        "event": {
            "operator": {"open_id": who},
            "action": {"value": {"action": choice, "id": cred}},
        },
    }


def has_buttons(card: dict) -> bool:
    for el in card.get("elements", []):
        if el.get("tag") == "action":
            return True
    return False


def card_of(resp: dict) -> dict:
    """从应答里取出**卡片本体**。

    应答的 ``card`` 是 ``{type, data}``，卡片在 ``data`` 里。测试里到处
    写 ``resp["card"]["header"]`` 会 KeyError —— 那不是代码有 bug，是测试
    把**包装层**当成了卡片本身。这里统一走这一层，顺带让「包装形状错」
    这类问题在测试里也看得见。

    踩过的坑：用正则批量改写 ``resp["card"]`` 时，把**这个函数体内**
    的两处也一起改了，于是它变成自己调自己 → RecursionError。
    批量改写一定要把「定义处」排除掉。
    """
    envelope = resp["card"]
    assert envelope["type"] == "raw", "card 必须声明 type=raw"
    return envelope["data"]


def all_text(card: dict) -> str:
    import json
    return json.dumps(card, ensure_ascii=False)


class TestAllowFeedback:
    def test_card_is_replaced(self, wired):
        _c, store, _k = wired
        item = store.ask("只读列出目录 D:/x")
        resp = bridge_mod._card_action(payload(item.credential, "allow_once"))
        assert "card" in resp, "没返回卡片 —— 点了跟没点一样"

    def test_buttons_are_gone(self, wired):
        """按钮必须消失。留着它 = 用户以为还能再选。"""
        _c, store, _k = wired
        item = store.ask("只读列出目录 D:/x")
        resp = bridge_mod._card_action(payload(item.credential, "allow_once"))
        assert not has_buttons(card_of(resp)), "已处理的卡片还留着按钮"

    def test_header_goes_green_and_says_allowed(self, wired):
        _c, store, _k = wired
        item = store.ask("只读列出目录 D:/x")
        card = card_of(bridge_mod._card_action(payload(item.credential, "allow_once")))
        assert card["header"]["template"] == "green"
        assert "已允许" in all_text(card)

    def test_names_who_clicked(self, wired):
        """「谁批的」要出现在卡上 —— 出事时用户自己就能对上。"""
        _c, store, _k = wired
        item = store.ask("x")
        card = bridge_mod._card_action(
            payload(item.credential, "allow_once", who="ou_老王"))["card"]
        assert "ou_老王" in all_text(card)

    def test_keeps_the_scope_text(self, wired):
        """保留原文：事后回看这张卡，还知道当时批准的是什么。"""
        _c, store, _k = wired
        item = store.ask("x", detail="目录：`D:/secret`\n授权范围：仅这一次")
        card = card_of(bridge_mod._card_action(payload(item.credential, "allow_once")))
        assert "D:/secret" in all_text(card)

    def test_toast_is_success(self, wired):
        _c, store, _k = wired
        item = store.ask("x")
        resp = bridge_mod._card_action(payload(item.credential, "allow_once"))
        assert resp["toast"]["type"] == "success"


class TestDenyFeedback:
    def test_header_goes_red(self, wired):
        _c, store, _k = wired
        item = store.ask("x")
        card = card_of(bridge_mod._card_action(payload(item.credential, "deny")))
        assert card["header"]["template"] == "red"
        assert "已拒绝" in all_text(card)

    def test_buttons_gone(self, wired):
        _c, store, _k = wired
        item = store.ask("x")
        card = card_of(bridge_mod._card_action(payload(item.credential, "deny")))
        assert not has_buttons(card)

    def test_says_nothing_was_accessed(self, wired):
        """"没访问任何东西"是**给用户的保证** —— 他拒绝就是为了这个。"""
        _c, store, _k = wired
        item = store.ask("x")
        card = card_of(bridge_mod._card_action(payload(item.credential, "deny")))
        assert "没有访问" in all_text(card)


class TestSecondClickToldHonestly:
    def test_second_click_says_it_did_not_take_effect(self, wired):
        _c, store, _k = wired
        item = store.ask("x")
        bridge_mod._card_action(payload(item.credential, "allow_once", who="ou_第一次"))
        resp = bridge_mod._card_action(payload(item.credential, "deny", who="ou_第二次"))
        card = card_of(resp)
        text = all_text(card)
        assert "没有生效" in text, (
            f"第二次点击必须明说没生效：{text[:200]}"
        )
        assert "ou_第一次" in text, "要说清先前是谁决定的"

    def test_second_click_does_not_flip_the_decision(self, wired):
        _c, store, _k = wired
        item = store.ask("x")
        bridge_mod._card_action(payload(item.credential, "allow_once"))
        bridge_mod._card_action(payload(item.credential, "deny"))
        assert store.decide(item.credential) == "allow", (
            "第二次点击把决策翻掉了 —— 用过的卡片变成长效授权"
        )

    def test_second_click_toast_is_warning_not_success(self, wired):
        """成功提示会骗人：这次点击其实没生效。"""
        _c, store, _k = wired
        item = store.ask("x")
        bridge_mod._card_action(payload(item.credential, "allow_once"))
        resp = bridge_mod._card_action(payload(item.credential, "allow_once"))
        assert resp["toast"]["type"] == "warning", (
            "没生效却弹「成功」—— 那比不提示更坏"
        )


class TestExpiredCardFeedback:
    def test_expired_click_says_expired(self, wired):
        _c, store, _k = wired
        item = store.ask("x", ttl_seconds=1)
        _c, _s, k = wired
        k.advance(seconds=5)
        resp = bridge_mod._card_action(payload(item.credential, "allow_once"))
        assert "过期" in all_text(card_of(resp))
        assert not has_buttons(card_of(resp))


class TestResponseEnvelopeShape:
    """应答体必须符合官方「卡片回传交互」文档的形状。

    这两条都是**实测踩出来的**，且症状都是「用户点了就看到出错了」，
    而我们侧一切正常（handler 跑完、记了日志、回了应答）—— 极难自查。

    官方错误码对应：
    - 200672 响应体格式错误
    - 200673 卡片错误
    - 200830 **2.0 卡不能更新成 1.0**（反之亦然）
    """

    def test_card_is_wrapped_in_type_data(self, wired):
        """``card`` 必须是 ``{type: "raw", data: {...}}``，不能是裸卡片。

        我原先直接回裸卡片（``{config, header, elements}``）—— 那是
        **发送**的形状。文档明写 ``card.type`` 必填。
        """
        _c, store, _k = wired
        item = store.ask("x")
        resp = bridge_mod._card_action(payload(item.credential, "allow_once"))
        # 断言**包装层**本身：card_of() 已经把 data 剥掉了，
        # 所以这里直接看 resp["card"]。
        assert resp["card"]["type"] == "raw", "card 必须声明 type=raw"
        assert "data" in resp["card"], "card 必须用 data 包住卡片 JSON"

    def test_card_data_is_not_a_bare_card(self, wired):
        """``card.data`` 才是卡片本体 —— 别把包装层当卡片本身。"""
        _c, store, _k = wired
        item = store.ask("x")
        data = bridge_mod._card_action(
            payload(item.credential, "allow_once"))["card"]["data"]
        assert "header" in data, "data 里才是卡片结构"


class TestVersionMustMatchSentCard:
    """发出的卡与更新的卡**必须同版本**（官方 200830）。

    这一条是纯结构检查，但它锁的正是刚才那次线上失败：我们发的是
    **1.0**（``config.wide_screen_mode`` + 顶层 ``elements``），
    更新时却写成 **2.0**（``schema:2.0`` + ``body.elements``），
    版本对不上 → 整条应答判失败 → 用户看到「出错了」。
    """

    def test_both_cards_are_1_0(self):
        from freeagent.feishu.sender import _approval_card, _decided_card

        sent = _approval_card("主题", "细节", "ap-x")
        decided = _decided_card("主题", "已允许", granted=True)
        for name, card in (("发出的", sent), ("已处理的", decided)):
            assert "schema" not in card, (
                f"{name}卡里出现了 schema —— 那是 2.0 的标志，与另一张不一致"
            )
            assert "body" not in card, (
                f"{name}卡里出现了 body —— 2.0 才用 body.elements"
            )
            assert "config" in card and "elements" in card, (
                f"{name}卡不像 1.0 结构（应有顶层 config/elements）"
            )

    def test_decided_card_has_no_action_element(self):
        from freeagent.feishu.sender import _decided_card

        card = _decided_card("主题", "已允许", granted=True)
        assert not any(e.get("tag") == "action" for e in card["elements"]), (
            "已处理的卡不该还留着可点的按钮"
        )


class TestNoConnectionFeedback:
    def test_no_connection_says_refused_not_allow(self, wired):
        conn, store, _k = wired
        item = store.ask("x")
        old = bridge_mod._card_conn
        bridge_mod._card_conn = None
        try:
            card = card_of(bridge_mod._card_action(payload(item.credential, "allow_once")))
        finally:
            bridge_mod._card_conn = old
        assert "已拒绝" in all_text(card), (
            "没连上数据库却显示别的 —— 必须说清没批准"
        )
        assert store.decide(item.credential) is None


class TestGarbageHasNoCard:
    def test_unrecognizable_payload_does_not_invent_a_card(self, wired):
        """载荷都认不出来时**不能凭空造一张卡** ——
        用户会以为之前那张真的被处理过。只给 toast。"""
        _c, _s, _k = wired
        for junk in (None, {}, [], "x", 42):
            resp = bridge_mod._card_action(junk)
            assert "card" not in resp, f"凭 {junk!r} 造出了卡片"

    def test_unknown_choice_keeps_card_unchanged(self, wired):
        _c, store, _k = wired
        item = store.ask("x")
        resp = bridge_mod._card_action(payload(item.credential, "allow_forever"))
        assert "card" not in resp
