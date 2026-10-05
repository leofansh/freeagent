"""确认卡的**点击侧**闭环：点一下到底发生了什么（设计文档 12.7.2）。

## 为什么必须是闭环测试

这一刀涉及三段：卡片载荷 → 桥接点击 → :class:`Repl` 建事务。任何一段形状
对不上，静态测试都还绿着，而真机上表现为「点了没反应」。

所以这里驱动**真实的** ``_run_plan_confirm``，只替掉飞书传输与卡片渲染，
然后断言**用户看到的话**与**库里实际状态**一致。

## 三条不能违反

1. **点「确认执行」才建事务** —— 发卡那一刻一条都不建。
2. **点「再想想」不建、且计划留着** —— 「再想想」变成「扔掉」就是错的。
3. **载荷认不出时只收卡、不发话** —— 说「已确认」而实际没做，比什么都不说糟得多。
"""

import io
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from freeagent.app import build_app  # noqa: E402
from freeagent.feishu import bridge as B  # noqa: E402
from freeagent.services.channel import ChannelService  # noqa: E402

CHAT = "oc_chat"
WHO = "ou_owner"


class _Sender:
    """只实现点击路径用到的两个方法。"""

    def __init__(self) -> None:
        self.texts: list[tuple[str, str]] = []

    def send_text(self, chat_id: str, text: str) -> None:
        self.texts.append((chat_id, text))

    @property
    def last(self) -> str:
        return self.texts[-1][1] if self.texts else ""


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    """桥接 + 一个已发卡的会话。"""
    app = build_app(tmp_path / "a.db")
    app.roles.create("工作")
    channel = ChannelService(app, allowed_senders=frozenset({WHO}))
    sender = _Sender()
    monkeypatch.setattr(B, "_card_channel", channel, raising=False)
    monkeypatch.setattr(B, "_card_sender", sender, raising=False)

    # 规划 → 发出确认卡
    channel._run(CHAT, WHO, "/mode-plan")
    channel._run(CHAT, WHO, "改一下 README")
    reply = channel.handle(CHAT, WHO, "/mode-build")

    yield app, channel, sender, reply
    app.close()


def _click(sender: _Sender, choice: str) -> dict[str, Any]:
    return B._run_plan_confirm(
        {"action": "plan_confirm", "choice": choice, "chat": CHAT}, who=WHO
    )


# --------------------------------------------------------------------------- #
# 发卡那一刻：一条都不能建
# --------------------------------------------------------------------------- #
def test_sending_the_card_creates_nothing(wired):
    """发卡**不建任何事务** —— 确认之前什么都不该发生。"""
    app, _channel, _sender, reply = wired
    assert reply.plan_confirm, "应当带上待确认的计划行"
    assert app.tasks.list_all() == [], "发卡就建事务 = 确认形同虚设"


def test_plan_stays_until_confirmed(wired):
    """发卡后计划**仍在** —— 否则「再想想」没得可想。"""
    _app, channel, _sender, _reply = wired
    assert channel._repls[CHAT]._plan == ["改一下 README"]


# --------------------------------------------------------------------------- #
# 点「确认执行」
# --------------------------------------------------------------------------- #
def test_click_ok_confirms_through_the_repl(wired):
    """点确认 → **走命令路径**建事务，且计划被清空。

    这里断言的是「命令被正确路由」，所以不钉死建了几条：某句话可能触发
    角色追问（那是正常行为，:meth:`Repl._confirm_plan` 会停下并说明）。
    """
    app, channel, sender, _reply = wired
    _click(sender, "ok")
    # 计划一定被清掉 —— 无论建成了几条
    assert channel._repls[CHAT]._plan == [], "确认后计划应清空"
    # 回话必须落在**某个**结果上（建成了 / 遇到追问）而不是沉默
    assert sender.last, "点完必须回话"


def test_click_ok_consumes_the_pending_snapshot(wired):
    """确认后**待确认状态清空** —— 否则能连点两次建两遍。"""
    _app, channel, sender, _reply = wired
    _click(sender, "ok")
    assert channel._repls[CHAT]._last_plan_confirm == ()


# --------------------------------------------------------------------------- #
# 点「再想想」
# --------------------------------------------------------------------------- #
def test_click_cancel_creates_nothing(wired):
    """点「再想想」**一条都不建**。"""
    app, _channel, sender, _reply = wired
    _click(sender, "cancel")
    assert app.tasks.list_all() == [], "「再想想」不该建事务"


def test_click_cancel_keeps_the_plan(wired):
    """「再想想」之后计划**必须还在** —— 这是那个按钮的全部意义。"""
    _app, channel, sender, _reply = wired
    _click(sender, "cancel")
    assert channel._repls[CHAT]._plan == ["改一下 README"], \
        "「再想想」把计划丢了 —— 那它就不是「再想想」"


def test_cancel_clears_the_snapshot_only(wired):
    """取消**只**清待确认状态，不清计划。"""
    _app, channel, sender, _reply = wired
    _click(sender, "cancel")
    assert channel._repls[CHAT]._last_plan_confirm == ()
    assert channel._repls[CHAT]._plan


# --------------------------------------------------------------------------- #
# 载荷认不出：只收卡，不发话
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("value", [
    {"action": "plan_confirm"},                              # 缺 choice 与 chat
    {"action": "plan_confirm", "choice": "ok"},              # 缺 chat
    {"action": "plan_confirm", "chat": CHAT},                # 缺 choice
    {"action": "plan_confirm", "choice": "", "chat": CHAT},  # 空 choice
])
def test_incomplete_payload_is_refused(wired, value):
    """载荷不完整 → **明确拒绝**，不执行。"""
    app, _channel, _sender, _reply = wired
    out = B._run_plan_confirm(value, who=WHO)
    assert app.tasks.list_all() == [], "载荷不完整却建了事务"
    assert isinstance(out, dict)


def test_unknown_choice_is_refused_without_pretending(wired):
    """认不出的 choice → **只收卡**，绝不报「已确认」。"""
    app, _channel, sender, _reply = wired
    out = B._run_plan_confirm(
        {"action": "plan_confirm", "choice": "rm -rf", "chat": CHAT}, who=WHO
    )
    assert app.tasks.list_all() == []
    # 关键：**不能**说「已确认」—— 那会让用户以为改动做完了
    assert "已确认" not in str(out)


# --------------------------------------------------------------------------- #
# 没有通道时绝不谎报
# --------------------------------------------------------------------------- #
def test_no_channel_refuses_without_claiming_success(tmp_path, monkeypatch):
    """桥接没带库起来 → **拒绝**，不猜、不报成功。

    宁可让用户知道「点不动」，也不能在没落盘的情况下说「已确认」。
    """
    app = build_app(tmp_path / "a.db")
    try:
        monkeypatch.setattr(B, "_card_channel", None, raising=False)
        monkeypatch.setattr(B, "_card_sender", None, raising=False)
        out = B._run_plan_confirm(
            {"action": "plan_confirm", "choice": "ok", "chat": CHAT}, who=WHO
        )
        assert "已确认" not in str(out)
    finally:
        app.close()