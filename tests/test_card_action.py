"""卡片点击处理器的守卫。

重点是**失败方向**：
- 没有连接 → 拒绝写入（不能猜「已允许」）
- 凭据不在库里 → 丢弃但**留痕**（否则「点了没反应」查不出）
- 载荷不认 → 丢弃
- 处理器自己崩 → 不掀翻消息通道
- **绝不能静默成功**

为什么要测这些：这一层跑在**桥接进程**里，一个异常如果掀翻了连接，
用户会同时失去消息能力 —— 代价远大于「这一次确认没记上」。
"""
from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from freeagent.feishu import bridge as bridge_mod
from freeagent.services.approval import ApprovalStore
from freeagent.storage.db import connect, init_schema


@pytest.fixture
def wired(tmp_path):
    """把连接注入模块全局，并保证测试后还原。"""
    conn = connect(Path(tmp_path) / "a.db")
    init_schema(conn)
    old = bridge_mod._card_conn
    bridge_mod._card_conn = conn
    try:
        yield conn
    finally:
        bridge_mod._card_conn = old


def _has_buttons(card: dict) -> bool:
    return any(el.get("tag") == "action" for el in card.get("elements", []))


def payload(cred: str, choice: str, who: str = "ou_tester") -> dict:
    """按**实测**的形状造一个载荷（值是普通 dict —— 探针已证 SDK 会解成这样）。"""
    return {
        "header": {"event_id": "ev-1", "event_type": "card.action.trigger"},
        "event": {
            "operator": {"open_id": who, "user_id": "2d1b7bec", "union_id": "on_x"},
            "action": {"value": {"action": choice, "id": cred}, "tag": "button"},
            "context": {"open_message_id": "om_1", "open_chat_id": "oc_1"},
        },
    }


class TestRecordsDecision:
    def test_allow_lands_in_the_store(self, wired):
        store = ApprovalStore(wired)
        item = store.ask("读目录")
        bridge_mod._card_action(payload(item.credential, "allow_once"))
        assert store.get(item.credential).decision == "allow"

    def test_deny_lands_in_the_store(self, wired):
        store = ApprovalStore(wired)
        item = store.ask("读目录")
        bridge_mod._card_action(payload(item.credential, "deny"))
        assert store.get(item.credential).decision == "deny"

    def test_records_who_clicked(self, wired):
        """「谁批的」必须事后查得到 —— 没这一列就无从追责。"""
        store = ApprovalStore(wired)
        item = store.ask("读目录")
        bridge_mod._card_action(payload(item.credential, "allow_once", who="ou_张三"))
        got = store.get(item.credential)
        assert got.decided_by == "ou_张三"

    def test_uses_open_id_preferred(self, wired):
        """飞书一人三层 id，取 ``ou_``（应用级，最权威）。"""
        store = ApprovalStore(wired)
        item = store.ask("x")
        bridge_mod._card_action(payload(item.credential, "allow_once", who="ou_abc"))
        assert store.get(item.credential).decided_by == "ou_abc"

    def test_returns_a_card_so_the_user_sees_the_result(self, wired):
        """**已实现**卡片回写：点了之后那张卡会被替换成「已允许/已拒绝」。

        契约变更（2026-09-29）：原先这里断言返回 ``{}``（= 不更新卡片），
        因为那时还没做视觉反馈 —— 用户点完什么都看不出来，
        而按钮还能再点。现在返回 ``{toast, card}``，
        形状由 SDK 的 ``P2CardActionTriggerResponse._types`` 定死。
        反馈的具体内容由 ``test_card_feedback.py`` 守。
        """
        store = ApprovalStore(wired)
        item = store.ask("x")
        resp = bridge_mod._card_action(payload(item.credential, "allow_once"))
        assert "toast" in resp and "card" in resp
        assert not _has_buttons(resp["card"]), "已处理的卡片还留着可点的按钮"


class TestRejects:
    def test_no_connection_writes_nothing(self, tmp_path):
        """没注入连接 → 明确拒绝。**不能偷偷新建连接**，也不能猜成允许。"""
        conn = connect(Path(tmp_path) / "a.db")
        init_schema(conn)
        store = ApprovalStore(conn)
        item = store.ask("x")
        old = bridge_mod._card_conn
        bridge_mod._card_conn = None
        try:
            bridge_mod._card_action(payload(item.credential, "allow_once"))
        finally:
            bridge_mod._card_conn = old
        assert store.get(item.credential).decision is None, (
            "没有连接却记了决定 —— 那是在没落盘的情况下报了「已允许」"
        )

    def test_unknown_credential_creates_nothing(self, wired):
        """凭据查不到 → 落一张「已过期」卡，**但绝不凭空造出一行**。

        关键不是返回值，是**库里没多东西** —— 那是安全边界。
        （契约变更：原先断言返回 ``{}``；现在是「明确告诉用户过期了」，
        因为静默丢弃会让人以为机器人坏了。）
        """
        store = ApprovalStore(wired)
        bridge_mod._card_action(payload("ap-不存在", "allow_once"))
        assert store.get("ap-不存在") is None

    def test_unknown_choice_creates_nothing(self, wired):
        """按钮文案被改、或 value 被塞了别的动作 → 认不出来就不猜。"""
        store = ApprovalStore(wired)
        item = store.ask("x")
        bridge_mod._card_action(payload(item.credential, "allow_forever"))
        assert store.get(item.credential).decision is None

    def test_missing_id_is_rejected(self, wired):
        bad = {"header": {}, "event": {"action": {"value": {"action": "allow_once"}}}}
        resp = bridge_mod._card_action(bad)
        assert "toast" in resp, "至少要让用户看到「没处理」"
        assert "card" not in resp, "认不出是哪个凭据时不该编一张卡出来"

    def test_garbage_payload_does_not_raise(self, wired):
        """什么乱七八糟的载荷都不能掀翻消息通道。

        也**不该打堆栈** —— 垃圾输入不是错误。真打堆栈的话，一条乱帧
        就能把日志刷满，而它本来什么都不是。
        """
        for junk in (None, {}, [], "字符串", 42, {"event": None}):
            resp = bridge_mod._card_action(junk)
            assert isinstance(resp, dict), f"{junk!r} 让处理器返回了非 dict"


class TestExpiredCard:
    def test_clicking_after_expiry_still_denies(self, wired):
        """过期后点老卡片 → 落成 deny，且**不记点击者**。

        不记是因为那次点击在时间上无效；记下是谁点的会让人误以为它算数。
        """
        store = ApprovalStore(wired)
        item = store.ask("x", ttl_seconds=-1)      # 已经过期
        bridge_mod._card_action(payload(item.credential, "allow_once", who="ou_晚点的人"))
        got = store.get(item.credential)
        assert got.decision == "deny", "过期后点「允许」必须记成拒绝"
        assert got.decided_by == "expired", (
            f"过期降级不该记点击者，实际={got.decided_by!r}"
        )
