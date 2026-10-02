"""The menu's button click, end-to-end from a Feishu callback payload.

## The gap this closes

`test_menu.py` calls `_run_menu` **directly**. That proves the handler works
but not that anything *reaches* it. The dispatch itself lives in
`_card_action`, and codegraph reported it as having no covering tests.

That distinction is the whole point: if the dispatch branch is wrong, every
handler unit test still passes and the buttons silently do nothing when
clicked -- which is the exact failure the user would hit on a real machine,
and the one thing a static test of the handler cannot catch.

## Why a synthetic payload is legitimate here

The payload shape is not guessed. It is documented in `_card_action`'s own
docstring as **measured**:

    event.action.value = {"action": "...", ...}
    event.operator      = {open_id, user_id, union_id, tenant_key}
    event.context       = {open_message_id, open_chat_id, ...}
    header.event_id     = 去重键

What this file still cannot prove is the *last* mile: that Feishu's servers
deliver a real click in this shape. That was verified separately on a real
machine (permission cards were clicked and the decisions landed in the DB).
This file's job is the wiring in between.
"""
from __future__ import annotations

import datetime
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from freeagent.app import build_app  # noqa: E402
from freeagent.feishu import bridge as bridge_mod  # noqa: E402
from freeagent.feishu import sender as sender_mod  # noqa: E402
from freeagent.feishu.sender import MENU_ACTION  # noqa: E402
from freeagent.services.clock import FrozenClock  # noqa: E402

ALICE = "ou_Alice_12345678"
CHAT = "oc_chat"


def _dt(y, m, d, h=12):
    return datetime.datetime(y, m, d, h)


class _Sender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send_text(self, chat_id: str, text: str) -> None:
        self.sent.append((chat_id, text))


class _Reply:
    def __init__(self, text: str, *, denied: bool = False) -> None:
        self.text = text
        self.denied = denied


class _Channel:
    def __init__(self, app, reply: _Reply) -> None:
        self.app = app
        self.reply = reply
        self.seen = None
        self.allowed = True

    def handle(self, chat_id, who, text):
        self.seen = (chat_id, who, text)
        return self.reply

    def is_allowed(self, sender_ids) -> bool:
        # 签名照抄 ChannelService.is_allowed：它**接受一个裸字符串**
        # （内部 if isinstance(sender_ids, str)），因为飞书同一个人有三层
        # ID，而卡片回调给的 `who` 就是单个 open_id。
        return self.allowed

    def allowlist_mismatch(self, sender_ids) -> str:
        return "不在白名单"


class _Cards:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, sender, *, open_id, subject, items, note="", chat_id=""):
        self.calls.append({"items": list(items), "chat_id": chat_id})
        return "om_menu"


def _click(value: dict, *, who: str = ALICE) -> dict:
    """一个形状与实测一致的卡片回调载荷。"""
    return {
        "header": {"event_id": "ev_click_1"},
        "event": {
            "action": {"value": value},
            "operator": {
                "open_id": who, "user_id": "u_1",
                "union_id": "on_1", "tenant_key": "t_1",
            },
            "context": {"open_message_id": "om_1", "open_chat_id": CHAT},
        },
    }


@pytest.fixture
def wired(tmp_path, monkeypatch):
    app = build_app(tmp_path / "a.db", clock=FrozenClock(_dt(2026, 10, 1)))
    app.roles.create("默认脉络")
    sender = _Sender()
    channel = _Channel(app, _Reply("角色下的事务"))
    monkeypatch.setattr(bridge_mod, "_card_conn", app.conn)
    monkeypatch.setattr(bridge_mod, "_card_sender", sender, raising=False)
    monkeypatch.setattr(bridge_mod, "_card_channel", channel, raising=False)
    monkeypatch.setattr(bridge_mod, "send_menu_card", _Cards())
    yield sender, channel, app
    app.close()


# --------------------------------------------------------------------------- #
# Dispatch: does a click REACH the menu handler at all?
# --------------------------------------------------------------------------- #
def test_click_reaches_menu_handler(wired):
    sender, channel, _ = wired
    out = bridge_mod._card_action(
        _click({"action": MENU_ACTION, "choice": "help", "chat": CHAT})
    )
    assert sender.sent, "点击菜单按钮后没有任何回话 —— 分派分支没接上"
    assert "/today" in sender.sent[0][1], "回的不是「帮助」那段正文"
    assert sender.sent[0][0] == CHAT, "回话发错了会话"
    assert out["toast"]["type"] == "success"
    assert "card" in out, "没有回写卡片，按钮会留在那儿让人以为还能再点"


def test_click_retires_the_buttons(wired):
    """点过之后必须把卡片换成不可再点的形态。

    留着可点的按钮等于骗人——他会以为还能再点，而第二次点只会得到同一份
    数据。这个道理与 ``_run_view_choice`` 完全一致。
    """
    _, _, _ = wired
    out = bridge_mod._card_action(
        _click({"action": MENU_ACTION, "choice": "help", "chat": CHAT})
    )
    # 应答体是 ``{"card": {"type": "raw", "data": <卡片>}}`` ——
    # 卡片在 **data** 里，不在 card 里。直接返回裸卡片是实测踩过的坑：
    # 飞书那边判整条应答失败，用户看到「出错了」，而我们侧一切正常。
    assert out["card"]["type"] == "raw", "少 {type, data} 包装 → 飞书判失败"
    card = out["card"]["data"]
    assert card["header"]["title"]["content"] == "已打开"
    assert "action" not in str(card), "回写的卡片里仍有可点按钮：" + str(card)


def test_role_click_goes_through_the_view_path(wired):
    sender, channel, _ = wired
    bridge_mod._card_action(
        _click({"action": MENU_ACTION, "choice": "role:默认脉络", "chat": CHAT})
    )
    assert channel.seen == (CHAT, ALICE, "/role 默认脉络"), (
        "角色按钮没有转手给只读视图路径"
    )
    assert sender.sent, "点了角色却什么都没发"


def test_delegate_click_fills_a_real_role(wired):
    sender, _, _ = wired
    bridge_mod._card_action(
        _click({"action": MENU_ACTION, "choice": "delegate", "chat": CHAT})
    )
    assert "/delegate" in sender.sent[0][1]
    assert "默认脉络" in sender.sent[0][1], "没填上真实角色名，用户还得自己查"


# --------------------------------------------------------------------------- #
# Refusals must not silently "succeed"
# --------------------------------------------------------------------------- #
def test_unknown_choice_is_refused_visibly(wired):
    sender, _, _ = wired
    out = bridge_mod._card_action(
        _click({"action": MENU_ACTION, "choice": "run_view", "chat": CHAT})
    )
    assert out["toast"]["type"] == "error"
    assert not sender.sent, "不认识的菜单项触发了动作"
    assert "card" not in out, "这一支刻意不动卡片：不能凭空编一张出来"


def test_menu_click_without_channel_is_refused(wired, monkeypatch):
    monkeypatch.setattr(bridge_mod, "_card_sender", None)
    out = bridge_mod._card_action(
        _click({"action": MENU_ACTION, "choice": "help", "chat": CHAT})
    )
    assert out["toast"]["type"] == "error"


def test_empty_choice_is_refused(wired):
    sender, _, _ = wired
    out = bridge_mod._card_action(_click({"action": MENU_ACTION, "choice": ""}))
    assert out["toast"]["type"] == "error"
    assert not sender.sent


# --------------------------------------------------------------------------- #
# A menu click must not be mistaken for a permission decision
# --------------------------------------------------------------------------- #
def test_non_allowlisted_clicker_gets_nothing(wired, monkeypatch):
    """白名单外点菜单按钮：**什么也拿不到**。

    这是我自己漏掉的洞：`_run_menu` 的 help/delegate/today/roles 分支直接
    调 `_card_sender.send_text`，不经过 `ChannelService.handle()` —— 而白名单
    判定住在那里。菜单卡在**群里**全员可见，于是非白名单成员点一下就收到
    回话。

    为什么这条要有：先前所有点击测试都用 ALICE（白名单内），
    **没有一条覆盖白名单外的点击者**，所以这个洞一路绿灯通过。
    """
    sender, channel, _ = wired
    channel.allowed = False
    out = bridge_mod._card_action(
        _click({"action": MENU_ACTION, "choice": "help", "chat": CHAT})
    )
    assert not sender.sent, "白名单外点菜单居然收到了回话 —— 这是泄露面"
    assert out["toast"]["type"] == "error"
    assert "card" not in out, "不该回写卡片：凭空改一张会让人以为处理过"


def test_non_allowlisted_cannot_use_role_button(wired, monkeypatch):
    """角色按钮那一支也不能漏 —— 它「本来安全」靠的是转手，
    不是靠自己的判定；一旦有人改分支就穿了。"""
    sender, channel, _ = wired
    channel.allowed = False
    bridge_mod._card_action(
        _click({"action": MENU_ACTION, "choice": "role:默认脉络", "chat": CHAT})
    )
    assert not sender.sent, "白名单外点角色按钮拿到了角色内容"


def test_menu_click_never_grants_permission(wired):
    """菜单点击绝不能落进批准流程。

    这是整个功能可以存在的**前提**（设计方案 11.8.1 第三道闸门）：
    打字与点菜单都只能「跑只读内容 / 重发一张卡」，绝不能批准代码执行。

    先前一版这条是**假通过**：``out["card"].get("value", {})`` 在真实形状
    下恒为 ``{}``（卡片在 ``card.data`` 里），所以断言永远成立、什么也没
    验证。现在改成两条真断言：回写的卡里没有任何可点元素，且库里**没有
    产生任何审批行**。
    """
    _, _, app = wired
    out = bridge_mod._card_action(
        _click({"action": MENU_ACTION, "choice": "help", "chat": CHAT})
    )

    card = out["card"]["data"]
    tags = [e.get("tag") for e in card.get("elements", [])]
    assert "action" not in tags, "回写卡里仍有可点元素：" + str(tags)

    rows = list(app.conn.execute(
        "SELECT COUNT(*) FROM pending_approvals WHERE decided_by IS NOT NULL"
    ))
    assert rows[0][0] == 0, "点菜单居然改动了审批记录 —— 那就是授权路径被穿透了"


def test_view_choice_click_still_works(wired):
    """菜单分支插在只读选项之后，不能把原来那条路弄坏。"""
    sender, channel, _ = wired
    out = bridge_mod._card_action(
        _click({"action": sender_mod.VIEW_CHOICE_ACTION, "view": "/today",
                "chat": CHAT})
    )
    assert channel.seen == (CHAT, ALICE, "/today")
    assert sender.sent