"""The menu: a landing card for what is NOT a delegation.

## Why this file exists

Two real messages, both measured on a live bridge, both handled by the
dispatch machinery even though the user was **not dispatching anything**:

    "你好"            -> channel.handle() -> CLARIFY -> role names as buttons
    "你有什么角色？"   -> _route_answer()  -> stored as the ANSWER to a
                         pending question

The first looks like a form the user never asked to fill in. The second is
worse: the question was consumed as data, so the delegation now waits for an
answer that will never arrive, and the user's actual question is gone.

So the menu's first job is not convenience. It is to give "I am not
dispatching" an explicit landing place **before** the dispatch machinery sees
the message.

## What is asserted here

Card shape is pinned to JSON 1.0 for a concrete reason: Feishu error 200830
forbids updating a 2.0 card into 1.0 or vice versa, and the approval/choice
card family is 1.0. A 2.0 menu would look nicer and fail on click.
"""
from __future__ import annotations

import datetime
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from freeagent.app import build_app  # noqa: E402
from freeagent.feishu import bridge as bridge_mod  # noqa: E402
from freeagent.feishu.bridge import FeishuBridge  # noqa: E402
from freeagent.feishu.sender import (  # noqa: E402
    MENU_ACTION,
    menu_card,
)
from freeagent.services.clock import FrozenClock  # noqa: E402

ALICE = "ou_Alice_12345678"


def _dt(y, m, d, h=12):
    return datetime.datetime(y, m, d, h)


class _Msg:
    def __init__(self, text: str, *, who: str = ALICE) -> None:
        self.text = text
        self.sender_open_id = who
        self.unsupported_type = ""
        self.chat_id = "oc_chat"
        self.event_id = "ev_1"
        self.message_id = "om_1"
        # `_route_menu` 现在会问白名单与去重，所以这两个属性是**被读的**，
        # 不是摆设：少了 `sender_ids` 就是 AttributeError，少了 `dedup_key`
        # 则去重闸门永远短路。
        self.sender_ids = frozenset({who})

    @property
    def dedup_key(self) -> str:
        return self.event_id or self.message_id


class _Sender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send_text(self, chat_id: str, text: str) -> None:
        self.sent.append((chat_id, text))


class _Cards:
    """Records every menu card instead of calling Feishu."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, sender, *, open_id, subject, items, note="",
                 chat_id=""):
        self.calls.append({
            "open_id": open_id, "subject": subject,
            "items": list(items), "note": note, "chat_id": chat_id,
        })
        return "om_menu"


class _Reply:
    """`ChannelService.handle` 的最小返回。"""

    def __init__(self, text: str, *, denied: bool = False) -> None:
        self.text = text
        self.denied = denied


class _Dedup:
    """与 `Deduplicator` 同语义：见过（含本次）返回 True，**并记下**。

    必须真的会记，否则「同一事件重投两次只发一张卡」这种断言是靠假货
    通过的 —— 而它守的正是「飞书重连重投 → 菜单卡重复发出」这个真实缺陷。
    """

    def __init__(self) -> None:
        self.seen: set[str] = set()

    def is_duplicate(self, event_id: str) -> bool:
        if not event_id:
            return False
        if event_id in self.seen:
            return True
        self.seen.add(event_id)
        return False


class _Channel:
    """`_run_view_choice` 用到 `app`/`handle`；`_route_menu` 用到白名单判定。

    白名单那两项是后加的：菜单插在 `channel.handle` **前面**，于是绕过了
    里面的白名单判定（那是个真实缺陷，见 `test_menu_routing.py`）。
    修好之后 `_route_menu` 自己会问一次，所以这个替身必须答得上来 ——
    否则本文件里每个菜单测试都会以 `AttributeError` 收场。
    """

    def __init__(self, app, reply: _Reply) -> None:
        self.app = app
        self.reply = reply
        self.allowed = True
        self.dedup = _Dedup()

    def handle(self, chat_id, who, text):
        self.seen = (chat_id, who, text)
        return self.reply

    def is_allowed(self, sender_ids) -> bool:
        return self.allowed

    def allowlist_mismatch(self, sender_ids) -> str:
        return "不在白名单"


@pytest.fixture
def wired(tmp_path, monkeypatch):
    app = build_app(tmp_path / "a.db", clock=FrozenClock(_dt(2026, 10, 1)))
    sender = _Sender()
    # **同一个** sender 实例挂在两个位置：`_run_menu` 走模块级 `_card_sender`，
    # 而 `_route_menu` 走 `self.sender`。做成两个实例的话，按钮点击的断言
    # 会读到一只空列表，而测试照样「通过」——那就是在验证假货。
    monkeypatch.setattr(bridge_mod, "_card_conn", app.conn)
    monkeypatch.setattr(bridge_mod, "_card_sender", sender, raising=False)
    # 角色按钮要转手 `_run_view_choice`，那条路会用到通道。
    # **不给它设通道就会得到「没连上通道」** —— 第一次跑这个测试时正是如此，
    # 于是断言失败暴露出 fixture 的漏洞，而不是代码的漏洞。
    channel = _Channel(app, _Reply("角色下的事务"))
    monkeypatch.setattr(bridge_mod, "_card_channel", channel, raising=False)
    bridge = FeishuBridge.__new__(FeishuBridge)
    bridge.sender = sender
    # `self.channel` 同样是**被读的**：菜单插在 `channel.handle` 前面，
    # 于是白名单与去重都得由 `_route_menu` 自己问 —— 它问的就是这个属性。
    # 少了这一行，本文件里每个菜单测试都会以 AttributeError 收场。
    bridge.channel = channel
    cards = _Cards()
    monkeypatch.setattr(bridge_mod, "send_menu_card", cards)
    yield bridge, sender, cards, app
    app.close()


# --------------------------------------------------------------------------- #
# Card shape
# --------------------------------------------------------------------------- #
def test_menu_card_is_json_1_0():
    card = menu_card("要做什么", [("甲", "a")], chat_id="oc_1")
    assert "schema" not in card, "写了 schema 就可能变成 2.0，与批准卡不同版本"
    assert "elements" in card, "1.0 的组件在顶层 elements，不在 body.elements"


def test_menu_card_buttons_carry_object_value():
    """`value` 必须是 object —— 飞书 SDK 只支持 object 形态的���传数据。"""
    card = menu_card("要做什么", [("委派", "delegate")], chat_id="oc_1")
    button = card["elements"][1]["actions"][0]
    assert button["value"] == {
        "action": MENU_ACTION, "choice": "delegate", "chat": "oc_1",
    }
    assert isinstance(button["value"], dict)


def test_menu_card_first_button_is_primary():
    card = menu_card("t", [("甲", "a"), ("乙", "b")])
    buttons = card["elements"][1]["actions"]
    assert [b["type"] for b in buttons] == ["primary", "default"]


def test_menu_card_truncates_long_label():
    card = menu_card("t", [("一" * 80, "a")])
    label = card["elements"][1]["actions"][0]["text"]["content"]
    assert len(label) == 40


def test_menu_card_rejects_empty_items():
    with pytest.raises(ValueError):
        menu_card("t", [])


# --------------------------------------------------------------------------- #
# Text routing: what must NOT reach the dispatch machinery
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text", [
    "/menu", "/菜单", "你好", "您好", "hi", "hello", "在吗", "你能做什么", "怎么用",
    "你好啊，今天有什么",          # 中文按包含匹配：句中有「你好」就该回菜单
])
def test_greetings_and_menu_commands_land_on_the_menu(wired, text):
    bridge, sender, cards, _ = wired
    assert bridge._route_menu(_Msg(text)) == ""
    assert len(cards.calls) == 1
    assert not sender.sent, "卡已经说明一切，不该再补一句文字"


@pytest.mark.parametrize("text", [
    "把 chip 寄存器改一下",       # 含 'hi'
    "this 那个字段呢",           # 含 'hi'
    "while 循环里加个日志",      # 含 'hi'
    "shipping 标签",             # 含 'hi'
])
def test_short_english_words_do_not_fire_on_substring(wired, text):
    """`hi` 绝不能靠包含命中 chip/this/while。

    这是我自己写出来的 bug：菜单把 ``hi`` 当子串匹配，于是「把 chip
    寄存器改一下」这种正经任务会被当成人打招呼，弹一张菜单卡出来。
    **一个把 chip 当成打招呼的助手，比没有菜单更糟** —— 它会静默吞掉
    一条真正的任务，而用户只看到一张莫名其妙的卡。
    """
    bridge, sender, cards, _ = wired
    assert bridge._route_menu(_Msg(text)) is None
    assert not cards.calls


def test_role_query_becomes_a_role_card(wired):
    """实测：这句话以前被当成答案吃掉，或被当成角色名塞进槽位。"""
    bridge, sender, cards, app = wired
    roles = app.roles
    roles.create("默认脉络")
    roles.create("工作")

    assert bridge._route_menu(_Msg("你有什么角色？")) == ""
    choices = [c for _, c in cards.calls[0]["items"]]
    assert choices == ["role:默认脉络", "role:工作"]


def test_role_query_without_db_says_so_instead_of_guessing(wired, monkeypatch):
    bridge, sender, cards, _ = wired
    monkeypatch.setattr(bridge_mod, "_card_conn", None)
    said = bridge._route_menu(_Msg("有哪些角色"))
    assert said and "读不到角色" in said
    assert not cards.calls


@pytest.mark.parametrize("text", [
    "/help",                      # 文档里写明的权威命令表，不许劫持
    "/today",
    "/delegate D:/p | r | 改一下 greet",
    "下周二要交的销售周报初稿",
])
def test_real_commands_pass_through_to_dispatch(wired, text):
    """菜单**不能**变成什么都拦的旁路，否则它就成了新的单点。"""
    bridge, sender, cards, _ = wired
    assert bridge._route_menu(_Msg(text)) is None
    assert not cards.calls


def test_blank_and_non_text_pass_through(wired):
    bridge, _, cards, _ = wired
    assert bridge._route_menu(_Msg("   ")) is None
    msg = _Msg("你好")
    msg.unsupported_type = "image"
    assert bridge._route_menu(msg) is None
    assert not cards.calls


# --------------------------------------------------------------------------- #
# Button clicks
# --------------------------------------------------------------------------- #
def test_menu_click_sends_text_and_retires_the_buttons(wired):
    bridge, sender, cards, _ = wired
    out = bridge_mod._run_menu(
        {"action": MENU_ACTION, "choice": "help", "chat": "oc_chat"},
        who=ALICE,
    )
    assert out["toast"]["type"] == "success"
    assert sender.sent and "/today" in sender.sent[0][1]


def test_role_button_reuses_the_view_path(wired, monkeypatch):
    """角色按钮转手给 `_run_view_choice` —— 一份能力一份实现。

    断言的是**转手过去的那句话本身**，不是「有东西发出来了」：
    后者在一个只发占位文本的实现下也会通过，而那正是分叉的起点。
    """
    bridge, sender, cards, app = wired
    channel = _Channel(app, _Reply("角色下的事务"))
    monkeypatch.setattr(bridge_mod, "_card_channel", channel, raising=False)
    app.roles.create("默认脉络")
    bridge_mod._run_menu(
        {"action": MENU_ACTION, "choice": "role:默认脉络", "chat": "oc_chat"},
        who=ALICE,
    )
    assert channel.seen == ("oc_chat", ALICE, "/role 默认脉络")
    assert sender.sent, "点了角色要真的看到那个角色的内容"


def test_delegate_button_fills_in_a_real_role_name(wired):
    bridge, sender, cards, app = wired
    app.roles.create("默认脉络")
    bridge_mod._run_menu(
        {"action": MENU_ACTION, "choice": "delegate", "chat": "oc_chat"},
        who=ALICE,
    )
    body = sender.sent[0][1]
    assert "/delegate" in body and "默认脉络" in body


def test_unknown_choice_is_refused_visibly(wired):
    bridge, sender, cards, _ = wired
    out = bridge_mod._run_menu(
        {"action": MENU_ACTION, "choice": "rm -rf", "chat": "oc_chat"}, who=ALICE,
    )
    assert out["toast"]["type"] == "error"
    assert not sender.sent, "不认识的菜单项不许触发任何动作"


def test_missing_sender_is_refused_not_guessed(wired, monkeypatch):
    monkeypatch.setattr(bridge_mod, "_card_sender", None)
    out = bridge_mod._run_menu(
        {"action": MENU_ACTION, "choice": "help", "chat": "oc_chat"}, who=ALICE,
    )
    assert out["toast"]["type"] == "error"