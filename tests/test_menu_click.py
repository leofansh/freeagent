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
        self.calls.append({"items": list(items), "chat_id": chat_id,
                            "subject": subject, "note": note})
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
    # 记录器挂在 sender 上：它是被 yield 出去的对象，测试才拿得到。
    # 之前 patch 完就丢，`sent_cards` 那种写法只能是幻觉。
    sender.cards = _Cards()
    monkeypatch.setattr(bridge_mod, "send_menu_card", sender.cards)
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
def test_roles_button_sends_a_card_not_a_text_list(wired):
    """点「我有哪些角色」必须**发角色卡**，不是发文本列表。

    这是本轮修的那处分叉：第一版这里退回纯文本 + 一句「发 /role <名字>」，
    于是同一件事在打字路上给卡片、按钮路上给文本。

    单测只覆盖 `_run_menu` 时**抓不到**这个洞 —— 它看起来完全正常。
    要抓它必须断言「发的是卡」，也就是下面这一条。
    """
    sender, channel, app = wired
    app.roles.create("工作")          # fixture 已建了「默认脉络」

    out = bridge_mod._card_action(
        _click({"action": MENU_ACTION, "choice": "roles", "chat": CHAT})
    )

    # 发的是卡，不是文字
    assert not sender.sent, "又退回纯文本了：角色列表该给可点的按钮"
    assert sender.cards.calls, "没发出角色卡"
    assert out["toast"]["type"] == "success"


def test_today_button_actually_runs_the_view(wired):
    """点「今天该做什么」要**真的跑**视图，不是回一句「发 /today」。

    转手 `_run_view_choice` 的收益就在这里：菜单与直接打 `/today` 的输出
    逐字一致，因为走的是同一份渲染（12.1.1 的硬约束）。
    """
    sender, channel, _ = wired
    bridge_mod._card_action(
        _click({"action": MENU_ACTION, "choice": "today", "chat": CHAT})
    )
    assert channel.seen == (CHAT, ALICE, "/today"), "没走只读视图路径"
    assert not any("发 /today" in t for _, t in sender.sent), (
        "又把人踢回去打字了"
    )


def test_role_query_and_role_button_agree(wired):
    """打字问角色与点角色按钮，**必须发出同一张卡**。

    这是「一张分派表 + 一份发卡实现」的可观测后果。第一版它们漂过
    （卡 vs 文本），而两边各自的单测都绿 —— 因为没有任何一条断言
    「两条路发的东西相同」。
    """
    sender, channel, app = wired
    app.roles.create("工作")

    # 按钮路
    bridge_mod._card_action(
        _click({"action": MENU_ACTION, "choice": "roles", "chat": CHAT})
    )
    from_button = [c["items"] for c in sender.cards.calls]

    # 打字路：走同一个 _send_role_card_via
    sender.cards.calls.clear()
    bridge = bridge_mod.FeishuBridge.__new__(bridge_mod.FeishuBridge)
    bridge.sender = sender
    bridge._send_role_card(_FakeMsg())
    from_text = [c["items"] for c in sender.cards.calls]

    assert from_button, "按钮路没发卡"
    assert from_button == from_text, (
        f"两条路发出的角色卡不同：按钮={from_button} 打字={from_text}"
    )


class _FakeMsg:
    """`_send_role_card` 读的就这两个字段。"""

    sender_open_id = ALICE
    chat_id = CHAT


def test_menu_never_ends_by_asking_you_to_type(wired):
    """规范性：菜单项**不许以「发 /xxx ……」把人踢回打字**（12.1.3）。

    菜单的意义是「识别优于回忆」。回一句命令让用户自己去打，等于把菜单
    刚省掉的那一步又塞回去。

    `help` 与 `delegate` 允许出现命令，但必须是**可直接照着发的那一条**
    （带反引号的完整命令），而不是「你去发 /xxx」这种指路。

    判据刻意只用「正文里有没有一条可直接照发的命令」。原先还想断言
    「不许以句号收尾」，那是坏判据 —— `完整命令表发 /help。` 收尾完全正当，
    而它确实让那条测试红了。**测试自己写错，比代码有 bug 更需要记下来。**
    """
    for choice in ("help", "delegate", "today", "roles"):
        plan = bridge_mod._menu_dispatch(choice)
        if plan.get("view") or plan.get("roles"):
            # 走视图或发卡：根本没有正文，不构成死胡同
            continue
        text = plan.get("text") or ""
        assert "`/" in text, (
            f"{choice} 的正文里没有一条可直接照发的命令：{text!r}"
        )


def test_view_and_card_choices_have_no_text_at_all(wired):
    """``today`` / ``roles`` 是**两条路都给结果**的菜单项：一条正文都不发。

    这是 12.1.3 那条规范最容易被绕开的地方 —— 表里有 ``text`` 兜底，实现
    就可能顺手「text 为空也给点什么」。明确断言它们**没有正文**，比断言
    正文内容更能守住结构。
    """
    for choice in ("today", "roles"):
        plan = bridge_mod._menu_dispatch(choice)
        assert not plan.get("text"), (
            f"{choice} 不该有正文：它要么跑视图要么发卡，两种都不需要文字"
        )


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


def test_retired_card_never_tells_you_to_click(wired):
    """退役卡面**不许教用户操作**——真机上被抓出来的缺陷。

    点「我有哪些角色」后，入口卡回绿并写「**卡已经发在上面了**，点一下就行」，
    而下面紧接着就是那张角色卡。也就是**教用户去点一张他已经在看的卡**，
    还违反本项目自己定的「菜单不教用户操作」。

    ## 为什么之前没抓住

    `test_roles_button_sends_a_card_not_a_text_list` 断言的是
    ``not sender.sent`` —— 那是「没发**文字气泡**」。可这句话**根本不是气泡**，
    它是**退役卡的正文**，装在回调响应里。于是那条断言一直是绿的，缺陷从
    断言的缝里漏了过去，还顺手改对了 ``_send_role_card_via`` 的返回类型
    却没让任何一条测试变红。

    教训：**断言要落在真正出问题的那个通道上。** 这里断言整份响应里没有
    「点一下」这类指示语——比逐字匹配文案更抗改写，又比 ``not sender.sent``
    抓得住真缺陷。
    """
    sender, channel, app = wired
    app.roles.create("工作")

    out = bridge_mod._card_action(
        _click({"action": MENU_ACTION, "choice": "roles", "chat": CHAT})
    )

    blob = repr(out)
    assert "点一下" not in blob, f"退役卡面在教用户操作：{blob}"
    assert "点一下就行" not in blob, f"退役卡面在指路：{blob}"
    # 角色卡本身照发——修掉废话不等于把结果也删了
    assert sender.cards.calls, "角色卡没发出去"


#: 卡片文案里**不许出现**的指示语：它们叫人去点他已经在看的按钮。
#:
#: 划线的理由（别扩大到「在下面」这类**位置陈述**）：「结果已发在上面」是
#: 如实报告内容在哪，而「点一下就行」是**叫人做一个他刚做完的动作**。
#: 前者有用，后者只是把界面写成了说明书。
BANNED_IN_CARD = ("点一下", "点一个", "点它")


def test_role_card_note_does_not_teach_the_user_to_click(wired):
    """角色卡**自己的 note** 也不许教用户点按钮 —— 真机截图顶出来的。

    上一轮只守了退役卡那一个位置就宣布「这一类修完了」，结果同一句
    「点一个看它下面的事务」原封不动留在角色卡的 note 上，而且后半句
    「名字已经按角色列在上面了」**还是错的**——名字**就是那些按钮**，
    不在「上面」。用户会低头再找一遍。

    所以这里断言的是**卡面 note 本身**，不是回调响应。
    """
    sender, channel, app = wired
    app.roles.create("工作")

    bridge_mod._card_action(
        _click({"action": MENU_ACTION, "choice": "roles", "chat": CHAT})
    )

    assert sender.cards.calls, "角色卡没发出去"
    note = sender.cards.calls[0]["note"]
    for bad in BANNED_IN_CARD:
        assert bad not in note, f"角色卡 note 在教用户操作：{note!r}"
    # 名字就是按钮，不在「上面」——那句话会把人引到错的地方去找
    assert "上面" not in note, f"note 指向了错误位置：{note!r}"
