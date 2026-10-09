"""卡片按钮 → 合成命令的守卫（设计文档 11.9.8）。

## 这一刀最要紧的是**窄白名单**

按钮 ``value`` 是**客户端回传**的，而 :meth:`ChannelService.handle` 见 ``/``
开头就当命令派发。所以照单全收等于开一个「点一下就能执行任意命令」的口子：
伪造一个载荷就能借桥接跑到 ``/merge``、``/role-del``、``/delegate`` 上。

白名单里的 ``who`` 校验挡的是「**谁**在点」，而窄白名单挡的是「**能点什么**」
—— 两件事，少一件就留了口子。所以这里最要紧的用例是
:meth:`TestNarrowAllowlist`。

## 为什么驱动**真实**的 ``_run_card_command``

这一刀跨三段：卡片载荷 → 桥接点击 → :class:`Repl` 改状态。任何一段形状对不上，
静态测试都还绿着，而真机表现为「点了没反应」。所以只替掉飞书传输与卡片渲染。
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from freeagent.app import build_app
from freeagent.feishu import bridge as B
from freeagent.feishu.config import ENV_ALLOWED, ENV_APP_ID, ENV_APP_SECRET, load_config
from freeagent.feishu.events import IncomingMessage
from freeagent.services.channel import ChannelService

CHAT = "oc_chat"
WHO = "ou_owner"
STRANGER = "ou_stranger"


def _cfg():
    """一份够桥接构造用的配置。

    刻意走真的 :func:`load_config` 而不是手搓一个对象：``FeishuBridge``
    要读 ``config.allowed_users`` 与 ``base_url``，手搓的那个会在字段名
    漂移时报``AttributeError`` —— 而那与本文件要测的东西无关。
    """
    return load_config({
        ENV_APP_ID: "cli_x",
        ENV_APP_SECRET: "secret",
        ENV_ALLOWED: f"{WHO},{STRANGER}",
    })


class _Sender:
    """只实现点击路径用到的那个方法。"""

    def __init__(self) -> None:
        self.texts: list[tuple[str, str]] = []

    def send_text(self, chat_id: str, text: str) -> None:
        self.texts.append((chat_id, text))

    @property
    def last(self) -> str:
        return self.texts[-1][1] if self.texts else ""


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    """桥接 + 一个已存在的会话与一条事务。"""
    app = build_app(tmp_path / "a.db")
    role = app.roles.create("工作")
    channel = ChannelService(app, allowed_senders=frozenset({WHO}))
    sender = _Sender()
    monkeypatch.setattr(B, "_card_channel", channel, raising=False)
    monkeypatch.setattr(B, "_card_sender", sender, raising=False)

    task = app.tasks.create("把用法那节补上", [role.id])
    app.tasks.start(task.id)

    yield app, channel, sender, task
    app.close()


def _click(cmd: str, task_id: str, *, who: str = WHO) -> dict[str, Any]:
    """按**实测**形状造点击载荷（与 :func:`test_card_action.payload` 同形）。"""
    return B._run_card_command(
        {"action": "command", "cmd": cmd, "id": task_id, "chat": CHAT},
        who=who,
    )


def _has_buttons(card: dict) -> bool:
    return any(el.get("tag") == "action" for el in card.get("elements", []))


# --------------------------------------------------------------------------- #
# 点一下，状态真的变
# --------------------------------------------------------------------------- #

class TestSynthesizedCommand:
    def test_done_click_marks_the_task_done(self, wired):
        app, _channel, _sender, task = wired
        _click("/done", task.id)
        assert app.tasks.get(task.id).state.value == "done"

    def test_pause_click_moves_it_back_to_inbox(self, wired):
        app, _channel, _sender, task = wired
        _click("/pause", task.id)
        assert app.tasks.get(task.id).state.value == "inbox"

    def test_start_click_works_on_a_paused_task(self, wired):
        """``/pause`` 之后还能 ``/start`` 回来 —— 三个按钮是同一条命令路径。"""
        app, _channel, _sender, task = wired
        _click("/pause", task.id)
        _click("/start", task.id)
        assert app.tasks.get(task.id).state.value == "active"

    def test_the_exact_command_text_is_what_repl_receives(self, wired):
        """载荷是 ``cmd`` + ``id`` 两个短字段，**不是** JSON。

        因为 ``Repl._command`` 按空白切分：JSON 里的空格会把一条命令切成
        好几段，症状是「点了没反应」而日志干净。
        """
        app, channel, sender, task = wired
        seen: list[str] = []

        real = channel.handle

        def spy(chat_id, sender_ids, text, **kw):
            seen.append(text)
            return real(chat_id, sender_ids, text, **kw)

        channel.handle = spy  # type: ignore[method-assign]
        _click("/done", task.id)
        assert seen == [f"/done {task.id}"]

    def test_user_sees_the_reply_in_the_chat(self, wired):
        """点了之后要**在窗口里**说一句话，而不只是卡片变个样。"""
        _app, _channel, sender, task = wired
        _click("/done", task.id)
        assert sender.last, "点了没回话 —— 用户会以为没生效"

    def test_card_loses_its_buttons_after_the_click(self, wired):
        """留着可点的按钮等于骗人（实测过的坑）。"""
        _app, _channel, _sender, task = wired
        resp = _click("/done", task.id)
        assert "toast" in resp and "card" in resp
        assert not _has_buttons(resp["card"]), "已处理的卡片还留着可点的按钮"


# --------------------------------------------------------------------------- #
# 窄白名单 —— 本文件最要紧的一段
# --------------------------------------------------------------------------- #

class TestNarrowAllowlist:
    @pytest.mark.parametrize("cmd", sorted(B._CARD_COMMAND_ALLOWLIST))
    def test_the_three_allowed_commands_are_in_the_list(self, cmd):
        """文档承诺的是「开始 / 做完 / 放一放」这三个，别偷偷多一个。"""
        assert cmd in B._CARD_COMMAND_ALLOWLIST

    @pytest.mark.parametrize(
        "cmd",
        ["/merge", "/role-del", "/delegate", "/drop", "/dod", "/note",
         "/redispatch", "/pair"],
    )
    def test_commands_outside_the_allowlist_are_refused(self, wired, cmd):
        """**伪造载荷不得变成任意命令执行器。**

        每一个都是真能改东西的命令：``/merge`` 与 ``/role-del`` 动组织结构、
        ``/delegate`` 能触发本地代码执行、``/redispatch`` 能让执行器再派一次。
        """
        _app, _channel, _sender, task = wired
        resp = _click(cmd, task.id)
        assert resp.get("toast", {}).get("type") == "error", (
            f"{cmd} 竟然没被拒 —— 窄白名单漏了它"
        )
        assert "不接受" in resp["toast"]["content"]

    def test_a_refused_command_changes_nothing(self, wired):
        """被拒之后那条事务必须**原样**—— 光回一句错不够。"""
        app, _channel, _sender, task = wired
        before = app.tasks.get(task.id).state.value
        _click("/merge", "x")   # 合并角色会改组织结构
        assert app.tasks.get(task.id).state.value == before

    def test_command_lookalike_is_refused(self, wired):
        """``/started`` 不是 ``/start``。

        写白名单时最容易漏的就是这种前缀/后缀变体：一个没闭合的集合
        （只 ``startswith``）会让 ``/started`` 混进来。
        """
        _app, _channel, _sender, task = wired
        resp = _click("/started", task.id)
        assert resp.get("toast", {}).get("type") == "error"


# --------------------------------------------------------------------------- #
# 载荷不完整 / 无通道：必须**不猜**
# --------------------------------------------------------------------------- #

class TestIncompletePayload:
    def test_missing_command_is_refused(self, wired):
        _app, _channel, _sender, task = wired
        resp = B._run_card_command(
            {"action": "command", "id": task.id, "chat": CHAT}, who=WHO,
        )
        assert resp.get("toast", {}).get("type") == "error"

    def test_missing_chat_is_refused(self, wired):
        _app, _channel, _sender, task = wired
        resp = B._run_card_command(
            {"action": "command", "cmd": "/done", "id": task.id}, who=WHO,
        )
        assert resp.get("toast", {}).get("type") == "error"

    def test_no_channel_says_so_and_does_not_claim_success(self, wired,
                                                          monkeypatch):
        """⚠️ 绝不能报「已完成」。

        那是这类处理器最危险的失败方向：用户以为改完了，而库里什么都没变。
        """
        app, _channel, _sender, task = wired
        monkeypatch.setattr(B, "_card_channel", None, raising=False)
        monkeypatch.setattr(B, "_card_sender", None, raising=False)
        resp = _click("/done", task.id)
        assert resp.get("toast", {}).get("type") == "error"
        assert app.tasks.get(task.id).state.value == "active", (
            "没连上通道却真的改了状态 —— 那是不可能的，说明判据写错了"
        )


# --------------------------------------------------------------------------- #
# 白名单：点的人必须在名单里
# --------------------------------------------------------------------------- #

class TestClickerMustBeAllowed:
    def test_stranger_click_does_not_execute(self, wired):
        """白名单里的判定在 ``handle`` 内部，所以这条路**也**必须拦住。

        ⚠️ 这一条最容易在重构时被漏掉：将来若把派发改成直接调
        ``Repl``（跳过 ``handle``），白名单校验会**一起消失**。
        """
        app, _channel, _sender, task = wired
        _click("/done", task.id, who=STRANGER)
        assert app.tasks.get(task.id).state.value == "active", (
            "陌生人点一下就改了状态 —— 白名单被绕过了"
        )

    def test_stranger_click_is_told_it_did_not_run(self, wired):
        """被拒要说「没有执行」，而不是报成功。"""
        _app, _channel, _sender, task = wired
        resp = _click("/done", task.id, who=STRANGER)
        assert resp.get("toast", {}).get("type") == "error"


# --------------------------------------------------------------------------- #
# 分发链：action 名要真的被认出来
# --------------------------------------------------------------------------- #

class TestDispatch:
    def test_command_action_is_routed_not_dropped(self, wired, monkeypatch):
        """``COMMAND_ACTION`` 必须**排在凭据分支之前**被认出来。

        载荷里没有 ``id`` 凭据，若落到凭据分支就会以「载荷不认」被丢弃 ——
        症状是「点了没反应」，静态测试若只测 ``_run_card_command`` 就发现不了。
        """
        app, channel, _sender, task = wired
        seen: list[str] = []
        real = channel.handle

        def spy(chat_id, sender_ids, text, **kw):
            seen.append(text)
            return real(chat_id, sender_ids, text, **kw)

        channel.handle = spy  # type: ignore[method-assign]
        monkeypatch.setattr(B, "_card_conn", app.conn, raising=False)
        resp = B._card_action(
            {
                "header": {"event_id": "ev-1"},
                "event": {
                    "operator": {"open_id": WHO},
                    "action": {
                        "value": {
                            "action": "command", "cmd": "/done",
                            "id": task.id, "chat": CHAT,
                        },
                        "tag": "button",
                    },
                    "context": {"open_chat_id": CHAT},
                },
            }
        )
        assert seen == [f"/done {task.id}"], "COMMAND_ACTION 没被认出来"
        assert resp.get("toast", {}).get("type") == "success"


# --------------------------------------------------------------------------- #
# 发卡入口 —— 光「能点」不够，得**真有卡可点**
# --------------------------------------------------------------------------- #

def _delegate_app(tmp_path):
    """一个**能建委派**的 app：白名单里要有那个项目，且角色必须先存在。

    两条缺一不可，而缺第二条的失败方式很隐蔽：``/delegate`` 里的
    ``_match_role`` 找不到角色就 ``return``，事务**根本不会建出来**，
    于是「卡片没发」与「角色没匹配」看起来一模一样。
    """
    app = build_app(tmp_path / "a.db")
    app.config = dataclasses.replace(app.config, delegate_projects=(str(tmp_path),))
    app.roles.create("工作")
    return app


class TestCardIsActuallySent:
    """/delegate 之后**真的发那张卡**（设计文档 11.9.8 的兑现点）。"""

    def test_delegate_sets_task_action_on_the_reply(self, tmp_path):
        """``/delegate`` 建好委派 ⇒ 本轮要发动作卡。

        卡片上那三个按钮替代的是正文里那句「确认要派的话：/start<id>」——
        原来要用户**手打**，这里才叫「点一下即可」。
        """
        app = _delegate_app(tmp_path)
        try:
            channel = ChannelService(app, allowed_senders=frozenset({WHO}))
            reply = channel.handle(
                CHAT, WHO, f"/delegate {tmp_path} | 工作 | 加个登录",
                event_id="ev-1",
            )
            assert reply.task_action is not None, "建了委派却没有动作卡"
            task_id, title = reply.task_action
            assert task_id and title, "动作卡必须带事务 id 与标题"
        finally:
            app.close()

    def test_the_card_carries_the_task_that_was_just_created(self, tmp_path):
        """卡上的 id 必须是**刚建的那一条**，不是随便一条。

        载荷带错id 的症状特别难查：用户点了「做完」，系统去改了**另一条**事务，
        而他自己的那条纹丝不动 —— 看起来像「按钮坏了」。
        """
        app = _delegate_app(tmp_path)
        try:
            channel = ChannelService(app, allowed_senders=frozenset({WHO}))
            reply = channel.handle(
                CHAT, WHO, f"/delegate {tmp_path} | 工作 | 加个登录",
                event_id="ev-1",
            )
            task_id, _ = reply.task_action
            got = app.tasks.get(task_id)
            assert got.project_path, "卡上指的那条不是委派事务"
            assert got.delegate_chat_id == CHAT, "卡的来源会话记错了"
        finally:
            app.close()

    def test_card_does_not_stick_to_the_next_message(self, tmp_path):
        """⚠️ 卡片**不许粘住**。

        它属于「那一条回复」—— 而下一句可能与委派毫无关系。粘住的后果是
        「问天气也长出一张动作卡」，而那张卡上的按钮**真的会改状态**。
        与 ``_last_choices`` 同理，所以每条消息都清（见 ``ChannelService._run``）。
        """
        app = _delegate_app(tmp_path)
        try:
            channel = ChannelService(app, allowed_senders=frozenset({WHO}))
            first = channel.handle(
                CHAT, WHO, f"/delegate {tmp_path} | 工作 | 加个登录",
                event_id="ev-1",
            )
            assert first.task_action is not None
            second = channel.handle(CHAT, WHO, "/today", event_id="ev-2")
            assert second.task_action is None, (
                "动作卡粘到了下一条消息上 —— 用户问天气也会看到「做完」按钮"
            )
        finally:
            app.close()

    def test_ordinary_reply_gets_no_card(self, tmp_path):
        """不是委派的那一轮**不许**发动作卡。

        每条飞书消息都发一张卡，界面就变成「每句话一张卡」，而卡片是用来
        省一次交互的 —— 滥发等于没有。
        """
        app = _delegate_app(tmp_path)
        try:
            channel = ChannelService(app, allowed_senders=frozenset({WHO}))
            reply = channel.handle(CHAT, WHO, "/today", event_id="ev-x")
            assert reply.task_action is None
        finally:
            app.close()

    def test_bridge_really_calls_the_card_sender(self, tmp_path, monkeypatch):
        """端到端那一下：桥接**真的**调了发卡函数。

        前四条锁的是「回复里带了这个意图」，这一条锁的是「桥接照它做了」。
        漏掉的话症状是「一切正常，可飞书里什么卡都没有」—— 而那正是
        这次要修的「能点但没卡可点」。
        """
        app = _delegate_app(tmp_path)
        try:
            channel = ChannelService(app, allowed_senders=frozenset({WHO}))
            sent_cards: list[dict[str, Any]] = []

            def fake_send(sender, **kw):
                sent_cards.append(kw)
                return "om_fake"

            monkeypatch.setattr(B, "send_task_action_card", fake_send)
            bridge = B.FeishuBridge(channel, _Sender(), _cfg(), bot_open_id="ou_bot")
            try:
                bridge._process(IncomingMessage(
                    event_id="ev-1", chat_id=CHAT, sender_open_id=WHO,
                    text=f"/delegate {tmp_path} | 工作 | 加个登录",
                    is_group=False, mentioned=True,
                ))
            finally:
                bridge.stop()
            assert sent_cards, "桥接没有发动作卡 —— 正文说了 /start 但没有按钮可点"
            card = sent_cards[0]
            assert card["chat_id"] == CHAT, "卡片的会话 id 不对"
            assert card["task_id"], "卡片没有指向任何事务"
            assert card["title"], "卡片没有标题"
        finally:
            app.close()