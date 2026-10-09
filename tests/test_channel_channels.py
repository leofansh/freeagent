"""通道维度：``_ChannelCtx.channel`` 与「回推目的地」的判定（设计文档 11.11）。

守的是什么
----------
一条**实测出来**的错误：从 WEB 发起的委派会把常量 ``"web"`` 写进
``Task.delegate_chat_id`` —— 而那一列的语义是「往哪儿发消息」。执行器于是
拿着一个不是会话 ID 的串去发飞书消息，打出一条**指向错误方向**的警告
（「缺少 FEISHU_APP_ID」——而凭据一直都在）。

所以这里钉三件事：

1. 只有**能推送**的通道才落 ``delegate_chat_id``；
2. WEB / 终端**不落** —— 它们的送达方式是「落库 + 拉取」，不是降级；
3. ``PUSH_CHANNELS`` 是一份**名单**，加通道改一处，不是散落的等值比较。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from freeagent.cli.app import (
    CHANNEL_FEISHU,
    CHANNEL_TERMINAL,
    CHANNEL_WEB,
    PUSH_CHANNELS,
    Repl,
    _ChannelCtx,
)
from freeagent.services import channel as ch
from freeagent.services.delegate import DelegationPolicy


@pytest.fixture()
def project(app, tmp_path: Path) -> Path:
    """一个真的存在的目录，并把它配进白名单。

    白名单是**必须**的：``_create_delegation`` 的第一道闸门就是它，
    不过的话拿不到 task（也就测不到回推地址）。
    """
    import dataclasses

    target = tmp_path / "proj"
    target.mkdir()
    app.config = dataclasses.replace(app.config, delegate_projects=(str(target),))
    return target


@pytest.fixture()
def role(app):
    """委派必须归属到一条脉络 —— 空列表下 ``_create_delegation`` 直接拒绝。"""
    return app.roles.create("沙箱通道")


def _delegate(app, project: Path, *, ctx: _ChannelCtx | None):
    """走**唯一**那条创建入口（与命令层、WEB 端点同一条）。"""
    repl = Repl(app, out=__import__("io").StringIO())
    repl.channel_ctx = ctx
    task, err, role, _ = repl._create_delegation(str(project), "", "加个乘法")
    assert err is None, err
    return task


class TestContextCarriesChannel:
    def test_default_is_terminal(self) -> None:
        """默认必须是**最保守**的那个 —— 终端不推送。

        理由：构造点漏传时，错误的后果是「少推一次」，而不是「推到错地方」。
        """
        assert _ChannelCtx("c").channel == CHANNEL_TERMINAL

    def test_web_is_not_a_push_channel(self) -> None:
        assert CHANNEL_WEB not in PUSH_CHANNELS

    def test_feishu_is_a_push_channel(self) -> None:
        assert CHANNEL_FEISHU in PUSH_CHANNELS

    def test_terminal_is_not_a_push_channel(self) -> None:
        assert CHANNEL_TERMINAL not in PUSH_CHANNELS


class TestOnlyPushChannelsGetADestination:
    """核心回归：``delegate_chat_id`` 只装**真会话 ID**。"""

    def test_web_does_not_write_a_fake_address(self, app, project: Path, role) -> None:
        """WEB 的 ``chat_id`` 是常量 ``"web"``，不是会话 ID —— 不许落库。"""
        task = _delegate(
            app, project,
            ctx=_ChannelCtx(chat_id=CHANNEL_WEB, sender_open_id="",
                            channel=CHANNEL_WEB),
        )
        assert task.delegate_chat_id is None, (
            "把常量写进了语义为「往哪儿发消息」的列 —— "
            "执行器会拿它去发飞书消息（实测过）"
        )

    def test_feishu_writes_the_real_chat_id(self, app, project: Path, role) -> None:
        """反向：飞书那条路**不能**被误伤 —— 回推是它的闭环。"""
        task = _delegate(
            app, project,
            ctx=_ChannelCtx(chat_id="oc_real_chat", sender_open_id="ou_x",
                            channel=CHANNEL_FEISHU),
        )
        assert task.delegate_chat_id == "oc_real_chat"

    def test_terminal_writes_nothing(self, app, project: Path, role) -> None:
        """终端本来就没有来源会话（``channel_ctx`` 是 None）。"""
        task = _delegate(app, project, ctx=None)
        assert task.delegate_chat_id is None
        assert task.delegate_requested_by is None

    def test_requested_by_is_unchanged_by_this_rule(
        self, app, project: Path, role
    ) -> None:
        """「谁发起的」**不跟着**一起收窄（理由不同，见 app.py 注释）。

        飞书发起时空串要变 ``None``、有值要保留 —— 这条如果被顺手改成
        跟着 ``PUSH_CHANNELS``，11.9.7 的「只有发起人能批」会先坏掉。
        """
        task = _delegate(
            app, project,
            ctx=_ChannelCtx(chat_id="oc_c", sender_open_id="ou_abc",
                            channel=CHANNEL_FEISHU),
        )
        assert task.delegate_requested_by == "ou_abc"


class TestChannelServiceDeclaresItsChannel:
    def test_default_matches_the_cli_constant(self) -> None:
        """刻意重复的两份值必须相等（同 ``DEFAULT_DEDUP_*`` 那个惯例）。

        ``services/channel.py`` 不能 import ``cli.app``（那层很重），
        所以只能复制。复制而不锁 = 迟早漂移，而漂移的症状是
        「通道标识对不上 → 判定成不推送 → 飞书结果不再回来」。
        """
        assert ch.DEFAULT_CHANNEL == CHANNEL_FEISHU

    def test_service_passes_its_channel_into_the_context(self, app) -> None:
        svc = ch.ChannelService(app, channel="wechat")
        assert svc.channel == "wechat"


class TestNoChannelKnowledgeLeaksIntoDelegate:
    def test_delegate_does_not_import_web_or_cli(self) -> None:
        """执行器**不该**知道有哪些通道 —— 它只看「有没有目的地」。

        这条守卫的意义：一旦 ``delegate.py`` 开始 ``from .web import ...``
        或 import ``cli``，方向就反了（执行器依赖前端层），而那时「哪些通道
        能推」会变成执行器的知识 —— 它本来只需要读一个字段。
        """
        src = Path(__file__).resolve().parents[1] / "src" / "freeagent" / "delegate.py"
        text = src.read_text(encoding="utf-8")
        for banned in ("from .web", "from ..web", "import .web",
                       "from .cli", "freeagent.web", "freeagent.cli"):
            assert banned not in text, f"delegate.py 不该出现 {banned!r}"

    def test_policy_default_is_still_disabled(self) -> None:
        """顺带钉住：这次改动没碰任何闸门的默认值。"""
        assert DelegationPolicy().enabled is False
