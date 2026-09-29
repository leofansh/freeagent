"""飞书通道。

全程离线：一次网络请求都不发，**也不需要装 lark-oapi**。
SDK 回调对象用鸭子类型的假对象代替 —— 这样「核心包零依赖」才真的被测到了，
而不是嘴上说说。

最该锁住的三件事：

1. **白名单失败关闭** —— 空 = 谁都不许指挥本机
2. **事件解析的两个真坑** —— content 是嵌套 JSON、@机器人 会在正文留占位符
3. **3 秒应答** —— 回调只入队，执行在单工作线程里串行做
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from freeagent.app import build_app
from freeagent.feishu import bridge as bridge_mod
from freeagent.feishu.bridge import (
    DEFAULT_LOCK_PORT,
    ENV_LOCK_PORT,
    AlreadyRunning,
    FeishuBridge,
    PortLock,
    lock_port_from_env,
)
from freeagent.feishu.bridge import main as bridge_main
from freeagent.feishu.config import (
    ENV_ALLOWED,
    ENV_APP_ID,
    ENV_APP_SECRET,
    ENV_DOMAIN,
    ENV_POLL,
    ConfigError,
    FeishuConfig,
    load_config,
)
from freeagent.feishu.doctor import check_identity, check_lock_free
from freeagent.feishu.events import (
    Ignored,
    IncomingMessage,
    describe_sdk_shape,
    from_sdk_event,
    parse_event,
)
from freeagent.feishu.identity import (
    BotIdentity,
    resolve_identity,
    save_identity,
)
from freeagent.secrets import SecretStr, as_secret
from freeagent.feishu.sender import MAX_TEXT, FeishuError, FeishuSender
from freeagent.services.channel import ChannelService
from freeagent.services.clock import FrozenClock

#: 仓库根。起子进程测跨进程端口排他时要用它算 ``src`` 路径。
ROOT = Path(__file__).resolve().parents[1]

ALICE = "ou_alice"
BOB = "ou_bob"
#: **不在**任何夹具白名单里的人，用来做「陌生人」测试。
#: 踩过的坑：一开始拿 BOB 当陌生人，可桥接夹具的白名单恰好是
#: ``{ALICE, BOB}`` —— 于是「陌生人被拒」这条安全断言根本没测到东西，
#: 却看起来是绿的。
CAROL = "ou_carol"

#: bot 自己。群聊「@ 的是不是 bot」全靠比对它，所以要有个固定值。
BOT = "ou_bot"


# --------------------------------------------------------------------------- #
# 真实形状的 payload
# --------------------------------------------------------------------------- #
def _payload(
    text="帮我记一下",
    chat_type="p2p",
    mentions=None,
    message_type="text",
    event_type="im.message.receive_v1",
):
    """构造一个**照飞书文档抄的**事件，不是编的形状。

    content 是**嵌套**的 JSON 字符串 —— 飞书就这么发，
    只解一层会拿到一坨带转义的字。
    """
    return {
        "schema": "2.0",
        "header": {
            "event_id": "ev-1",
            "event_type": event_type,
            "create_time": "1700000000000000",
            "token": "tok",
            "app_id": "cli_x",
            "tenant_key": "tk",
        },
        "event": {
            "sender": {
                "sender_id": {"open_id": ALICE, "user_id": "u1"},
                "sender_type": "user",
            },
            "message": {
                "message_id": "om-1",
                "chat_id": "oc_chat1",
                "chat_type": chat_type,
                "message_type": message_type,
                "content": json.dumps({"text": text}, ensure_ascii=False),
                "mentions": mentions or [],
            },
        },
    }


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
class TestConfig:
    def _env(self, **kw):
        base = {
            ENV_APP_ID: "cli_x",
            ENV_APP_SECRET: "secret",
            ENV_ALLOWED: ALICE,
        }
        base.update(kw)
        return base

    def test_reads_from_env(self):
        cfg = load_config(self._env())
        cfg.check_ready()
        assert cfg.app_id == "cli_x"
        assert cfg.allowed_users == frozenset({ALICE})

    def test_missing_app_id_fails(self):
        env = self._env()
        del env[ENV_APP_ID]
        with pytest.raises(ConfigError, match=ENV_APP_ID):
            load_config(env).check_ready()

    def test_missing_secret_fails(self):
        env = self._env()
        del env[ENV_APP_SECRET]
        with pytest.raises(ConfigError, match=ENV_APP_SECRET):
            load_config(env).check_ready()

    def test_empty_allowlist_fails_closed(self):
        """**核心安全属性**：没人被授权 = 全部拒绝，不能默认放行。"""
        with pytest.raises(ConfigError, match=ENV_ALLOWED):
            load_config(self._env(**{ENV_ALLOWED: ""})).check_ready()

    def test_too_many_users_fails(self):
        """配错时宁可报错，也不能默默放行一堆人。"""
        many = ",".join(f"ou_{i}" for i in range(500))
        with pytest.raises(ConfigError, match="上限"):
            load_config(self._env(**{ENV_ALLOWED: many}))

    def test_allowlist_ignores_spaces_and_blanks(self):
        cfg = load_config(self._env(**{ENV_ALLOWED: f" {ALICE} , , {BOB} "}))
        assert cfg.allowed_users == frozenset({ALICE, BOB})

    def test_domain_selects_base_url(self):
        assert load_config(self._env()).base_url.endswith("open.feishu.cn")
        lark = load_config(self._env(**{ENV_DOMAIN: "lark"}))
        assert lark.base_url.endswith("open.larksuite.com")

    def test_bad_domain_fails(self):
        with pytest.raises(ConfigError, match="feishu"):
            load_config(self._env(**{ENV_DOMAIN: "slack"})).check_ready()

    def test_non_integer_poll_fails(self):
        with pytest.raises(ConfigError, match=ENV_POLL):
            load_config(self._env(**{ENV_POLL: "soon"}))

    def test_secret_never_appears_in_repr(self):
        """日志里打 repr 就等于泄密。"""
        cfg = load_config(self._env(**{ENV_APP_SECRET: "sk-super-secret"}))
        assert "sk-super-secret" not in repr(cfg)


# --------------------------------------------------------------------------- #
# 事件解析
# --------------------------------------------------------------------------- #
class TestParseEvent:
    def test_parses_p2p_text(self):
        msg = parse_event(_payload())
        assert isinstance(msg, IncomingMessage)
        assert msg.text == "帮我记一下"
        assert msg.sender_open_id == ALICE
        assert msg.chat_id == "oc_chat1"
        assert msg.is_group is False
        assert msg.mentioned is True          # 私聊不需要 @

    def test_content_is_double_encoded_json(self):
        """**坑 1**：content 是 JSON 字符串，解一次还不够。"""
        raw = _payload()
        assert isinstance(raw["event"]["message"]["content"], str)
        assert raw["event"]["message"]["content"].startswith("{")
        msg = parse_event(raw)
        assert "帮我记一下" in msg.text, "content 只解一层会拿到带转义的字"

    def test_mention_placeholder_is_stripped(self):
        """**坑 2**：@机器人 会以 ``@_user_1`` 留在正文里。

        注意必须传 ``bot_open_id``：判定「@ 的是不是 bot 自己」得靠身份，
        拿不到身份就一律当「没 @ 到 bot」处理（见 :class:`TestMentionGate`）。
        """
        msg = parse_event(_payload(
            text="@_user_1 帮我记一下",
            chat_type="group",
            mentions=[{"key": "@_user_1", "id": {"open_id": "ou_bot"}}],
        ), bot_open_id="ou_bot")
        assert "@_user_1" not in msg.text, "占位符会污染意图识别和角色推断"
        assert msg.text == "帮我记一下"
        assert msg.is_group is True
        assert msg.mentioned is True

    def test_group_without_mention(self):
        msg = parse_event(_payload(chat_type="group"))
        assert msg.is_group is True
        assert msg.mentioned is False

    def test_only_mention_yields_nothing(self):
        assert isinstance(parse_event(_payload(
            text="@_user_1",
            chat_type="group",
            mentions=[{"key": "@_user_1"}],
        )), Ignored)

    def test_other_event_type_returns_none(self):
        """不是收消息事件要静默跳过（None），不是 Ignored。"""
        assert parse_event(_payload(event_type="im.chat.member.bot.added_v1")) is None

    def test_non_text_is_carried_as_unsupported_type(self):
        """非文字**不再被丢弃**，而是带上类型往下传，好让桥接回一句提示。

        原实现返回 ``Ignored``，于是桥接只记一行日志、用户收到彻底沉默。
        用户会以为「它收到了但不想理」，于是去查白名单、查 @ 门控、查网络 ——
        而真正原因是**它压根不处理图片**。查错方向比不查更费时间。
        见设计方案 11.9.1「不支持的消息类型要回一句提示，不能沉默」。
        """
        got = parse_event(_payload(message_type="image"))
        assert isinstance(got, IncomingMessage), (
            f"非文字该带类型往下传，实际 {type(got).__name__}"
        )
        assert got.unsupported_type == "image", "类型要能说清，否则用户还得再问一次"
        assert got.text == "", "不可派发：空正文 + 类型，桥接见此就回提示而不是派发"

    def test_malformed_content_does_not_crash(self):
        raw = _payload()
        raw["event"]["message"]["content"] = "not json"
        assert isinstance(parse_event(raw), Ignored)

    @pytest.mark.parametrize("mutate", [
        lambda p: p.pop("header"),
        lambda p: p.pop("event"),
        lambda p: p["event"].pop("message"),
        lambda p: p["event"].pop("sender"),
        lambda p: p["event"]["sender"].pop("sender_id"),
        lambda p: p["event"]["message"].pop("chat_id"),
    ])
    def test_missing_pieces_are_ignored_not_crashed(self, mutate):
        raw = _payload()
        mutate(raw)
        assert isinstance(parse_event(raw), Ignored)

    def test_non_dict_payload(self):
        assert isinstance(parse_event(["nope"]), Ignored)


class TestMentionGate:
    """群里「@ 的是不是 **bot 自己**」—— 11.9.1 的核心判定。

    这组测试是为了锁住一个**曾经真实存在**的 bug：早先只看
    ``mentions`` 非空就当「被 @ 了」。于是群里 ``@张三 你看下这个``
    也会让 bot 插嘴 —— 它明明在跟人类说话。
    """

    def _mention(self, key, open_id, name=""):
        m = {"key": key, "id": {"open_id": open_id}}
        if name:
            m["name"] = name
        return m

    def test_at_bot_responds(self):
        msg = parse_event(_payload(
            text="@_user_1 /today",
            chat_type="group",
            mentions=[self._mention("@_user_1", "ou_bot", "助手")],
        ), bot_open_id="ou_bot")
        assert msg.mentioned is True
        assert msg.mention_note == ""
        assert msg.text == "/today", "@ 的是自己就删掉，不留「助手」在句子里"

    def test_at_someone_else_does_not_respond(self):
        """**本组测试的主角**：@ 别人不是 @ bot。"""
        msg = parse_event(_payload(
            text="@_user_1 你看下这个",
            chat_type="group",
            mentions=[self._mention("@_user_1", ALICE, "张三")],
        ), bot_open_id="ou_bot")
        assert msg.mentioned is False, "@ 的是别人，不该插嘴"
        assert "别人" in msg.mention_note, "要能说清原因，否则日志只有一句「没响应」"

    def test_at_other_keeps_name_in_text(self):
        """@ 别人时**替换成显示名，不能删掉**。

        删掉会把「@张三 你看下这个」变成「你看下这个」，语义主体被抹掉，
        意图识别拿到的是残句。替换后是「张三 你看下这个」。
        """
        msg = parse_event(_payload(
            text="@_user_1 你看下这个",
            chat_type="group",
            mentions=[self._mention("@_user_1", ALICE, "张三")],
        ), bot_open_id="ou_bot")
        assert msg.text == "张三 你看下这个"
        assert "@_user_1" not in msg.text, "占位符无论如何都得清掉"

    def test_at_other_without_name_falls_back_to_removal(self):
        """取不到显示名才退化。**绝不能把 ``@_user_1`` 留在正文里。**"""
        msg = parse_event(_payload(
            text="@_user_1 你看下这个",
            chat_type="group",
            mentions=[self._mention("@_user_1", ALICE)],
        ), bot_open_id="ou_bot")
        assert "@_user_1" not in msg.text
        assert msg.text == "你看下这个"

    def test_at_both_bot_and_human_responds_and_both_handled(self):
        """@ 了 bot 也 @ 了别人 → 响应，且两个占位符分别按身份处理。"""
        msg = parse_event(_payload(
            text="@_user_1 @_user_2 一起看下",
            chat_type="group",
            mentions=[
                self._mention("@_user_1", "ou_bot", "助手"),
                self._mention("@_user_2", ALICE, "张三"),
            ],
        ), bot_open_id="ou_bot")
        assert msg.mentioned is True
        assert msg.text == "张三 一起看下"

    def test_unknown_identity_fails_closed(self):
        """**核心安全属性**：身份未知时，群里一律不响应。

        判不准的时候朝「关」偏。代价是新装完还没探到身份时群里会安静 ——
        所以桥接启动时必须显著告警（见 ``bridge.main``）。
        """
        msg = parse_event(_payload(
            text="@_user_1 帮我记一下",
            chat_type="group",
            mentions=[self._mention("@_user_1", "ou_bot", "助手")],
        ), bot_open_id=None)
        assert msg.mentioned is False, "探不到身份就不能放行群消息"
        assert "失败关闭" in msg.mention_note
        assert "私聊" in msg.mention_note, "要说清代价只限群聊，否则用户以为全挂了"

    def test_private_chat_unaffected_by_unknown_identity(self):
        """私聊不需要 @，**身份未知也照常处理**。"""
        msg = parse_event(_payload(chat_type="p2p"), bot_open_id=None)
        assert msg.mentioned is True
        assert msg.mention_note == ""

    def test_group_without_any_mention_does_not_respond(self):
        msg = parse_event(_payload(chat_type="group"), bot_open_id="ou_bot")
        assert msg.mentioned is False

    def test_no_mention_says_so_instead_of_blaming_someone_else(self):
        """群里一个人都没 @ 时，原因**不能说成「@ 的是别人」**。

        踩过的坑：原先没有「没 @」这个分支，于是这条消息的 ``mention_note``
        是「群里 @ 的是别人不是 bot」—— 结论没错，但原因是**假的**。
        照着日志排查的人会去查「谁 @ 了 bot」，而实际上根本没人 @。
        这条测试原来只断言 ``mentioned is False``、不看文案，正是漏掉它的原因。
        """
        msg = parse_event(_payload(chat_type="group"), bot_open_id="ou_bot")
        assert "没有 @ bot" in msg.mention_note
        assert "别人" not in msg.mention_note, (
            f"没人 @ 的时候说「@ 的是别人」是编的：{msg.mention_note}"
        )

    def test_no_mention_with_unknown_identity_does_not_blame_mentions(self):
        """同样地，没 @ 时也不能说「带 @ 但身份未知」。"""
        msg = parse_event(_payload(chat_type="group"), bot_open_id=None)
        assert msg.mentioned is False
        assert "没有 @ bot" in msg.mention_note
        assert "带 @" not in msg.mention_note, (
            f"这条消息里没有任何 @：{msg.mention_note}"
        )

    def test_unknown_identity_still_fails_closed_when_actually_mentioned(self):
        """身份未知 + **确实 @ 了东西** → 仍然必须说清「失败关闭」。

        这是上一条的对照：把「没 @」和「@ 了但身份未知」分开之后，
        真正的安全警告不能被顺带削弱掉。
        """
        msg = parse_event(_payload(
            text="@_user_1 帮我记一下",
            chat_type="group",
            mentions=[self._mention("@_user_1", "ou_bot", "助手")],
        ), bot_open_id=None)
        assert msg.mentioned is False
        assert "失败关闭" in msg.mention_note
        assert "私聊" in msg.mention_note

    def test_mention_placeholder_never_leaks(self):
        """无论什么身份组合，正文里都不许残留 ``@_user_n``。"""
        cases = [
            (None, [self._mention("@_user_1", "ou_bot", "助手")]),
            ("ou_bot", [self._mention("@_user_1", "ou_bot", "助手")]),
            ("ou_bot", [self._mention("@_user_1", ALICE, "张三")]),
            ("ou_bot", [self._mention("@_user_1", ALICE)]),
        ]
        for bot, mentions in cases:
            msg = parse_event(_payload(
                text="@_user_1 帮我记一下", chat_type="group", mentions=mentions,
            ), bot_open_id=bot)
            assert "@_user_" not in msg.text, f"bot={bot} mentions={mentions}"


class TestFromSdkEvent:
    """用**鸭子类型**假对象测 —— 证明这层不需要装 SDK。"""

    def _fake(self, **kw):
        data = kw.get("data", NS(
            message=NS(
                chat_id="oc_chat1", chat_type="group", message_type="text",
                content=json.dumps({"text": "@_user_1 记一下"}, ensure_ascii=False),
                mentions=[NS(key="@_user_1")],
            ),
            sender=NS(sender_id=NS(open_id=ALICE)),
        ))
        return NS(
            header=NS(event_id="ev-9", event_type="im.message.receive_v1"),
            event=data,
        )

    def test_maps_sdk_object_to_message(self):
        msg = from_sdk_event(self._fake())
        assert isinstance(msg, IncomingMessage)
        assert msg.event_id == "ev-9"
        assert msg.sender_open_id == ALICE
        assert "@_user_1" not in msg.text
        assert msg.text == "记一下"

    def test_broken_object_does_not_crash(self):
        """SDK 给什么形状都不能把回调搞崩。"""
        assert isinstance(from_sdk_event(NS()), Ignored)
        assert isinstance(from_sdk_event(NS(header=None, event=None)), Ignored)

    def test_wrong_event_type_skipped(self):
        fake = self._fake()
        fake.header.event_type = "im.chat.updated_v1"
        assert from_sdk_event(fake) is None

    def _with_mentions(self, mentions):
        return self._fake(data=NS(
            message=NS(
                chat_id="oc_chat1", chat_type="group", message_type="text",
                content=json.dumps(
                    {"text": "@_user_1 @_user_2 一起看下"}, ensure_ascii=False
                ),
                mentions=mentions,
            ),
            sender=NS(sender_id=NS(open_id=ALICE)),
        ))

    def test_sdk_mention_keeps_open_id_and_name(self):
        """**回归测试**：适配器曾经只搬 ``key``，把身份丢了。

        丢的后果是 @ 门控在 SDK 路径上**静默失效** —— 手上只有
        「@_user_1」这个字符串，没有 ``id.open_id``，没法判断 @ 的是不是
        bot 自己，于是群消息要么全放行、要么全拦掉。而用真实 dict payload
        写的测试全绿，因为那条路径的 mentions 是完整的。**只改适配器不改
        这条测试，等于没测。**
        """
        fake = self._with_mentions([
            NS(key="@_user_1", id=NS(open_id="ou_bot"), name="助手"),
            NS(key="@_user_2", id=NS(open_id=ALICE), name="张三"),
        ])
        msg = from_sdk_event(fake, bot_open_id="ou_bot")
        assert isinstance(msg, IncomingMessage)
        assert msg.mentioned is True, "适配器丢了 open_id 就会判不出 @ 的是 bot 自己"
        assert msg.text == "张三 一起看下"

    def test_sdk_at_someone_else_does_not_respond(self):
        """同一条链路，@ 别人时必须不响应。"""
        fake = self._with_mentions([NS(key="@_user_1", id=NS(open_id=ALICE), name="张三")])
        msg = from_sdk_event(fake, bot_open_id="ou_bot")
        assert msg.mentioned is False
        assert msg.text == "张三 一起看下"

    def test_sdk_mention_with_dict_id_also_works(self):
        """``id`` 可能是 dict 也可能是类型化对象，两种都得认。"""
        fake = self._with_mentions([{"key": "@_user_1", "id": {"open_id": "ou_bot"}}])
        assert from_sdk_event(fake, bot_open_id="ou_bot").mentioned is True

    def test_sdk_mention_missing_id_is_not_treated_as_bot(self):
        """取不到 ``open_id`` 时不能当成「就是 bot 自己」—— 那等于放行一切。"""
        fake = self._with_mentions([NS(key="@_user_1", name="某人")])
        assert from_sdk_event(fake, bot_open_id="ou_bot").mentioned is False

    def test_sdk_mention_without_key_is_dropped(self):
        """没有 ``key`` 的 mention 直接丢掉，别让它进正文。"""
        fake = self._with_mentions([NS(key=None, id=NS(open_id="ou_bot"), name="助手")])
        assert from_sdk_event(fake, bot_open_id="ou_bot").mentioned is False


class TestDescribeSdkShape:
    """结构描述：**只列属性名，绝不列值**。

    为什么要有它：``from_sdk_event`` 是照文档写的，从没在真实飞书事件上
    验证过。万一真实结构对不上，机器人会安静地不理人，而日志里只有
    「缺 message」—— 没有任何线索能让人发现是形状问题。
    """

    def _fake(self):
        return NS(
            header=NS(event_id="ev-1", event_type="im.message.receive_v1"),
            event=NS(
                message=NS(chat_id="oc_c", chat_type="p2p", message_type="text",
                           content='{"text":"我信用卡号是 6222"}', mentions=[]),
                sender=NS(sender_id=NS(open_id="ou_alice")),
            ),
        )

    def test_lists_attribute_names(self):
        shape = describe_sdk_shape(self._fake())
        assert "header" in shape
        assert "message" in shape
        assert "sender_id" in shape

    def test_never_leaks_values(self):
        """**隐私红线**：事件里有消息正文和 open_id，结构报告只许有键名。"""
        shape = describe_sdk_shape(self._fake())
        assert "6222" not in shape, "消息正文绝不能进日志"
        assert "我信用卡号" not in shape
        assert "ou_alice" not in shape, "open_id 不该出现在结构报告里"
        assert "oc_c" not in shape

    def test_shows_unexpected_shape(self):
        """真实用武之地：结构对不上时，一次日志就能看出长什么样。"""
        weird = NS(
            header=NS(event_id="ev-1", event_type="im.message.receive_v1"),
            event=NS(payload=NS(chat=NS(id="oc_x"))),   # 全是猜错的字段名
        )
        shape = describe_sdk_shape(weird)
        assert "payload" in shape and "chat" in shape
        assert "message" not in shape, "结构不同就该看得出来"

    def test_handles_none_and_empty(self):
        assert describe_sdk_shape(None) != ""
        assert describe_sdk_shape(NS()) != ""

    def test_never_raises(self):
        """描述失败是次要问题，绝不能连带把回调搞崩。"""

        class Hostile:
            def __getattr__(self, name):
                raise RuntimeError("别碰我")

        assert describe_sdk_shape(Hostile()) != ""


# --------------------------------------------------------------------------- #
# 发消息
# --------------------------------------------------------------------------- #
class FakeTransport:
    """记录调用、可编排返回值。**一次真网络请求都不发。**"""

    def __init__(self, token="t-1", send_code=0):
        self.calls: list[tuple[str, dict, dict]] = []
        self.token = token
        self.send_code = send_code

    def __call__(self, url, payload, headers, timeout):
        self.calls.append((url, dict(payload), dict(headers)))
        if "tenant_access_token/internal" in url:
            return json.dumps({
                "code": 0, "msg": "ok",
                "tenant_access_token": self.token, "expire": 7200,
            })
        return json.dumps({"code": self.send_code, "msg": "sent", "data": {}})

    @property
    def sends(self):
        return [c for c in self.calls if "/im/v1/messages" in c[0]]


@pytest.fixture()
def sender_cfg():
    return load_config({
        ENV_APP_ID: "cli_x", ENV_APP_SECRET: "secret", ENV_ALLOWED: ALICE,
    })


class TestSender:
    def test_sends_text(self, sender_cfg):
        t = FakeTransport()
        FeishuSender(sender_cfg, transport=t).send_text("oc_1", "你好")
        assert len(t.sends) == 1
        body = t.sends[0][1]
        assert body["receive_id"] == "oc_1"
        assert body["msg_type"] == "text"
        # content 也是 JSON 字符串，和收消息时对称
        assert json.loads(body["content"])["text"] == "你好"

    def test_token_is_cached(self, sender_cfg):
        """每次现取 token 会白多一次请求，且飞书有频率限制。"""
        t = FakeTransport()
        s = FeishuSender(sender_cfg, transport=t)
        s.send_text("oc_1", "一")
        s.send_text("oc_1", "二")
        token_calls = [c for c in t.calls if "tenant_access_token" in c[0]]
        assert len(token_calls) == 1, "token 应该只取一次"

    def test_token_travels_in_headers_not_payload(self, sender_cfg):
        """payload 容易被日志打出来，headers 不容易被顺手打印。"""
        t = FakeTransport()
        FeishuSender(sender_cfg, transport=t).send_text("oc_1", "x")
        call = t.sends[0]
        assert "t-1" not in json.dumps(call[1]), "token 不能进 payload"
        assert call[2].get("Authorization") == "Bearer t-1"

    def test_token_fetch_does_not_leak_secret_into_error(self, sender_cfg):
        class Boom:
            def __call__(self, url, payload, headers, timeout):
                return json.dumps({"code": 99991663, "msg": "invalid app secret"})

        with pytest.raises(FeishuError):
            FeishuSender(sender_cfg, transport=Boom()).send_text("oc_1", "x")

    def test_business_code_nonzero_raises(self, sender_cfg):
        """**坑**：飞书 HTTP 200 不代表成功，业务码在 body 的 code 里。
        只看 HTTP 状态码会把「权限不足」当成成功 → 表现为「bot 不回话」。"""
        t = FakeTransport(send_code=230002)
        with pytest.raises(FeishuError, match="230002"):
            FeishuSender(sender_cfg, transport=t).send_text("oc_1", "x")

    def test_empty_text_sends_nothing(self, sender_cfg):
        t = FakeTransport()
        FeishuSender(sender_cfg, transport=t).send_text("oc_1", "   ")
        assert t.sends == []

    def test_long_text_truncated_with_notice(self, sender_cfg):
        t = FakeTransport()
        FeishuSender(sender_cfg, transport=t).send_text("oc_1", "字" * (MAX_TEXT + 50))
        sent = json.loads(t.sends[0][1]["content"])["text"]
        assert len(sent) < MAX_TEXT + 50
        assert "截断" in sent, "静默截断会让人以为看到的是全部"

    def test_non_json_response_raises(self, sender_cfg):
        t = lambda url, payload, headers, timeout: "<html>gateway</html>"
        with pytest.raises(FeishuError):
            FeishuSender(sender_cfg, transport=t).send_text("oc_1", "x")

    def test_send_to_user_uses_open_id_type(self, sender_cfg):
        """**回归**：按 open_id 私聊必须用 ``receive_id_type=open_id``。

        踩过的坑：提醒推送原本调 ``send_text(open_id, …)``，而它把
        ``receive_id_type`` 写死成 ``chat_id``。飞书的 ``ou_``（open_id）
        和 ``oc_``（chat_id）是**两个不同的东西**，这么发必被拒 ——
        定时提醒在真实环境是坏的，而测试全绿。
        """
        t = FakeTransport()
        FeishuSender(sender_cfg, transport=t).send_to_user("ou_alice", "提醒")
        url = t.sends[0][0]
        assert "receive_id_type=open_id" in url, f"实际 URL：{url}"
        assert t.sends[0][1]["receive_id"] == "ou_alice"

    def test_send_text_uses_chat_id_type(self, sender_cfg):
        t = FakeTransport()
        FeishuSender(sender_cfg, transport=t).send_text("oc_1", "回复")
        url = t.sends[0][0]
        assert "receive_id_type=chat_id" in url, f"实际 URL：{url}"
        assert "open_id" not in url

    def test_both_paths_encode_content_as_json_string(self, sender_cfg):
        """发消息时 content 也是 JSON 字符串，和收消息时对称。"""
        t = FakeTransport()
        s = FeishuSender(sender_cfg, transport=t)
        s.send_text("oc_1", "甲")
        s.send_to_user("ou_1", "乙")
        for call in t.sends:
            body = call[1]
            assert isinstance(body["content"], str)
            assert json.loads(body["content"])["text"] in ("甲", "乙")


# --------------------------------------------------------------------------- #
# 桥接
# --------------------------------------------------------------------------- #
class FakeSender:
    """记录收件方与内容。**不校验 id 类型** —— 那正是要靠真 sender 测的。"""

    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self.sent_to_user: list[tuple[str, str]] = []
        #: 白名单条目 → 用的 receive_id_type。用来验「按形状选类型」。
        self.entry_types: list[tuple[str, str]] = []

    def send_text(self, chat_id, text):
        self.sent.append((chat_id, text))

    def send_to_user(self, open_id, text):
        self.sent_to_user.append((open_id, text))

    def send_to_allowlist_entry(self, entry, text):
        """镜像真 sender 的分支：ou_ → open_id，oc_ → 抛错，其余 → user_id。"""
        if entry.startswith("ou_"):
            self.entry_types.append((entry, "open_id"))
        elif entry.startswith("oc_"):
            raise ValueError(f"{entry} 看起来是会话 id（oc_ 开头），不是人的 id。")
        else:
            self.entry_types.append((entry, "user_id"))
        self.sent_to_user.append((entry, text))


@pytest.fixture()
def bridge(tmp_path, clock):
    app = build_app(tmp_path / "a.db", clock=clock)
    cfg = load_config({
        ENV_APP_ID: "cli_x", ENV_APP_SECRET: "secret",
        ENV_ALLOWED: f"{ALICE},{BOB}",
    })
    channel = ChannelService(app, allowed_senders=cfg.allowed_users)
    sender = FakeSender()
    # 传真实 bot 身份：群聊门控靠它判定。不传的话群里会**一律不响应**
    # （失败关闭），于是所有群聊用例都测不到「该响应」那条路径。
    b = FeishuBridge(channel, sender, cfg, bot_open_id=BOT)
    yield b
    b.stop()
    app.close()


@pytest.fixture()
def bridge_blind(tmp_path, clock):
    """**探不到身份**的桥接：群聊门控失败关闭，私聊照常。"""
    app = build_app(tmp_path / "b.db", clock=clock)
    cfg = load_config({
        ENV_APP_ID: "cli_x", ENV_APP_SECRET: "secret",
        ENV_ALLOWED: f"{ALICE},{BOB}",
    })
    channel = ChannelService(app, allowed_senders=cfg.allowed_users)
    sender = FakeSender()
    b = FeishuBridge(channel, sender, cfg, bot_open_id=None)
    yield b
    b.stop()
    app.close()


def _free_port() -> int:
    """借操作系统一个**当下空闲**的端口。

    踩过的坑：子进程测试原来不设 ``FEISHU_LOCK_PORT``，于是去抢默认 8771 ——
    而那正是真桥接在用的端口。于是**只要用户真的把桥接跑着**（也就是这个工具
    的正常使用状态），整条测试就会因为「端口 8771 已被占用」而失败。
    测试不能依赖被测环境恰好没在跑。绑 0 让内核分配，闭 socket 即刻释放。
    """
    import socket

    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _sdk_event(text="/today", chat_id="oc_1", sender=ALICE, chat_type="p2p",
               event_id="ev-1"):
    return NS(
        header=NS(event_id=event_id, event_type="im.message.receive_v1"),
        event=NS(
            message=NS(chat_id=chat_id, chat_type=chat_type, message_type="text",
                       content=json.dumps({"text": text}), mentions=[]),
            sender=NS(sender_id=NS(open_id=sender)),
        ),
    )


class TestBridgeRouting:
    def test_queues_instead_of_processing_inline(self, bridge):
        """**3 秒应答**：回调只入队。执行慢也不占回调线程。"""
        bridge.on_message(_sdk_event())
        assert bridge._queue.qsize() == 1
        assert bridge.sender.sent == [], "还没到工作线程，不该已发"

    def test_group_without_mention_is_dropped(self, bridge):
        """群里不 @ 就插嘴会很烦。"""
        bridge.on_message(_sdk_event(chat_type="group"))
        assert bridge._queue.qsize() == 0

    def test_group_with_mention_is_queued(self, bridge):
        """@ 到 **bot 自己** 才入队。

        mention 必须带 ``id.open_id``：门控比的是身份，光有个 ``key``
        占位符判不出来。这正是 SDK 适配器曾经丢掉的那部分。
        """
        ev = _sdk_event(chat_type="group")
        ev.event.message.mentions = [NS(key="@_bot", id=NS(open_id=BOT), name="助手")]
        ev.event.message.content = json.dumps({"text": "@_bot /today"})
        bridge.on_message(ev)
        assert bridge._queue.qsize() == 1

    def test_group_at_human_is_dropped(self, bridge):
        """**回归**：群里 @ 别人不该让 bot 插嘴。

        早期实现只看 ``mentions`` 非空，于是 ``@张三 你看下这个`` 也会
        入队并回一句 —— 它明明在跟人类说话，而且 bot 拿到的是一句被剥掉
        占位符的残句，意图识别必然跑偏。
        """
        ev = _sdk_event(chat_type="group")
        ev.event.message.mentions = [NS(key="@_u1", id=NS(open_id=ALICE), name="张三")]
        ev.event.message.content = json.dumps({"text": "@_u1 你看下这个"})
        bridge.on_message(ev)
        assert bridge._queue.qsize() == 0, "@ 的是别人，不该入队"

    def test_group_at_human_log_says_why(self, bridge, caplog):
        """被丢掉时日志要说清原因，否则用户只看到「群里没反应」。"""
        import logging

        ev = _sdk_event(chat_type="group")
        ev.event.message.mentions = [NS(key="@_u1", id=NS(open_id=ALICE), name="张三")]
        ev.event.message.content = json.dumps({"text": "@_u1 你看下这个"})
        with caplog.at_level(logging.INFO, logger="freeagent.feishu"):
            bridge.on_message(ev)
        assert "别人" in caplog.text, f"日志该说清原因，实际：{caplog.text!r}"

    def test_blind_bridge_drops_group_but_keeps_private(self, bridge_blind):
        """**失败关闭**：身份探不到时群里一律不响应，私聊不受影响。"""
        group = _sdk_event(chat_type="group")
        group.event.message.mentions = [NS(key="@_bot", id=NS(open_id=BOT), name="助手")]
        group.event.message.content = json.dumps({"text": "@_bot /today"})
        bridge_blind.on_message(group)
        assert bridge_blind._queue.qsize() == 0, "身份未知不能放行群消息"

        bridge_blind.on_message(_sdk_event(chat_type="p2p", event_id="ev-p2p"))
        assert bridge_blind._queue.qsize() == 1, "私聊不需要 @，不该被身份问题连累"

    def test_blind_bridge_logs_fail_closed(self, bridge_blind, caplog):
        """启动后群里不响，用户得能从日志看出「这是配置问题不是权限问题」。"""
        import logging

        ev = _sdk_event(chat_type="group")
        ev.event.message.mentions = [NS(key="@_bot", id=NS(open_id=BOT), name="助手")]
        ev.event.message.content = json.dumps({"text": "@_bot /today"})
        with caplog.at_level(logging.INFO, logger="freeagent.feishu"):
            bridge_blind.on_message(ev)
        assert "失败关闭" in caplog.text
        assert "doctor" in caplog.text, "要告诉用户去哪查，否则无从下手"

    def test_other_event_type_dropped(self, bridge):
        ev = _sdk_event()
        ev.header.event_type = "im.chat.updated_v1"
        bridge.on_message(ev)
        assert bridge._queue.qsize() == 0

    def test_broken_event_does_not_raise(self, bridge):
        bridge.on_message(NS())          # 垃圾事件不许搞崩回调
        assert bridge._queue.qsize() == 0

    def test_stranger_is_queued_but_rejected_downstream(self, bridge):
        """入队不代表放行 —— 白名单在路由层拦。

        注意用的是 CAROL（不在白名单里）。用 BOB 的话这条断言是假的：
        桥接夹具的白名单是 {ALICE, BOB}。
        """
        bridge.on_message(_sdk_event(sender=CAROL))
        assert bridge._queue.qsize() == 1
        bridge._process(bridge._queue.get_nowait())
        assert bridge.sender.sent[0][1] == "没有权限。"

    def test_denied_sender_open_id_is_logged(self, bridge, caplog):
        """**回归**：白名单拒人时必须打出 open_id。

        白名单填错时的表现是「bot 一句话都不回」，而用户拿不到任何
        可操作信息 —— 不知道自己的 open_id 就没法填白名单。
        手册里「发一条消息、从日志里取 open_id」这条排查路径全靠这一行；
        没有它，那段文档是假的。
        """
        import logging

        bridge.on_message(_sdk_event(sender=CAROL, event_id="ev-deny"))
        with caplog.at_level(logging.INFO, logger="freeagent.feishu"):
            bridge._process(bridge._queue.get_nowait())
        assert CAROL in caplog.text, f"日志里必须有 open_id，实际：{caplog.text!r}"

    def test_denied_log_does_not_leak_message_body(self, bridge, caplog):
        """只打身份，不打正文 —— 正文可能有隐私内容。"""
        import logging

        secret = "别告诉别人我信用卡号"
        bridge.on_message(_sdk_event(text=secret, sender=CAROL, event_id="ev-x"))
        with caplog.at_level(logging.INFO, logger="freeagent.feishu"):
            bridge._process(bridge._queue.get_nowait())
        assert secret not in caplog.text, "被拒的消息正文不该进日志"

    def test_successful_reply_is_logged(self, bridge, caplog):
        """**回归**：成功处理的消息也必须留痕。

        踩过的坑（真在飞书里撞出来的）：原先成功时**一行都不打**。于是用户报
        「发了没反应」时，日志是空的 —— 和「消息压根没到」长得一模一样，
        我为此白查了一轮（甚至去动 dedup 计数器）。

        只记元信息（会话、发送者、字数），**不记正文**。
        """
        import logging

        bridge.on_message(_sdk_event(text="/today", sender=ALICE, event_id="ev-ok"))
        with caplog.at_level(logging.INFO, logger="freeagent.feishu"):
            bridge._process(bridge._queue.get_nowait())
        assert bridge.sender.sent, "先确认真的回了，否则这条断言是空的"
        assert "已回复" in caplog.text, (
            f"成功处理必须留痕，实际日志：{caplog.text!r}"
        )

    def test_success_log_does_not_leak_body(self, bridge, caplog):
        """留痕也不许带正文 —— 回复里会含用户自己的话（任务标题等）。"""
        import logging

        secret = "我的体检报告在抽屉里"
        bridge.on_message(_sdk_event(text=secret, sender=ALICE, event_id="ev-ok2"))
        with caplog.at_level(logging.INFO, logger="freeagent.feishu"):
            bridge._process(bridge._queue.get_nowait())
        assert secret not in caplog.text, "正文不该进日志"

    def test_empty_reply_is_logged_loudly(self, bridge, caplog):
        """**回归**：没被拒却算出空回复 = 异常，必须 warning。

        这正是 ``ChannelService._run`` 少绑一次 ``repl.out`` 时的症状 ——
        **同一会话第 2 条起全部沉默**。原先空回复一律静默 ``return``，
        和「重复事件」混在一起；而沉默又不留日志，两个缺陷叠加，
        让它**完全无法被诊断**。
        """
        import logging

        from freeagent.services.channel import ChannelReply

        bridge.channel.handle = lambda *a, **k: ChannelReply(text="", denied=False)
        bridge.on_message(_sdk_event(text="/today", sender=ALICE, event_id="ev-empty"))
        with caplog.at_level(logging.WARNING, logger="freeagent.feishu"):
            bridge._process(bridge._queue.get_nowait())
        assert bridge.sender.sent == [], "空回复不该给用户发任何东西"
        assert "空回复" in caplog.text, (
            f"异常的空回复必须出声，实际日志：{caplog.text!r}"
        )

    def test_duplicate_event_stays_quiet(self, bridge, caplog):
        """对照：重复事件是**预期**，仍然静默。

        和上面那条必须分开 —— 把「预期」和「异常」混在一起，就是当初
        这个 bug 查不出来的原因。
        """
        import logging

        bridge.on_message(_sdk_event(text="/today", sender=ALICE, event_id="ev-dup"))
        bridge._process(bridge._queue.get_nowait())
        first = len(bridge.sender.sent)
        bridge.on_message(_sdk_event(text="/today", sender=ALICE, event_id="ev-dup"))
        with caplog.at_level(logging.WARNING, logger="freeagent.feishu"):
            bridge._process(bridge._queue.get_nowait())
        assert len(bridge.sender.sent) == first, "重投不该重复回"
        assert "空回复" not in caplog.text, "重复事件是预期，不该报成异常"

    # -- 文字消息的 event_id 为空（真实事件里出现过）------------------------ #
    # 下面三条与 TestUnsupportedMessageType 里那三条**成对**：那边修过一次
    # 同样的坑（当时只改了非文字那条路径），文字这条漏了，于是同一个 bug 要
    # 在同一个文件里踩两次。两条路径现在都得守住。
    @staticmethod
    def _text_no_event_id(message_id, text="/today"):
        """文字事件**没有** ``event_id``，只有 ``message_id``。

        实测撞见过：一条私聊消息被正常处理、也回了话，可去重表条目数
        **一动不动** —— 那次事件的 event_id 就是空的。
        """
        ev = _sdk_event(text=text, chat_id="oc_1", event_id="tmp")
        ev.event.message.message_id = message_id
        ev.header.event_id = ""              # 关键：置空
        return ev

    def test_text_without_event_id_is_still_deduped(self, bridge):
        """**回归**：文字消息 ``event_id`` 为空也要靠 ``message_id`` 记上去。

        踩过的坑：``_process`` 传的是 ``event_id=msg.event_id or None``，
        于是空 event_id 会让去重整段被跳过 —— 而飞书重连时**会重投**事件，
        同一条消息就会**再建一次事务**。
        """
        before = len(bridge.channel.dedup)
        bridge.on_message(self._text_no_event_id("om_txt_1"))
        bridge._process(bridge._queue.get_nowait())
        assert len(bridge.sender.sent) == 1, "首次该回一句"
        assert len(bridge.channel.dedup) == before + 1, (
            f"没有 event_id 也必须被记住（靠 message_id），"
            f"去重表 {before} → {len(bridge.channel.dedup)}"
        )

    def test_redelivered_text_without_event_id_runs_once(self, bridge, app):
        """**回归**：同样没有 ``event_id``，重投也只处理一次。

        断言落在**「有没有再问一遍」**和**库里建了几条**两处，而不是
        「回了几句」—— 只看回复数，重投若被当成新消息，在「本来就需要
        追问」的场景下回复数是一样的，看不出来。而**追问被问第二遍**
        正是用户能直接察觉的异常。
        """
        bridge.on_message(self._text_no_event_id("om_txt_2", text="记一下买牛奶"))
        bridge._process(bridge._queue.get_nowait())
        first = len(bridge.sender.sent)
        assert first == 1, "首次该回一句"
        made = app.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]

        bridge.on_message(self._text_no_event_id("om_txt_2", text="记一下买牛奶"))
        bridge._process(bridge._queue.get_nowait())
        assert len(bridge.sender.sent) == first, (
            f"重投不该重复回，实际 {len(bridge.sender.sent)} 次"
        )
        again = app.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        assert again == made, f"重投不该再建事务（{made} → {again}）"

    def test_text_with_no_key_at_all_warns(self, bridge, caplog):
        """**回归**：两个键都缺时**必须告警**，不能静默跳过去重。

        静默是这次排查绕路的根因：日志里什么都不打，只能靠猜「是不是
        event_id 为空」。
        """
        import logging

        ev = self._text_no_event_id("", text="/today")
        with caplog.at_level(logging.WARNING, logger="freeagent.feishu"):
            bridge.on_message(ev)
            bridge._process(bridge._queue.get_nowait())
        assert "无法去重" in caplog.text, f"两个键都空时必须告警：{caplog.text!r}"


class TestUnsupportedMessageType:
    """不支持的消息类型（图片/文件/富文本）：**回一句提示，且绝不建事务**。

    踩过的坑：用户文档写着「图片、文件会明确回「暂只处理文字」而不是沉默」，
    而实现是 ``parse_event`` 返回 ``Ignored`` → ``bridge.on_message`` 记一行
    日志就 ``return``，**一个字节都不发出去**。文档撒了谎，且零测试守着。

    为什么非得钉死：用户发图片没反应时，会以为「bot 收到了但不想理」，
    于是去查白名单、查 @ 门控、查网络 —— 而真正原因是**它压根不处理图片**。
    查错方向比不查更费时间。

    但**回提示是有代价的**：它是 bot 主动发出的消息，于是必须守住两道闸 ——
    白名单（回一句就等于向陌生人确认「这里有个 bot」）与群聊 @ 门控
    （群里随手发张图就抢着说话，等于把「不抢答」又破一次）。
    这两条比「回一句」本身更重要，所以单独占测试。
    """

    def _image_event(self, chat_id="oc_1", sender=ALICE, event_id="ev-img",
                     chat_type="p2p"):
        ev = _sdk_event(chat_id=chat_id, sender=sender, event_id=event_id,
                        chat_type=chat_type)
        ev.event.message.message_type = "image"
        ev.event.message.content = json.dumps({"image_key": "img_v2_x"})
        return ev

    def _drain(self, bridge):
        """把队列里的东西走完 ``_process``，返回处理条数。"""
        n = 0
        while True:
            try:
                msg = bridge._queue.get_nowait()
            except queue.Empty:
                return n
            bridge._process(msg)
            n += 1

    # -- 该回的 ------------------------------------------------------------- #
    def test_private_image_gets_a_hint(self, bridge):
        """私聊里发图片（白名单内）→ 回一句提示，带上实际类型。"""
        bridge.on_message(self._image_event())
        self._drain(bridge)
        assert bridge.sender.sent, "私聊发图片该回一句，别让用户对着沉默猜"
        text = bridge.sender.sent[0][1]
        assert "image" in text, f"提示语要带上实际类型，用户才知道什么没被处理：{text!r}"
        assert "文字" in text, f"要说明自己只处理文字：{text!r}"

    def test_hint_does_not_create_a_task(self, bridge):
        """**关键**：回提示只是告知，**不许建事务**。"""
        app_ = bridge.channel.app
        bridge.on_message(self._image_event(event_id="ev-hint-no-task"))
        self._drain(bridge)
        assert app_.tasks.list_open() == [], (
            f"提示不该建事务，实际建了：{app_.tasks.list_open()!r}"
        )

    def test_group_image_with_mention_gets_a_hint(self, bridge):
        """群里 @ 到 bot 再发图片 → 该回。"""
        ev = self._image_event(chat_id="oc_g", chat_type="group", event_id="ev-g1")
        ev.event.message.mentions = [NS(key="@_bot", id=NS(open_id=BOT), name="助手")]
        bridge.on_message(ev)
        self._drain(bridge)
        assert bridge.sender.sent, "群里 @ 到 bot 了，该回提示"

    # -- 不该回的（这两条比「回一句」本身更重要）------------------------- #
    def test_group_image_without_mention_gets_nothing(self, bridge):
        """群里**没 @ bot** 就发图片 → 一声不吭。

        这是最容易写坏的一条：提示若在解析阶段就发出去，群里任何人随手
        发张图，bot 就抢着说「我只处理文字」—— 那比沉默更糟。
        """
        bridge.on_message(self._image_event(chat_id="oc_g", chat_type="group",
                                            event_id="ev-g2"))
        self._drain(bridge)
        assert bridge.sender.sent == [], (
            f"群里没 @ bot 不该插话，实际发了：{bridge.sender.sent!r}"
        )

    def test_non_whitelisted_sender_gets_no_hint(self, bridge):
        """**白名单外连提示都不回** —— 回一句就泄露了「这里有个 bot」。

        CAROL 不在白名单里（见 bridge fixture：只授权了 ALICE/BOB）。
        """
        bridge.on_message(self._image_event(sender=CAROL, event_id="ev-nope"))
        self._drain(bridge)
        assert bridge.sender.sent == [], (
            f"白名单外不该收到任何回复（提示也算），实际：{bridge.sender.sent!r}"
        )

    def test_group_at_human_image_gets_nothing(self, bridge):
        """群里 @ 的是别人 + 发图片 → 不该回（那是它在跟人类说话）。"""
        ev = self._image_event(chat_id="oc_g", chat_type="group", event_id="ev-g3")
        ev.event.message.mentions = [NS(key="@_u1", id=NS(open_id=ALICE), name="张三")]
        bridge.on_message(ev)
        self._drain(bridge)
        assert bridge.sender.sent == [], (
            f"@ 的是别人，不该插话，实际：{bridge.sender.sent!r}"
        )

    # -- 富文本与留痕 ------------------------------------------------------- #
    def test_rich_text_post_gets_hint_not_dispatch(self, bridge):
        """富文本（post）同样回提示，不进派发。"""
        ev = _sdk_event(chat_id="oc_1", event_id="ev-post")
        ev.event.message.message_type = "post"
        ev.event.message.content = json.dumps({"zh_cn": {"title": "", "content": []}})
        bridge.on_message(ev)
        self._drain(bridge)
        assert bridge.sender.sent, "富文本也该回一句"
        assert "post" in bridge.sender.sent[0][1], (
            f"提示语要带上实际类型：{bridge.sender.sent[0][1]!r}"
        )

    def test_image_logs_the_reason(self, bridge, caplog):
        """回提示也要留痕，否则排查时无从判断「它到底处理了没有」。"""
        import logging

        with caplog.at_level(logging.INFO, logger="freeagent.feishu"):
            bridge.on_message(self._image_event(event_id="ev-log"))
            self._drain(bridge)
        assert "image" in caplog.text, f"日志该说清收到的类型：{caplog.text!r}"
        assert "不建事务" in caplog.text, f"日志该说清没有建事务：{caplog.text!r}"

    # -- 去重（实测才发现的漏洞）--------------------------------------------- #
    def test_image_is_recorded_in_dedup(self, bridge):
        """非文字消息**也必须**写去重表。

        踩过的坑：去重发生在 ``channel.handle`` 里面，而回提示这条路径
        刻意不走 ``handle``（它只处理可派发的正文）。于是图片**从不写表** ——
        「跨重启去重」这个承诺对图片是假的，而离线测试全绿，因为
        离线测试只断言「回了一句」，没断言「记住了」。
        """
        before = len(bridge.channel.dedup)
        bridge.on_message(self._image_event(event_id="ev-dedup"))
        self._drain(bridge)
        assert len(bridge.channel.dedup) == before + 1, (
            f"处理过的图片必须被记住，去重表 {before} → {len(bridge.channel.dedup)}"
        )

    def test_redelivered_image_is_not_answered_twice(self, bridge):
        """**回归**：飞书重连重投同一条图片 → 只回一次。

        这是上面那个漏洞的直接后果：文字消息不会重复，图片会。
        症状极隐蔽 —— 用户只看到同一张图被回了两次「只处理文字」。
        """
        bridge.on_message(self._image_event(event_id="ev-dup"))
        self._drain(bridge)
        first = len(bridge.sender.sent)
        assert first == 1, f"首次应回一次，实际 {first}"

        bridge.on_message(self._image_event(event_id="ev-dup"))
        self._drain(bridge)
        assert len(bridge.sender.sent) == first, (
            f"重投不该重复回提示，实际回了 {len(bridge.sender.sent)} 次"
        )

    # -- event_id 为空的场景（真实事件里出现过）---------------------------- #
    @staticmethod
    def _image_no_event_id(message_id):
        """图片事件**没有** ``event_id``，只有 ``message_id``。"""
        ev = _sdk_event(chat_id="oc_1", event_id="tmp")
        ev.event.message.message_type = "image"
        ev.event.message.content = json.dumps({"image_key": "img_v2_x"})
        ev.event.message.message_id = message_id
        ev.header.event_id = ""              # 关键：置空
        return ev

    def test_image_without_event_id_is_still_deduped(self, bridge):
        """**回归**：`event_id` 为空也要靠 `message_id` 记上去。

        踩过的坑：原写法 ``if msg.event_id and dedup.is_duplicate(...)``，
        ``event_id` 为空时整段被跳过 —— 回提示但不写表，同一张图重投会
        **再回一次**。离线 fixture 的 event_id 一直都有，所以测试全绿。
        """
        before = len(bridge.channel.dedup)
        bridge.on_message(self._image_no_event_id("om_img_1"))
        self._drain(bridge)
        assert len(bridge.sender.sent) == 1, "首次该回一句"
        assert len(bridge.channel.dedup) == before + 1, (
            f"没有 event_id 也必须被记住（靠 message_id），"
            f"去重表 {before} → {len(bridge.channel.dedup)}"
        )

    def test_redelivery_without_event_id_is_not_answered_twice(self, bridge):
        """**回归**：同样没有 `event_id`，重投也只回一次。"""
        bridge.on_message(self._image_no_event_id("om_img_2"))
        self._drain(bridge)
        first = len(bridge.sender.sent)
        assert first == 1

        bridge.on_message(self._image_no_event_id("om_img_2"))
        self._drain(bridge)
        assert len(bridge.sender.sent) == first, (
            f"重投不该重复回提示，实际回了 {len(bridge.sender.sent)} 次"
        )

    def test_no_key_at_all_warns_instead_of_silently_skipping(self, bridge, caplog):
        """**回归**：两个键都缺时**必须告警**，不能静默跳过去重。

        静默是这次排查绕了两轮的根因：日志里什么都不打，只能靠猜
        「是不是 event_id 为空」。分不清原因，用户就只能猜
        —— 与 11.9.1「两种『群里不响应』必须能分开看」同源。
        """
        import logging

        ev = self._image_no_event_id("om_img_3")
        ev.event.message.message_id = ""      # 两个键都空
        with caplog.at_level(logging.WARNING, logger="freeagent.feishu"):
            bridge.on_message(ev)
            self._drain(bridge)
        assert "无法去重" in caplog.text, (
            f"无键可用必须明确告警，否则排查只能靠猜：{caplog.text!r}"
        )

    def test_rich_text_post_is_ignored_too(self, bridge):
        """富文本（post）同样不处理 —— 门控回退对它不可达，因为它先被拒。"""
        ev = _sdk_event(chat_type="group", event_id="ev-post")
        ev.event.message.message_type = "post"
        ev.event.message.content = json.dumps({"zh_cn": {"title": "", "content": []}})
        bridge.on_message(ev)
        assert bridge._queue.qsize() == 0


class TestBridgeWorker:
    def test_worker_answers(self, bridge):
        bridge.start_worker()
        bridge.on_message(_sdk_event(text="/roles"))
        deadline = time.time() + 5
        while not bridge.sender.sent and time.time() < deadline:
            time.sleep(0.02)
        assert bridge.sender.sent, "工作线程应把回复发出去"
        assert bridge.sender.sent[0][0] == "oc_1"

    def test_duplicate_event_is_not_answered_twice(self, bridge):
        bridge.start_worker()
        ev = _sdk_event(text="/roles", event_id="same")
        bridge.on_message(ev)
        time.sleep(0.4)
        bridge.on_message(ev)
        time.sleep(0.4)
        assert len(bridge.sender.sent) == 1, "飞书重投不该产生两条回复"

    def test_one_bad_message_does_not_kill_worker(self, bridge):
        """一条消息炸了，通道必须还活着。"""
        # 先抓住**原方法**再替换。踩过的坑：一开始在替身里写
        # ``bridge.channel.handle(...)``，而那已经是替身自己 ——
        # 无限递归，RecursionError 被宽 except 吞掉，测试表现为
        # 「后面那条也没被处理」，完全指不到真正的原因。
        original = bridge.channel.handle
        calls: list[str] = []

        def flaky(chat_id, sender_id, text, *, event_id=None):
            calls.append(text)
            if text == "/boom":
                raise RuntimeError("炸了")
            return original(chat_id, sender_id, text, event_id=event_id)

        bridge.channel.handle = flaky        # type: ignore[method-assign]
        bridge.start_worker()
        bridge.on_message(_sdk_event(text="/boom", event_id="e1"))
        time.sleep(0.3)
        bridge.on_message(_sdk_event(text="/roles", event_id="e2"))
        deadline = time.time() + 5
        while not bridge.sender.sent and time.time() < deadline:
            time.sleep(0.02)
        assert bridge.sender.sent, "第一条炸了之后，后面的仍要能处理"
        assert calls == ["/boom", "/roles"], "两条都该被尝试过"

    def test_queue_full_drops_oldest(self, bridge):
        """内存吃光会影响机器上别的事 —— 宁可丢消息。"""
        from freeagent.feishu.bridge import _QUEUE_MAX

        for i in range(_QUEUE_MAX + 5):
            bridge.on_message(_sdk_event(text=f"/roles {i}", event_id=f"e{i}"))
        assert bridge._queue.qsize() <= _QUEUE_MAX


class TestBridgeReminders:
    def test_pushes_to_each_allowed_user(self, bridge):
        bridge._notify_operators("该交周报了")
        targets = {uid for uid, _ in bridge.sender.sent_to_user}
        assert targets == {ALICE, BOB}, "每个被授权的人都该收到"

    def test_pushes_nothing_when_no_due(self, bridge):
        bridge._notify_operators("")
        assert bridge.sender.sent_to_user == []
        assert bridge.sender.sent == [], "没到期就不该发任何消息"

    def test_reminder_goes_to_user_not_chat(self, bridge):
        """**回归**：提醒必须走 ``send_to_user``（open_id），不能走
        ``send_text``（chat_id）。

        踩过的坑：白名单里存的是 ``open_id``（``ou_``），而提醒推送原本
        直接调 ``send_text`` —— 后者把 ``receive_id_type`` 写死成
        ``chat_id``。飞书的 open_id 和私聊会话 chat_id **是两个不同的东西**，
        这么发必然被接口拒掉：**定时提醒在真实环境是坏的**，
        而离线测试全绿（FakeSender 只记录传了什么，不校验 id 类型）。
        """
        bridge._notify_operators("该交周报了")
        assert bridge.sender.sent == [], "提醒不该按会话 id 发"
        assert bridge.sender.sent_to_user, "提醒必须按 open_id 发"

    # -- 送达与否必须报告出来（设计文档 9.2）--------------------------- #
    def test_reports_delivered_when_someone_got_it(self, bridge):
        assert bridge._notify_operators("该交周报了") is True

    def test_reports_not_delivered_when_all_sends_fail(self, bridge):
        """白名单全是会话 id 时每个发送都抛错 —— 必须报告「没送到」。

        以前这里把每个用户的异常各自吞掉且不报结果，调用方只能当成功。
        于是飞书推送失败时，那条提醒照样被落去重令牌、永久标记成已送达，
        而用户从没收到过。
        """
        import dataclasses

        # config 是 frozen dataclass，只能整体换掉
        bridge.config = dataclasses.replace(bridge.config, allowed_users=["oc_a", "oc_b"])
        assert bridge._notify_operators("该交周报了") is False

    def test_empty_text_is_never_delivered(self, bridge):
        assert bridge._notify_operators("") is False
        assert bridge._notify_operators(None) is False
        assert bridge._notify_operators("   ") is False

    # 「推送失败后提醒仍在待发状态」这条端到端保证放在 test_channel.py ——
    # 那里有 _task 建任务的 helper，而本文件全程走消息接口、不直接建任务。

    def test_zero_poll_does_not_start_thread(self, bridge):
        """poll=0 就是明确关掉提醒，不该起线程。"""
        import dataclasses

        # 踩过的坑：一开始写 ``type(cfg)(**{**cfg.__dict__, …})`` ——
        # 可 ``FeishuConfig`` 是 ``slots=True`` 的 dataclass，**没有 __dict__**，
        # 于是改配置的意图变成了一段莫名其妙的 AttributeError。
        bridge.config = dataclasses.replace(bridge.config, reminder_poll=0.0)
        bridge.start_reminders()
        assert bridge._reminder is None, "poll=0 就不该起线程"

    def test_positive_poll_starts_thread(self, bridge):
        import dataclasses

        bridge.config = dataclasses.replace(bridge.config, reminder_poll=0.05)
        bridge.start_reminders()
        assert bridge._reminder is not None
        assert bridge._reminder.daemon, "提醒线程必须是守护线程，Ctrl+C 才能退出"


class TestEntryPoint:
    """``python -m freeagent.feishu.bridge`` 必须**真的执行** main()。

    **回归**：漏了 ``if __name__ == "__main__"`` 守卫时，这条命令会
    导入模块、什么都不做、**退出码 0 且没有任何输出** —— 看起来「启动成功」，
    其实一条命令都没执行。控制台脚本 ``freeagent-feishu`` 走的是另一条路，
    所以它是对的、测试也全绿，只有 ``-m`` 这条是哑的。联调时白等一轮才发现。

    这里用退出码判断，而且**不需要联网**：不配 FEISHU_* 时
    ``check_ready`` 必然失败并返回 2。
    """

    def _run(self, env_extra=None):
        import os
        import subprocess

        env = {
            k: v for k, v in os.environ.items() if not k.startswith("FEISHU_")
        }
        env["PYTHONPATH"] = "src"
        env["PYTHONIOENCODING"] = "utf-8"
        # 端口必须自己挑：默认值 8771 可能正被真桥接占着，而「桥接跑着」
        # 是这个工具的正常使用状态。测试不能依赖被测环境恰好是空的。
        env[ENV_LOCK_PORT] = str(_free_port())
        env.update(env_extra or {})
        return subprocess.run(
            [sys.executable, "-m", "freeagent.feishu.bridge"],
            env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120,
        )

    def test_module_run_actually_invokes_main(self):
        p = self._run()
        assert p.returncode == 2, (
            "缺配置时应退出 2（配置错误）。退出 0 说明 main() 根本没被调用 —— "
            "检查 __main__ 守卫。实际输出：%r" % (p.stdout + p.stderr)
        )

    def test_missing_config_explains_itself(self):
        """不能只给个退出码，用户得知道缺什么。"""
        p = self._run({ENV_APP_ID: "cli_x", ENV_APP_SECRET: "s"})
        assert ENV_ALLOWED in (p.stdout + p.stderr)

    def test_missing_sdk_gives_install_hint(self):
        """缺 lark-oapi 时要说清怎么装，而不是抛一段栈。"""
        import os
        import subprocess

        # 先用「有配置但假装缺 SDK」的方式验证提示语存在
        src = (
            "import sys;"
            "sys.modules['lark_oapi'] = None;"
            "from freeagent.feishu import bridge;"
            "sys.argv = ['x'];"
            "raise SystemExit(bridge.main([]))"
        )
        env = {
            k: v for k, v in os.environ.items() if not k.startswith("FEISHU_")
        }
        env.update({
            "PYTHONPATH": "src", "PYTHONIOENCODING": "utf-8",
            ENV_APP_ID: "cli_x", ENV_APP_SECRET: "s", ENV_ALLOWED: "ou_x",
            # 同 _run：默认 8771 可能被真桥接占着，会在到达 SDK 检查前
            # 就被端口锁拦掉，于是这条测试变成「测端口占用」而不是「测提示语」。
            ENV_LOCK_PORT: str(_free_port()),
        })
        p = subprocess.run(
            [sys.executable, "-c", src], env=env, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=120,
        )
        assert "[feishu]" in (p.stdout + p.stderr) or p.returncode == 3, (
            "缺依赖时应提示 pip install \".[feishu]\"，实际：%r"
            % (p.stdout + p.stderr)
        )


class TestPortLock:
    """单实例守卫（11.9.3）。

    为什么这条必须有：同一个 ``app_id`` 上跑两个长连接，飞书只会把事件
    投给其中一个，另一个**静默失效** —— 现场是「bot 时好时坏」，两边日志
    都正常、都没有报错。所以它是一条边界，不是个便利功能。
    """

    def _port(self) -> int:
        """挑一个当前没人用的端口。固定端口会让并发跑测试时互相干扰。"""
        import socket

        s = socket.socket()
        try:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]
        finally:
            s.close()

    def test_first_acquire_ok_second_raises(self):
        port = self._port()
        first = PortLock(port)
        first.acquire()
        try:
            with pytest.raises(AlreadyRunning):
                PortLock(port).acquire()
        finally:
            first.release()

    def test_release_frees_the_port(self):
        """进程退出（含崩溃）后端口必须能再拿到 —— 这正是不用锁文件的原因。"""
        port = self._port()
        first = PortLock(port)
        first.acquire()
        first.release()
        second = PortLock(port)
        second.acquire()                  # 不该抛
        second.release()

    def test_release_is_idempotent(self):
        lock = PortLock(self._port())
        lock.acquire()
        lock.release()
        lock.release()                     # 再来一次也不该炸

    def test_context_manager_releases(self):
        port = self._port()
        with PortLock(port):
            with pytest.raises(AlreadyRunning):
                PortLock(port).acquire()
        # 出了 with 就该能再拿。用 ``with`` 而不是裸 acquire()：裸的还得手动
        # release()，漏了就留下一个 unclosed socket，而 ``filterwarnings =
        # ["error"]`` 会把它变成一条看不懂的失败。
        with PortLock(port):
            pass

    def test_context_manager_releases_on_exception(self):
        port = self._port()
        with pytest.raises(RuntimeError):
            with PortLock(port):
                raise RuntimeError("boom")
        with PortLock(port):          # 异常退出也要释放
            pass

    def test_error_message_says_how_to_change_port(self):
        """报错必须可操作：只说「已被占用」的话用户不知道该怎么办。"""
        port = self._port()
        first = PortLock(port)
        first.acquire()
        try:
            with pytest.raises(AlreadyRunning) as exc:
                PortLock(port).acquire()
            text = str(exc.value)
            assert ENV_LOCK_PORT in text, f"报错里要说明怎么换端口：{text}"
            assert "已经有一个桥接在跑" in text, f"要说清原因：{text}"
        finally:
            first.release()

    def test_excludes_another_process(self):
        """**必须跨进程**成立 —— 设计文档要的就是「同一台机器一个桥接」。

        同进程里 bind 两次报错，不代表换进程也报错，所以这条专门起一个
        真子进程去占端口。

        踩过的坑（很隐蔽）：子进程里一开始写的是 ``PortLock(p).acquire()`` ——
        临时对象 ``acquire()`` 一返回就被回收，socket 跟着关闭，端口**当场
        就放掉了**，于是「第二个进程也占住了同一个端口」。这正说明 ``main()``
        里那句 ``lock = PortLock(...)`` 必须是**赋值**：少写一个 ``lock =``，
        整道守卫静默失效，而所有同进程测试照样全绿。
        """
        import os as _os
        import subprocess

        port = self._port()
        src = str(ROOT / "src")
        child = subprocess.Popen(
            [sys.executable, "-c",
             "import sys, time;"
             f"sys.path.insert(0, {src!r});"
             "from freeagent.feishu.bridge import PortLock;"
             f"lock = PortLock({port});"   # 必须存进变量，见上面的坑
             "lock.acquire();"
             "print('held', flush=True);"
             "time.sleep(30)"],
            stdout=subprocess.PIPE, text=True, encoding="utf-8",
            env={**_os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        try:
            assert child.stdout.readline().strip() == "held", "子进程没占住端口"
            second = PortLock(port)
            try:
                second.acquire()
            except AlreadyRunning:
                return                      # 正是要的行为
            else:
                second.release()
                pytest.fail("第二个进程也占住了同一个端口，单实例守卫失效")
        finally:
            child.terminate()
            child.wait(timeout=10)
            child.stdout.close()            # 漏了就留下 ResourceWarning

    def test_main_releases_lock_when_assembly_fails(self, monkeypatch):
        """回归：装配失败也必须放锁。

        踩过的坑：``main()`` 里第二个 ``ImportError`` 分支忘了 ``lock.release()``，
        而非 ``ImportError`` 的异常更是直接往外抛、连 ``app.close()`` 都不走。
        漏放锁的后果不是「多占一会儿」，而是**下一次也起不来**，
        而用户只看到一句「端口已被占用」—— 没有任何线索指回真正的原因。
        正是这道守卫本该解决的问题自己变成了故障。
        """
        import socket as _socket

        s = _socket.socket()
        try:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        finally:
            s.close()

        monkeypatch.setenv(ENV_LOCK_PORT, str(port))
        monkeypatch.setenv(ENV_APP_ID, "cli_x")
        monkeypatch.setenv(ENV_APP_SECRET, "s")
        monkeypatch.setenv(ENV_ALLOWED, ALICE)

        def boom(*_a, **_kw):
            raise RuntimeError("装配炸了")

        # ``build_app`` 是在 ``main()`` 内部 import 的，所以补在模块属性上
        # 才会被看见（提前 import 的话补的是另一份绑定）。
        monkeypatch.setattr("freeagent.app.build_app", boom)

        # 意外的异常**不吞**：让它带 traceback 冒出来，资源照样收干净。
        with pytest.raises(RuntimeError, match="装配炸了"):
            bridge_main([])

        # 关键断言：端口必须已经还回来，否则用户再也起不来第二次。
        with PortLock(port):
            pass

    def test_main_returns_4_when_already_running(self, monkeypatch):
        """第二个桥接必须**非 0 退出**。

        退出码 0 会被 ``cmd && next``、CI 脚本判定为「启动成功」，
        而实际上一条事件都没收到。
        """
        port = self._port()
        monkeypatch.setenv(ENV_LOCK_PORT, str(port))
        monkeypatch.setenv(ENV_APP_ID, "cli_x")
        monkeypatch.setenv(ENV_APP_SECRET, "s")
        monkeypatch.setenv(ENV_ALLOWED, ALICE)

        with PortLock(port):
            got = bridge_main([])
        assert got == 4, f"该返回 4（已被占用），实际 {got}"

    def test_main_returns_2_on_bad_config(self, monkeypatch):
        """白名单为空 = 谁都不许指挥本机，这必须在起库之前就拒掉。"""
        monkeypatch.setenv(ENV_APP_ID, "cli_x")
        monkeypatch.setenv(ENV_APP_SECRET, "s")
        monkeypatch.setenv(ENV_ALLOWED, "")
        assert bridge_main([]) == 2

    def test_main_returns_2_on_garbage_lock_port(self, monkeypatch):
        """锁端口配错也要走配置错误那条路（2），而不是 4。

        4 的意思是「已经有另一个桥接在跑」—— 用它去说「你自己配错了端口」
        会把人引到完全错误的方向上去排查。
        """
        monkeypatch.setenv(ENV_LOCK_PORT, "not-a-port")
        monkeypatch.setenv(ENV_APP_ID, "cli_x")
        monkeypatch.setenv(ENV_APP_SECRET, "s")
        monkeypatch.setenv(ENV_ALLOWED, ALICE)
        assert bridge_main([]) == 2


class TestLockPortFromEnv:
    def test_defaults(self, monkeypatch):
        monkeypatch.delenv(ENV_LOCK_PORT, raising=False)
        assert lock_port_from_env() == DEFAULT_LOCK_PORT

    def test_override(self, monkeypatch):
        monkeypatch.setenv(ENV_LOCK_PORT, "12345")
        assert lock_port_from_env() == 12345

    @pytest.mark.parametrize("raw", ["", "   "])
    def test_blank_falls_back_to_default(self, monkeypatch, raw):
        monkeypatch.setenv(ENV_LOCK_PORT, raw)
        assert lock_port_from_env() == DEFAULT_LOCK_PORT

    def test_garbage_raises_config_error(self, monkeypatch):
        """配错要**报错**，不能悄悄回默认值。

        静默回默认的后果：用户以为自己改了端口、实际两个实例都锁在同一个
        默认端口上，第二个桥接仍然起不来。
        """
        monkeypatch.setenv(ENV_LOCK_PORT, "not-a-port")
        with pytest.raises(ConfigError, match=ENV_LOCK_PORT):
            lock_port_from_env()

    def test_default_port_avoids_web_port(self):
        """**别和 Web 的 8770 撞** —— 两个进程撞端口会互相误杀。"""
        assert DEFAULT_LOCK_PORT != 8770


class TestDoctorAgreesWithBridge:
    """doctor 与桥接必须对「端口有没有被占」看法一致。

    两者若各写一套端口解析，就会出现「doctor 说没占、桥接说占了」——
    这种分裂没法排查，所以 doctor **复用**桥接的函数而不是自己再写一遍。
    """

    def _cfg(self):
        return load_config({
            ENV_APP_ID: "cli_x", ENV_APP_SECRET: "secret",
            ENV_ALLOWED: ALICE,
        })

    def test_reports_free_when_free(self, monkeypatch):
        port = self._free_port()
        monkeypatch.setenv(ENV_LOCK_PORT, str(port))
        got = check_lock_free()
        assert got.ok, f"空闲时该报 OK：{got.detail}"
        assert str(port) in got.detail, f"要说清是哪个端口：{got.detail}"

    def test_reports_taken_when_taken(self, monkeypatch):
        port = self._free_port()
        monkeypatch.setenv(ENV_LOCK_PORT, str(port))
        held = PortLock(port)
        held.acquire()
        try:
            got = check_lock_free()
            assert not got.ok, "被占时不该报 OK"
            assert "桥接" in got.detail, f"要说清是桥接占着：{got.detail}"
        finally:
            held.release()

    def test_invalid_port_is_fatal(self, monkeypatch):
        monkeypatch.setenv(ENV_LOCK_PORT, "abc")
        got = check_lock_free()
        assert not got.ok and got.fatal, "配错端口是致命项"

    def test_identity_check_is_not_fatal_when_missing(self, tmp_path, monkeypatch):
        """**探不到身份不是致命**：私聊还能用。

        标致命会让人以为整个通道都废了，反而不好定位 —— 真实情况只是
        群里不响应。
        """
        monkeypatch.delenv("FREEAGENT_HOME", raising=False)
        got = check_identity(self._cfg(), tmp_path, live=False)
        assert not got.ok, "没缓存又没联网就该报未探到"
        assert not got.fatal, "身份缺失不该标致命"
        assert "私聊" in got.detail, "要说清代价只限群聊"
        assert "doctor" in got.detail or "live" in got.detail, "要告诉用户怎么查"

    def test_identity_check_reads_cache_offline(self, tmp_path):
        from freeagent.feishu.identity import save_identity

        save_identity(
            BotIdentity(BOT, "缓存里的"), tmp_path, now=1000.0
        )
        got = check_identity(self._cfg(), tmp_path, live=False, now=1000.0)
        assert got.ok, f"命中缓存就该 OK：{got.detail}"
        assert BOT in got.detail
        assert "缓存里的" in got.detail

    def _free_port(self) -> int:
        import socket

        s = socket.socket()
        try:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]
        finally:
            s.close()


class ExplodingSender(FeishuSender):
    """**每次联网都记账**的假 sender。

    记账而不是只靠「抛异常」，是因为 ``fetch_identity`` 里有宽泛的
    ``except Exception`` —— 异常会被当成探测失败**吞掉**，于是「用异常证明
    没联网」根本不成立。次数不会被吞，所以结论可靠。

    ``super().__init__()`` 正常调用：``FeishuSender`` 的构造只存字段、初始化
    锁，**没有网络副作用**，所以没必要为省事而跳过（跳过会被类型检查判
    ``reportMissingSuperCall``，而且会让「这是个 sender」这件事变得可疑）。
    """

    def __init__(self) -> None:
        super().__init__(
            FeishuConfig(
                app_id="cli_test",
                # 与 config.py 同一套兜底：as_secret 对空串返回 None。
                app_secret=as_secret("t", label=ENV_APP_SECRET)
                or SecretStr("", label=ENV_APP_SECRET),
                allowed_users=frozenset({ALICE}),
                domain="feishu",
                reminder_poll=60.0,
            )
        )
        self.calls: list[str] = []

    def get_json(self, path: str) -> dict[str, object]:
        self.calls.append(path)
        raise AssertionError(f"不该发网络请求，却调了 {path}")


class TestIdentityGatingSeam:
    """「探测身份」与「@ 门控」这个**接缝**上的测试。

    踩过的坑（类型检查抓出来的，1076 个测试全绿都没抓到）：``main()`` 里
    ``resolve_identity`` 返回的是 ``BotIdentity`` **对象**，而 ``FeishuBridge``
    要的是 ``str``。之前把对象直接传了进去，于是 ``events.py`` 里
    ``mention_open_id == bot_open_id`` **恒为 False** —— 真实环境里 bot 在群里
    永远不会被 @ 叫醒，而且一声不吭。

    为什么 1076 个测试都漏了：每条门控测试都直接注入 ``"ou_bot"`` 这样的
    **裸字符串**，从没走过 ``resolve_identity`` → 桥接 → 门控这条真实装配。
    所以这里刻意**从缓存探身份**开始，一路走到门控，模拟 ``main()`` 的做法。
    """

    @staticmethod
    def _mention(key, open_id, name=""):
        m = {"key": key, "id": {"open_id": open_id}}
        if name:
            m["name"] = name
        return m

    @staticmethod
    def _sender():
        """假 sender：**记录**每次 ``get_json``，并当场炸。

        刻意继承 ``FeishuSender``：``resolve_identity`` 的形参是**有类型**的，
        光有个同名方法不算数。

        踩过的坑：原先只让它 ``raise AssertionError``，以为这样就等于「证明没
        联网」。其实不然 —— ``fetch_identity`` 里有宽泛的 ``except Exception``，
        ``AssertionError`` 会被**当成探测失败吞掉**，测试照样绿，绿得毫无意义。
        所以改成**记录调用次数**，由断言去数：缓存命中必须是 0 次（真没联网），
        未命中必须是 1 次（真去探了）。炸仍然炸，但结论不依赖异常有没有被吞。
        """
        sender = ExplodingSender()
        return sender

    def _resolve_open_id(self, tmp_path):
        """照 ``main()`` 的做法：探身份，然后取出 ``open_id`` 字符串。"""
        save_identity(BotIdentity(BOT, "助手"), tmp_path, now=1000.0)
        sender = self._sender()
        identity, note = resolve_identity(sender, home=tmp_path, now=1000.0)
        assert identity is not None, f"缓存里明明有身份：{note}"
        assert sender.calls == [], f"命中缓存就不该联网，却调了 {sender.calls}"
        return identity.open_id if identity is not None else None

    @staticmethod
    def _gate(payload, bot_open_id):
        """跑门控，并把结果收窄成 ``IncomingMessage``。

        ``parse_event`` 返回三态联合（``IncomingMessage | Ignored | None``），
        群消息**被拦也仍然是** ``IncomingMessage``（只是 ``mentioned=False``），
        ``Ignored`` 只留给「不是我要处理的事件」。所以这里断言拿到的是
        ``IncomingMessage``，既消掉了联合类型的噪音，也顺手多一层保证：
        门控不该把这条消息整个丢掉。
        """
        msg = parse_event(payload, bot_open_id=bot_open_id)
        assert isinstance(msg, IncomingMessage), f"群消息不该被整个丢弃：{msg!r}"
        return msg

    def _resolve_open_id(self, tmp_path):
        """照 ``main()`` 的做法：探身份，然后取出 ``open_id`` 字符串。"""
        save_identity(BotIdentity(BOT, "助手"), tmp_path, now=1000.0)
        identity, note = resolve_identity(
            self._sender(), home=tmp_path, now=1000.0
        )
        assert identity is not None, f"缓存里明明有身份：{note}"
        return identity.open_id if identity is not None else None

    def test_bridge_receives_a_string(self, tmp_path):
        """桥接存下的必须是 ``str``，不是 ``BotIdentity``。"""
        bot_open_id = self._resolve_open_id(tmp_path)
        assert isinstance(bot_open_id, str), (
            f"必须是字符串，实际拿到 {type(bot_open_id).__name__}"
        )

    def test_group_mention_of_bot_actually_passes_the_gate(self, tmp_path):
        """**这条才是重点**：从缓存探到身份后，群里 @ bot 真的能通过门控。

        修之前这里必然失败：门控拿 ``BotIdentity`` 去和 mention 的
        ``open_id`` 比，永不相等，于是 ``mentioned`` 恒为 ``False`` ——
        bot 在群里彻底失聪，而没有任何报错。
        """
        bot_open_id = self._resolve_open_id(tmp_path)
        msg = self._gate(_payload(
            text="@_user_1 帮我记一下",
            chat_type="group",
            mentions=[self._mention("@_user_1", BOT, "助手")],
        ), bot_open_id)
        assert msg.mentioned is True, (
            f"群里 @ bot 自己却没通过门控：{msg.mention_note}"
        )
        assert msg.mention_note == ""

    def test_someone_elses_mention_still_blocked(self, tmp_path):
        """对照：探到的身份是**别人**时仍要拦住。防止上面那条「改成永远放行」。"""
        bot_open_id = self._resolve_open_id(tmp_path)
        msg = self._gate(_payload(
            text="@_user_1 你看下这个",
            chat_type="group",
            mentions=[self._mention("@_user_1", ALICE, "张三")],
        ), bot_open_id)
        assert msg.mentioned is False
        assert "别人" in msg.mention_note

    def test_unresolved_identity_fails_closed(self, tmp_path):
        """探不到身份 → ``None`` → 失败关闭。断的是接缝的另一端。

        顺带锁住「**没有缓存就一定会去探**」：否则这条测试可能因为缓存
        意外命中而拿到 ``None`` 之外的路径，绿得毫无意义。
        """
        sender = self._sender()
        identity, _note = resolve_identity(
            sender, home=tmp_path / "空的", now=1000.0
        )
        assert len(sender.calls) == 1, (
            f"没缓存就必须真去探一次，却调了 {len(sender.calls)} 次：{sender.calls}"
        )
        bot_open_id = identity.open_id if identity is not None else None
        assert bot_open_id is None
        msg = self._gate(_payload(
            text="@_user_1 帮我记一下",
            chat_type="group",
            mentions=[self._mention("@_user_1", BOT, "助手")],
        ), bot_open_id)
        assert msg.mentioned is False
        assert "失败关闭" in msg.mention_note


class _ScriptedSender(FeishuSender):
    """前 N 次探测失败，之后返回**真实形状**的 bot 信息。

    刻意用真实的 ``FeishuError``：``fetch_identity`` 只认它，
    用别的异常类型测出来的路径不是真实路径。
    ``boom=True`` 则让**第一次**抛 RuntimeError —— 模拟「探测代码本身
    出了个意外」，用来验证一次意外不该把整条重试带走。
    """

    def __init__(self, succeed_after: int, *, boom: bool = False) -> None:
        super().__init__(
            FeishuConfig(
                app_id="cli_test",
                app_secret=as_secret("t", label=ENV_APP_SECRET)
                or SecretStr("", label=ENV_APP_SECRET),
                allowed_users=frozenset({ALICE}),
                domain="feishu",
                reminder_poll=60.0,
            )
        )
        self.succeed_after = succeed_after
        self.boom = boom
        self.calls = 0

    def get_json(self, path: str) -> dict[str, object]:
        self.calls += 1
        if self.boom and self.calls == 1:
            raise RuntimeError("模拟非 FeishuError 的意外")
        if self.calls <= self.succeed_after:
            raise FeishuError("模拟探测失败")
        return {"bot": {"open_id": BOT, "app_name": "助手"}}


class TestIdentityRecovery:
    """身份探测失败后的**后台重试**，成功后门控自动恢复、无需重启。

    没有这条时的表现很难排查：启动那一瞬网络抖了一下 → 探测失败 →
    群里 @ 永远不回 → 用户以为配置坏了，于是去查权限、查网络、查白名单 ——
    而真正原因是「你运气不好，重启一下就好了」。

    退避取值抄 openclaw 的 ``monitor.bot-identity.ts``（1/2/5/10/15 分钟共 5 次）。
    """

    @pytest.fixture()
    def fast_backoff(self, monkeypatch):
        """把退避压到毫秒级，否则测试要干等 33 分钟。"""
        monkeypatch.setattr(
            bridge_mod, "_IDENTITY_RETRY_DELAYS", (0.02,) * 5
        )

    @staticmethod
    def _wait_for(pred, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline and not pred():
            time.sleep(0.02)
        return pred()

    @staticmethod
    def _group_at_bot(bot_open_id) -> IncomingMessage:
        """群里 @ bot 的那条消息，**收窄成** ``IncomingMessage``。

        收窄放在这里而不是各调用点：三处断言都要访问 ``.mentioned``，
        在源头 ``assert isinstance`` 一次，类型就定了，顺带还多一层保证 ——
        这条群消息**不该被整个丢弃**（门控拦下也仍是 ``IncomingMessage``，
        ``Ignored`` 只留给「不是我要处理的事件」）。
        """
        msg = parse_event(_payload(
            text="@_user_1 帮我记一下", chat_type="group",
            mentions=[{"key": "@_user_1", "id": {"open_id": BOT}}],
        ), bot_open_id=bot_open_id)
        assert isinstance(msg, IncomingMessage), f"群消息不该被丢弃：{msg!r}"
        return msg

    def test_recovered_identity_reopens_group_gate(
        self, bridge_blind, tmp_path, fast_backoff
    ):
        """**核心**：后台探到身份后，群里 @ bot 真的能通过门控了。"""
        sender = _ScriptedSender(succeed_after=1)
        bridge_blind.start_identity_recovery(sender, home=tmp_path)
        assert self._wait_for(lambda: bridge_blind.bot_open_id is not None), (
            f"后台重试该探到身份（探了 {sender.calls} 次），"
            f"实际仍是 {bridge_blind.bot_open_id!r}"
        )
        assert bridge_blind.bot_open_id == BOT
        msg = self._group_at_bot(bridge_blind.bot_open_id)
        assert msg.mentioned is True, (
            f"探到身份后门控该放行，实际 note={msg.mention_note!r}"
        )

    def test_exhausted_backoff_stays_fail_closed(
        self, bridge_blind, tmp_path, fast_backoff
    ):
        """退避用完仍探不到 → **保持失败关闭**，绝不退化成「永远放行」。"""
        sender = _ScriptedSender(succeed_after=99)
        bridge_blind.start_identity_recovery(sender, home=tmp_path)
        assert self._wait_for(lambda: sender.calls >= 5), (
            f"应把 5 档退避用完，实际只探了 {sender.calls} 次"
        )
        assert bridge_blind.bot_open_id is None, "全都探不到就必须保持 None"
        assert self._group_at_bot(None).mentioned is False, (
            "探不到身份时群里 @ bot 仍必须拦住"
        )

    def test_unexpected_exception_does_not_abort_retry(
        self, bridge_blind, tmp_path, fast_backoff
    ):
        """一次意外（RuntimeError）不该让整条重试放弃 —— 否则白重试了。"""
        sender = _ScriptedSender(succeed_after=0, boom=True)
        bridge_blind.start_identity_recovery(sender, home=tmp_path)
        assert self._wait_for(lambda: bridge_blind.bot_open_id is not None), (
            f"非 FeishuError 的意外也不该中断重试（探了 {sender.calls} 次）"
        )

    def test_stop_interrupts_the_backoff_wait(
        self, bridge_blind, tmp_path, monkeypatch
    ):
        """退避用 ``_stop.wait()``：桥接一停就该收手，不该再发探测请求。"""
        monkeypatch.setattr(bridge_mod, "_IDENTITY_RETRY_DELAYS", (0.4,) * 5)
        sender = _ScriptedSender(succeed_after=99)
        bridge_blind.start_identity_recovery(sender, home=tmp_path)
        bridge_blind.stop()
        time.sleep(0.15)
        before = sender.calls
        time.sleep(0.6)                 # 远超第一档退避
        assert sender.calls == before, (
            f"桥接已停却还在探：{before} → {sender.calls}"
        )
