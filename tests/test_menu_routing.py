"""The menu must be reached BEFORE the dispatch machinery.

## The gap this file closes

`test_menu.py` calls `bridge._route_menu(...)` directly and asserts it
returns the right thing. That proves the *decision* is correct. It does
**not** prove the *wiring*: that `_process` calls it at all, and — the part
that actually matters — that it runs before `self.channel.handle(...)`.

## Why the ordering is the whole fix

The live bug was never "`_route_menu` returns the wrong value". It was
"the dispatch machinery saw the message first":

    "你好"           -> channel.handle() -> CLARIFY -> role buttons
    "你有什么角色？"  -> consumed as the ANSWER to a pending question

Routing is a chain, and a correct function placed at the wrong point in
the chain is a no-op. So these tests assert on **whether `handle` was
reached at all**, not on what `_route_menu` returned. A regression that
moved the call one step later would keep all 31 tests in `test_menu.py`
green while silently restoring the original bug.

This is also the only place that pins the interaction between the two
routes: a pending question must still win over the menu, or the menu
would swallow answers to the agent's own questions.
"""
from __future__ import annotations

import datetime
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from freeagent.app import build_app  # noqa: E402
from freeagent.feishu import bridge as bridge_mod  # noqa: E402
from freeagent.feishu import sender as sender_mod  # noqa: E402
from freeagent.feishu.bridge import FeishuBridge  # noqa: E402
from freeagent.services.approval import ApprovalStore  # noqa: E402
from freeagent.services.clock import FrozenClock  # noqa: E402

ALICE = "ou_Alice_12345678"


def _dt(y, m, d, h=12):
    return datetime.datetime(y, m, d, h)


class _Msg:
    """Everything `_process` reads off an incoming message."""

    def __init__(self, text: str, *, who: str = ALICE) -> None:
        self.text = text
        self.sender_open_id = who
        self.sender_ids = frozenset({who})
        self.unsupported_type = ""
        self.chat_id = "oc_chat"
        self.event_id = "ev_1"
        self.message_id = "om_1"

    @property
    def dedup_key(self) -> str:
        return self.event_id or self.message_id

    @property
    def sender_label(self) -> str:
        return "Alice"


class _Sender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send_text(self, chat_id: str, text: str) -> None:
        self.sent.append((chat_id, text))


class _Reply:
    def __init__(self, text: str, *, choices=(), denied: bool = False) -> None:
        self.text = text
        self.choices = list(choices)
        self.denied = denied


class _Dedup:
    """与 `Deduplicator` 同语义：**见过（含本次）就返回 True，并记下**。

    这里必须真的会记，否则去重那条测试就是假的 —— 而它守的正是
    「飞书重连重投 → 菜单卡重复发出」这个真实缺陷。
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
    """Records whether dispatch was reached. `calls` is the whole point."""

    def __init__(self, reply: _Reply) -> None:
        self.reply = reply
        self.calls: list[str] = []
        self.dedup = _Dedup()
        self.allowed = True

    def handle(self, chat_id, sender_ids, text, *, event_id=None):
        self.calls.append(text)
        if not self.allowed:
            # 真实的 ChannelService 在这里就拒了，且**一个字都不回**
            # （回一句就等于向陌生人确认「这里有个 bot 在跑」）。
            # 替身先前不学这一步，于是「白名单外保持沉默」这条测试是靠
            # 一个比真实实现更宽松的假货通过的。
            return _Reply("", denied=True)
        return self.reply

    def is_allowed(self, sender_ids) -> bool:
        return self.allowed

    def allowlist_mismatch(self, sender_ids) -> str:
        return "不在白名单"


class _Cards:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, sender, *, open_id, subject, items, note="", chat_id=""):
        self.calls.append({"items": list(items), "chat_id": chat_id})
        return "om_menu"


@pytest.fixture
def wired(tmp_path, monkeypatch):
    clock = FrozenClock(_dt(2026, 10, 1, 12))
    app = build_app(tmp_path / "a.db", clock=clock)
    # 角色查询那条路径要真有角色可列，否则 `_role_names()` 返回空、
    # 走「读不到角色」那条分支——那是对的，却测不到发卡。
    app.roles.create("默认脉络")

    sender = _Sender()
    monkeypatch.setattr(bridge_mod, "_card_conn", app.conn)
    monkeypatch.setattr(bridge_mod, "_card_sender", sender, raising=False)
    # `_card_clock` 必须和 app 的钟一致。`_route_answer` 用它构造
    # ApprovalStore：若它是真实时间而 app 的钟停在 2026-10-01，
    # 那条提问在桥接自己看来**早就过期**，`pending_question_for` 返回
    # None，于是「你好」不会被记成答案——测试会以一种与真实行为
    # 无关的方式失败。
    monkeypatch.setattr(bridge_mod, "_card_clock", lambda: clock.now(), raising=False)

    cards = _Cards()
    monkeypatch.setattr(bridge_mod, "send_menu_card", cards)

    # 必须打在 **sender 模块**上，不能打在 bridge 上。
    #
    # `_process` 里那句是**函数内导入**（``from .sender import
    # send_view_choice_card``），所以 bridge 模块上根本没有这个名字。
    # 第一次写成 ``monkeypatch.setattr(bridge_mod, "send_view_choice_card", ...)``
    # 直接 AttributeError；就算加 ``raising=False`` 能建出属性，函数内导入
    # 依然会从 sender 模块取——于是这道守卫**看起来装上了，实际从不生效**，
    # 而它恰恰是「菜单是否被绕开」的唯一指示器。
    monkeypatch.setattr(
        sender_mod, "send_view_choice_card",
        lambda *a, **k: pytest.fail(
            "CLARIFY 发了选项卡：说明消息落到了派发 machinery"
        ),
    )

    channel = _Channel(_Reply("已建事务 abc123"))
    bridge = FeishuBridge.__new__(FeishuBridge)
    bridge.channel = channel
    bridge.sender = sender
    bridge.config = None
    yield bridge, sender, cards, channel, app
    app.close()


# --------------------------------------------------------------------------- #
# The ordering, which is the actual fix
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text", ["你好", "/menu", "你能做什么", "你有什么角色？"])
def test_menu_short_circuits_before_dispatch(wired, text):
    bridge, sender, cards, channel, _ = wired
    bridge._process(_Msg(text))
    assert not channel.calls, (
        f"{text!r} 到了 channel.handle —— 菜单排在派发后面，"
        "原 bug 原样存在，而 test_menu.py 会照样全绿"
    )
    assert len(cards.calls) == 1


def test_real_command_still_reaches_dispatch(wired):
    """菜单不能变成什么都拦的旁路：真命令必须仍然到得了派发。"""
    bridge, sender, cards, channel, _ = wired
    bridge._process(_Msg("/today"))
    assert channel.calls == ["/today"]
    assert not cards.calls, "真命令不该弹菜单"
    assert sender.sent and sender.sent[0][1] == "已建事务 abc123"


def test_plain_task_still_reaches_dispatch(wired):
    bridge, sender, cards, channel, _ = wired
    bridge._process(_Msg("下周二要交的销售周报初稿"))
    assert channel.calls == ["下周二要交的销售周报初稿"]
    assert not cards.calls


# --------------------------------------------------------------------------- #
# Precedence between the two routes
# --------------------------------------------------------------------------- #
def test_pending_question_outranks_the_menu(wired):
    """菜单不能吃掉 agent 自己问题的答案。

    顺序必须是 `_route_answer` → `_route_menu`：有挂起提问时，
    哪怕用户说的是「你好」，那也**是答案**，不是打招呼。
    反过来会让 agent 永远等不到它要的答复。
    """
    bridge, sender, cards, channel, app = wired
    store = ApprovalStore(app.conn, clock=lambda: _dt(2026, 10, 1, 12))
    # 凭据必须在处理**之前**拿到：一旦「你好」被记成答案，这一问就答完了，
    # 而 `pending_question_for` 只回「还挂着的问题」—— 事后再去查必然是
    # None。先前就踩了这个：断言挂在「提问行不见了」上，看起来像代码坏了，
    # 其实是断言问错了对象。
    asked = store.request_question(
        "agent 在提问", questions=["叫什么名字？"], requested_by=ALICE,
    )

    bridge._process(_Msg("你好"))

    assert store.answers_of(asked.credential) == [["你好"]], (
        "「你好」应当被记成答案；被菜单吃掉的话 agent 会一直等下去"
    )
    assert store.is_complete(asked.credential), (
        "一问一答就该收口；没完成说明答复没落进去"
    )
    assert not cards.calls, "有挂起提问时不该弹菜单"
    assert not channel.calls


def test_menu_works_when_no_question_is_pending(wired):
    bridge, sender, cards, channel, app = wired
    bridge._process(_Msg("你好"))
    assert len(cards.calls) == 1
    assert not channel.calls


def test_deduplicated_event_is_not_reprocessed(wired):
    """去重仍然生效：菜单不该把重投的飞书事件变成重复发卡。"""
    bridge, sender, cards, channel, _ = wired
    channel.reply = _Reply("已建事务 abc123", denied=True)
    bridge._process(_Msg("你好"))
    first = len(cards.calls)
    bridge._process(_Msg("你好"))
    assert len(cards.calls) == first, "重投事件又发了一张菜单卡"


# --------------------------------------------------------------------------- #
# Non-text must be untouched
# --------------------------------------------------------------------------- #
def test_image_still_gets_the_text_only_notice(wired):
    bridge, sender, cards, channel, _ = wired
    msg = _Msg("")
    msg.unsupported_type = "image"
    bridge._process(msg)
    assert not cards.calls, "图片不该弹菜单"
    assert not channel.calls, "图片绝不进派发"
    assert sender.sent and "只处理文字" in sender.sent[0][1]


def test_silent_on_wrong_role_stays_silent(wired):
    """白名单外的人连提示都不该收 —— 回一句就等于确认这里有个 bot。

    这条守的是我自己引入的一个真实缺陷：菜单插在 ``channel.handle``
    **前面**，于是绕过了那里的白名单判定。曾经的后果是陌生人发一句
    「你好」就收到一张菜单卡 —— 那等于向白名单外确认这里有个 bot
    在跑，正是白名单要挡的泄露面。
    """
    bridge, sender, cards, channel, _ = wired
    channel.allowed = False
    bridge._process(_Msg("你好"))
    assert not cards.calls, "给白名单外的人发了菜单卡 —— 这是泄露面"
    assert not sender.sent, "白名单外的人连提示都不该收"