"""第 5 步「交简报」的**确认卡点击路径**：`_run_delegate_confirm`。

## 这个文件守的是什么

`test_brief_consume.py` 证明「收到简报会发确认卡」。它**不**证明点了
「建这条委派」之后真的会建 —— 那是这个文件的事。

## 三条要害

1. **按点击者取态**（``claim(who)``）。确认卡发在**群**里，全员可见。
   若按 ``chat_id`` 或全局单槽取态，群里另一个人点一下，取到的就是
   **上一个人的**简报，而 ``delegate_requested_by`` 记的是点击者 ——
   一次张冠李戴的建事务。
2. **路由回命令，不自己建**。12.1.1「一份能力一份实现」：这里若自己调
   ``tasks.create``，就成了第二份委派实现，而分叉极隐蔽。
3. **单次消费**。取即删让重复点击 / 重投拿不到东西，不会建出两条委派。
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
from freeagent.feishu.sender import (  # noqa: E402
    BRIEF_CONFIRM_ACTION,
    COMMAND_ACTION,
    MENU_ACTION,
    PLAN_CONFIRM_ACTION,
    SELECT_ACTION,
    VIEW_CHOICE_ACTION,
)
from freeagent.services import oc_selection as sel  # noqa: E402

ALICE = "ou_Alice_12345678"
BOB = "ou_Bob_87654321"

SNAPSHOT = dict(
    project="D:/p/freeagent", model="opencode/big-pickle",
    agent="Sisyphus - ultraworker", variant="high",
)


class _Sender:
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
    """鸭子类型替身。桥接会摸 ``task_action`` —— 缺字段就是
    ``AttributeError``，刻意不兜底（与 ``test_brief_consume.py`` 同一约定）。

    ``task_action`` 是**建委派成功**的信号（``_cmd_delegate`` 只在成功时置位），
    所以判成败靠它，而不是去匹配 ``text`` 里有没有「已建委派事务」字样。
    """

    def __init__(self, text: str, *, denied: bool = False,
                 task_action: tuple[str, str] | None = None) -> None:
        self.text = text
        self.denied = denied
        self.task_action = task_action


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
    """记录被路由的命令。`calls` 是这个文件的核心断言对象。"""

    def __init__(self, reply: _Reply, *, allowed: bool = True,
                 roles=("默认脉络",)) -> None:
        self.reply = reply
        self.calls: list[str] = []
        self.allowed = allowed
        self.app = _App(roles)

    def handle(self, chat_id, sender_ids, text, *, event_id=None):
        self.calls.append(text)
        if not self.allowed:
            return _Reply("没有权限。", denied=True)
        return self.reply

    def is_allowed(self, sender_ids) -> bool:
        return self.allowed


@pytest.fixture(autouse=True)
def _clear_pending():
    brief_pending._PENDING.clear()
    yield
    brief_pending._PENDING.clear()


@pytest.fixture
def wired(monkeypatch):
    sender = _Sender()
    # 成功的回话**必须**带 ``task_action`` —— 桥接靠它判「建出来了」。
    channel = _Channel(_Reply(
        "已建委派事务 abc12345", task_action=("abc12345", "改一下登录页"),
    ))
    monkeypatch.setattr(bridge_mod, "_card_sender", sender, raising=False)
    monkeypatch.setattr(bridge_mod, "_card_channel", channel, raising=False)
    # 快照核对读的是 ``oc_selection.json``。默认让它与 arm 的快照一致，
    # 否则每条 create 都会撞上「选择已变」那条分支。
    monkeypatch.setattr(
        bridge_mod, "_oc_load_selection",
        lambda: sel.Selection(**SNAPSHOT),
    )
    return sender, channel


def _arm_and_fill(who: str = ALICE, brief: str = "改一下登录页") -> None:
    brief_pending.arm(who, chat_id="oc_chat", **SNAPSHOT)
    brief_pending.fill_brief(who, brief)


def _click(choice: str, *, who: str = ALICE, chat: str = "oc_chat"):
    return bridge_mod._run_delegate_confirm(
        {"choice": choice, "chat": chat}, who=who,
    )


def _heading(resp) -> str:
    return resp["card"]["data"]["header"]["title"]["content"]


# --------------------------------------------------------------------------- #
# create：路由成 /delegate
# --------------------------------------------------------------------------- #
def test_create_routes_delegate_command(wired):
    sender, channel = wired
    _arm_and_fill()
    resp = _click("create")

    assert channel.calls == [
        "/delegate D:/p/freeagent | 默认脉络 | 改一下登录页"
    ], "必须路由成 /delegate，而不是自己建事务"
    assert brief_pending.peek(ALICE) is None, "取即删：单次消费"
    assert sender.texts and sender.texts[-1][1] == "已建委派事务 abc12345"
    assert _heading(resp) == "已建委派"


def test_create_without_pending_is_expired(wired):
    """没 arm 过就点「建这条委派」—— 绝不执行，如实说已过期。"""
    sender, channel = wired
    resp = _click("create")
    assert not channel.calls, "取不到待填简报时绝不能建"
    assert _heading(resp) == "已过期"


def test_double_click_creates_once(wired):
    """取即删：第二次点击拿不到东西，不会建出两条委派。"""
    sender, channel = wired
    _arm_and_fill()
    _click("create")
    resp = _click("create")
    assert len(channel.calls) == 1, "重复点击建了两条委派"
    assert _heading(resp) == "已过期"


def test_snapshot_drift_blocks_create(wired, monkeypatch):
    """快照与当前选择不一致 —— 提示重选，**不建委派**。

    这是「回显与实际派发同源」的另一半：回显用快照，派发前再核对一次，
    防止用户在 Web 编程页改了选择而飞书这边还按旧快照建。
    """
    sender, channel = wired
    _arm_and_fill()
    monkeypatch.setattr(
        bridge_mod, "_oc_load_selection",
        lambda: sel.Selection(project="D:/p/other", model="opencode/big-pickle",
                              agent="Sisyphus - ultraworker", variant="high"),
    )
    resp = _click("create")
    assert not channel.calls, "快照漂移时绝不能建"
    assert _heading(resp) == "选择已变"


def test_no_roles_blocks_create(wired):
    """没有任何脉络 —— 命令会因角色为空而回用法，那对用户毫无意义。"""
    sender, channel = wired
    channel.app.roles.names = []
    _arm_and_fill()
    resp = _click("create")
    assert not channel.calls
    assert _heading(resp) == "没有脉络"


def test_brief_with_pipe_still_routes(wired):
    """简报里含带空格的 ``|`` —— 优雅降级：不崩、不注入，命令照常被调用。

    ``parse_delegate_args`` 会把裸 ``|`` 当分段符，所以简报会被吞掉一个
    ``|``、两侧拼成一个空格。这是**已知边界**，不是崩溃。
    """
    sender, channel = wired
    _arm_and_fill(brief="改 a | b 布局")
    _click("create")
    assert len(channel.calls) == 1
    assert channel.calls[0].startswith("/delegate D:/p/freeagent | 默认脉络 | ")


def test_denied_reply_reports_not_created(wired):
    """被拒就**如实说没建** —— 绝不能回一句「已建委派」。"""
    sender, channel = wired
    channel.reply = _Reply("没有权限。", denied=True)
    _arm_and_fill()
    resp = _click("create")
    assert _heading(resp) == "没有建"


# --------------------------------------------------------------------------- #
# cancel / retry / 认不出的 choice
# --------------------------------------------------------------------------- #
def test_cancel_discards(wired):
    sender, channel = wired
    _arm_and_fill()
    resp = _click("cancel")
    assert brief_pending.peek(ALICE) is None, "取消要清掉待填态"
    assert not channel.calls
    assert _heading(resp) == "好，先不做了"


def test_retry_discards_and_resends_prompt(wired):
    sender, channel = wired
    _arm_and_fill()
    resp = _click("retry")
    assert brief_pending.peek(ALICE) is None, "改一下要清掉旧简报"
    assert not channel.calls
    assert sender.cards and sender.cards[-1]["header"]["title"]["content"] == "要做的事？"
    assert _heading(resp) == "改一下"


def test_unknown_choice_only_toasts(wired):
    """认不出的 choice **只把卡收掉**，不发话 —— 说「已确认」而没做更糟。"""
    sender, channel = wired
    _arm_and_fill()
    resp = _click("nonsense")
    assert "card" not in resp, "认不出时不该替换卡片"
    assert resp["toast"]["type"] == "error"
    assert not channel.calls
    assert brief_pending.peek(ALICE) is not None, "认不出时不该消费简报"


# --------------------------------------------------------------------------- #
# 白名单
# --------------------------------------------------------------------------- #
def test_non_whitelisted_clicker_gets_nothing(wired):
    """白名单外的人点确认卡 —— 不执行、不回话、不消费。

    确认卡在群里全员可见，不判就会向白名单外确认这里有个 bot，
    而且会消费别人的简报。
    """
    sender, channel = wired
    _arm_and_fill()
    channel.allowed = False
    resp = _click("create", who=BOB)
    assert not channel.calls, "白名单外的人点了不该建"
    assert not sender.texts
    assert "card" not in resp
    assert brief_pending.peek(ALICE) is not None, "不该消费别人的简报"


# --------------------------------------------------------------------------- #
# 接线：确认卡排在凭据流程之前
# --------------------------------------------------------------------------- #
def test_create_is_dispatched_before_credentials(wired):
    """确认卡载荷里**没有** ``id``，不是凭据型卡片 —— 必须排在凭据流程之前。"""
    sender, channel = wired
    _arm_and_fill()
    data = {"event": {
        "action": {"value": {"action": BRIEF_CONFIRM_ACTION,
                             "choice": "create", "chat": "oc_chat"}},
        "operator": {"open_id": ALICE},
    }}
    resp = bridge_mod._card_action(data)
    assert channel.calls, "确认卡没被派发到 _run_delegate_confirm"
    assert resp.get("card") is not None


def test_brief_confirm_action_is_distinct():
    """action 不能与别的卡片同名 —— 否则「哪个 choice 能当命令执行」失去单一判据。"""
    assert BRIEF_CONFIRM_ACTION not in (
        SELECT_ACTION, MENU_ACTION, PLAN_CONFIRM_ACTION,
        COMMAND_ACTION, VIEW_CHOICE_ACTION,
    )
