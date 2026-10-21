"""第 5 步「交简报」的**消费路径**：`_process` 里 `_route_brief` 的接线。

## 这个文件守的是什么

四段选完后，用户下一句纯文字**必然**是简报。这句话必须在
``channel.handle`` **之前**被接走 —— 否则它会进意图分类，而
「了解最新开发进度」会被判成「查某条事务的进展」，用户当天没排事务，
于是回了「没找到你说的是哪一条」。

`test_feishu_select_card.py` 只证明第四段点击会 arm 一份快照、发一张提示卡。
它**不**证明下一句话真的会被消费 —— 那是这个文件的事。

## 为什么断言「handle 有没有被摸到」

与 `test_menu_routing.py` 同因：路由是一条链，一个正确的函数放在错的
位置就是 no-op。所以这里断言的是 ``channel.calls``（派发有没有被走到），
而不是 ``_route_brief`` 返回了什么。把调用挪后一步，返回值的测试照样绿，
而原 bug 原样复发。

## 白名单与去重：插在前面就得自己补

简报路径插在 ``channel.handle`` 前面，于是绕过了它内部的白名单与去重。
两条后果各有一条测试钉住：白名单外的人发简报**不能**被消费（否则等于
向陌生人确认这里有个 bot），重投事件**不能**再发一张确认卡。
"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from freeagent.feishu import bridge as bridge_mod  # noqa: E402
from freeagent.feishu import brief_pending  # noqa: E402
from freeagent.feishu.bridge import FeishuBridge  # noqa: E402
from freeagent.feishu.sender import BRIEF_CONFIRM_ACTION  # noqa: E402

ALICE = "ou_Alice_12345678"
STRANGER = "ou_Stranger_99999999"

SNAPSHOT = dict(
    project="D:/p/freeagent", model="opencode/big-pickle",
    agent="Sisyphus - ultraworker", variant="high",
)


class _Msg:
    """`_process` 会读到的每一个字段。"""

    def __init__(self, text: str, *, who: str = ALICE,
                 event_id: str = "ev_1", message_id: str = "om_1") -> None:
        self.text = text
        self.sender_open_id = who
        self.sender_ids = frozenset({who})
        self.unsupported_type = ""
        self.chat_id = "oc_chat"
        self.event_id = event_id
        self.message_id = message_id
        #: V1.48 起 ``_capture_unsupported`` 会读它（None = 抽不出可下载
        #: 附件，走富文本分支只回提示）。真类 ``IncomingMessage`` 是同名
        #: 字段，缺了就是 AttributeError —— 替身必须跟真接口同形状。
        self.attachment = None

    @property
    def dedup_key(self) -> str:
        return self.event_id or self.message_id

    @property
    def sender_label(self) -> str:
        return "Alice"


class _Sender:
    """记下发出去的卡与文字。`_transport` 是发卡那条路。"""

    def __init__(self) -> None:
        self.cards: list[dict] = []
        self.texts: list[tuple[str, str]] = []

    def _receive_id_type(self, open_id: str) -> str:
        return "open_id"

    class config:
        base_url = "https://open.feishu.cn"

    timeout = 5
    token = staticmethod(lambda: "t")

    def _transport(self, url, payload, headers, timeout):
        self.cards.append(json.loads(payload["content"]))
        return b'{"code":0,"data":{"message_id":"om_test"}}'

    def send_text(self, chat_id, text, **kw):
        self.texts.append((chat_id, text))


class _Reply:
    """鸭子类型替身。桥接会摸 ``plan_confirm`` / ``deny_reason`` /
    ``task_action``，缺字段就是 ``AttributeError`` —— 刻意不兜底。"""

    def __init__(self, text: str, *, choices=(), denied: bool = False,
                 plan_confirm=(), deny_reason: str = "",
                 task_action=None) -> None:
        self.text = text
        self.choices = list(choices)
        self.denied = denied
        self.plan_confirm = tuple(plan_confirm)
        self.deny_reason = deny_reason
        self.task_action = task_action


class _Dedup:
    """与 `Deduplicator` 同语义：**见过（含本次）就 True，并记下**。"""

    def __init__(self) -> None:
        self.seen: set[str] = set()

    def is_duplicate(self, event_id: str) -> bool:
        if not event_id:
            return False
        if event_id in self.seen:
            return True
        self.seen.add(event_id)
        return False


class _Role:
    def __init__(self, name: str) -> None:
        self.name = name


class _Roles:
    def __init__(self, names) -> None:
        self.names = list(names)

    def list_roles(self):
        return [_Role(n) for n in self.names]


class _App:
    def __init__(self, roles) -> None:
        self.roles = _Roles(roles)
        self.lock = threading.RLock()


class _Channel:
    """记录派发有没有被走到。`calls` 是这个文件的核心断言对象。"""

    def __init__(self, reply: _Reply, *, allowed: bool = True,
                 roles=("默认脉络",)) -> None:
        self.reply = reply
        self.calls: list[str] = []
        self.dedup = _Dedup()
        self.allowed = allowed
        self.app = _App(roles)

    def handle(self, chat_id, sender_ids, text, *, event_id=None):
        self.calls.append(text)
        if not self.allowed:
            return _Reply("", denied=True)
        return self.reply

    def is_allowed(self, sender_ids) -> bool:
        return self.allowed

    def allowlist_mismatch(self, sender_ids) -> str:
        return "不在白名单"


@pytest.fixture(autouse=True)
def _clear_pending():
    """待填简报是模块级内存，测试之间必须清干净。"""
    brief_pending._PENDING.clear()
    yield
    brief_pending._PENDING.clear()


@pytest.fixture
def wired(monkeypatch):
    sender = _Sender()
    channel = _Channel(_Reply("已建事务 abc123"))
    monkeypatch.setattr(bridge_mod, "_card_sender", sender, raising=False)
    monkeypatch.setattr(bridge_mod, "_card_channel", channel, raising=False)
    bridge = FeishuBridge.__new__(FeishuBridge)
    bridge.channel = channel
    bridge.sender = sender
    bridge.config = None
    return bridge, sender, channel


def _arm(who: str = ALICE) -> None:
    brief_pending.arm(who, chat_id="oc_chat", **SNAPSHOT)


# --------------------------------------------------------------------------- #
# 消费：这句话被接走，不进派发
# --------------------------------------------------------------------------- #
def test_brief_is_consumed_before_dispatch(wired):
    bridge, sender, channel = wired
    _arm()
    bridge._process(_Msg("了解最新开发进度"))

    assert not channel.calls, (
        "简报到了 channel.handle —— 它会被意图分类误判成「查某条事务的进展」"
    )
    assert brief_pending.peek(ALICE).brief == "了解最新开发进度"
    assert len(sender.cards) == 1, "收到简报要发确认卡"
    assert not sender.texts, "卡已说明一切，不必再回文字"


def test_confirm_card_is_json_1_0(wired):
    """1.0 = 顶层 ``elements``；2.0 是 ``schema``/``body``。

    官方 200830：2.0 卡不能更新成 1.0，反之亦然 —— 版本写错的后果是
    「点一下直接失败」，而**发出去时完全正常**。
    """
    bridge, sender, _ = wired
    _arm()
    bridge._process(_Msg("改一下登录页"))
    card = sender.cards[0]
    assert "elements" in card
    assert "body" not in card and "schema" not in card


def test_confirm_card_payload_carries_no_content(wired):
    """载荷**只**说点了哪个按钮 —— 项目 / 简报 / 模型 / 角色一个都不进。

    载荷是客户端回传、**可伪造**的。放项目进去就等于让任何人伪造一个
    项目路径，绕过项目白名单那唯一一道闸门。
    """
    bridge, sender, _ = wired
    _arm()
    bridge._process(_Msg("改一下登录页"))
    card = sender.cards[0]
    buttons = [b for el in card["elements"] if el["tag"] == "action"
               for b in el["actions"]]
    assert buttons, "确认卡要有按钮"
    for btn in buttons:
        assert set(btn["value"]) == {"action", "choice", "chat"}, (
            f"载荷里混进了内容字段：{sorted(btn['value'])}"
        )
        assert btn["value"]["action"] == BRIEF_CONFIRM_ACTION


def test_confirm_card_shows_the_preview(wired):
    """回显必须印出「会建出什么」—— 没有回显，用户点的就是「信任」。"""
    bridge, sender, _ = wired
    _arm()
    bridge._process(_Msg("改一下登录页"))
    body = sender.cards[0]["elements"][0]["text"]["content"]
    assert "D:/p/freeagent" in body
    assert "改一下登录页" in body
    assert "默认脉络" in body


def test_no_roles_preview_says_it_will_be_rejected(wired):
    """没有脉络时如实写在卡上 —— 建委派时会被拒，提前说清比事后丢失败有用。"""
    bridge, sender, channel = wired
    channel.app.roles.names = []
    _arm()
    bridge._process(_Msg("改一下登录页"))
    body = sender.cards[0]["elements"][0]["text"]["content"]
    assert "没有脉络" in body


# --------------------------------------------------------------------------- #
# 不消费：不在待填态 / 命令 / 空 / 非文字
# --------------------------------------------------------------------------- #
def test_plain_text_without_pending_reaches_dispatch(wired):
    """没 arm 过就不是简报 —— 一个字都不该被吞。"""
    bridge, sender, channel = wired
    bridge._process(_Msg("了解最新开发进度"))
    assert channel.calls == ["了解最新开发进度"]
    assert not sender.cards


def test_slash_command_discards_pending_and_reaches_dispatch(wired):
    """命令优先：以 ``/`` 开头的不当简报，且**顺手清掉待填态**。

    用户改主意去打命令了，那份快照就该作废 —— 否则下一条普通文字会被
    当成简报，而用户其实已经不在第 5 步了。
    """
    bridge, sender, channel = wired
    _arm()
    bridge._process(_Msg("/today"))
    assert brief_pending.peek(ALICE) is None, "命令该清掉待填态"
    assert channel.calls == ["/today"]
    assert not sender.cards


def test_empty_text_is_not_consumed(wired):
    bridge, sender, channel = wired
    _arm()
    bridge._process(_Msg("   "))
    assert brief_pending.peek(ALICE).brief == "", "空话不该填进简报"
    assert channel.calls == ["   "]


def test_unsupported_type_is_not_consumed(wired):
    bridge, sender, channel = wired
    _arm()
    msg = _Msg("")
    msg.unsupported_type = "image"
    bridge._process(msg)
    assert brief_pending.peek(ALICE).brief == ""
    assert not sender.cards
    assert not channel.calls, "图片绝不进派发"


# --------------------------------------------------------------------------- #
# 白名单与去重：插在前面就得自己补
# --------------------------------------------------------------------------- #
def test_stranger_brief_is_silent_and_not_consumed(wired):
    """白名单外的人发简报 —— 不消费、不回话、不发卡。

    回一句就等于向陌生人确认「这里有个 bot」，而那正是白名单要挡的泄露面。
    这条守的是「简报路径插在 handle 前面、绕过了它的白名单判定」这个真实缺陷。
    """
    bridge, sender, channel = wired
    _arm(STRANGER)
    channel.allowed = False
    bridge._process(_Msg("改一下登录页", who=STRANGER))

    assert brief_pending.peek(STRANGER).brief == "", "白名单外不该被消费"
    assert not sender.cards, "给白名单外的人发了确认卡 —— 这是泄露面"
    assert not sender.texts, "白名单外的人连提示都不该收"
    assert not channel.calls, "已处理（静默），不该再交回 handle"


def test_duplicate_event_does_not_resend_confirm(wired):
    """飞书重连重投同一条消息 —— 不能重复发确认卡。

    去重表是在 ``handle`` 里写的，简报路径压根没走到那里；不自己补，
    第二次 ``peek`` 仍在，于是**再发一张**。这是本段最容易漏的坑。
    """
    bridge, sender, channel = wired
    _arm()
    bridge._process(_Msg("改一下登录页", event_id="ev_dup"))
    first = len(sender.cards)
    bridge._process(_Msg("改一下登录页", event_id="ev_dup"))
    assert len(sender.cards) == first, "重投事件又发了一张确认卡"
    assert first == 1
