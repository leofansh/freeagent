"""CardKit 2.0 流式进度卡片（设计文档 11.16）。

## 这个文件守什么

飞书进度卡片是本项目**第一个** CardKit 2.0 用途，而 2.0 与 1.0 有实质
差异（``schema: "2.0"`` + ``body.elements``，且必须显式声明版本）。混用
版本的后果是飞书直接拒收（错误码 200830），而那种失败在离线测试里看
不出来 —— 所以卡片结构必须被断言钉死。

## 三条要害

1. **必须是 JSON 2.0 结构** —— 本项目其它卡片都是 1.0
   （``config``/``header``/``elements`` 在顶层）。这个文件故意断言
   「长得不一样」，因为**它就是**不一样。
2. **``streaming_mode`` 必须在创建时开** —— 否则后续流式更新报
   ``300309 Card streaming closed``。
3. **sequence 严格递增** —— 飞书用它保证多次更新的时序性。
"""
from __future__ import annotations

import io
import email.message
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

from freeagent.feishu.config import FeishuConfig  # noqa: E402
from freeagent.feishu.sender import (  # noqa: E402
    FeishuSender,
    FeishuError,
    _progress_card,
    _tool_card,
)
from freeagent.secrets import SecretStr, as_secret  # noqa: E402

WHO = "ou_owner"
CHAT = "oc_chat"


class _Transport:
    """记录请求，不真发网络。

    签名与 :data:`~freeagent.feishu.sender.Transport` 一致（含 ``method``）。

    第一个请求恒为取 token（FeishuSender 惰性取），返回 token 响应；
    之后按 ``responses`` 队列依次返回，队列空则用 ``default``。
    """

    def __init__(self, *responses: str) -> None:
        # ``dict`` 裸写要类型参数。值是异构的（url/payload/method/bool），
        # 所以是 ``Any`` 而不是 ``object``。
        self.calls: list[dict[str, Any]] = []
        self.responses = [
            r if isinstance(r, str) else json.dumps(r)
            for r in responses
        ]
        self.default = '{"code":0,"data":{}}'

    @property
    def business_calls(self) -> list[dict[str, Any]]:
        """只数**业务**调用 —— 取 token 那次与断言无关。"""
        return [c for c in self.calls if not c["is_token"]]

    def __call__(self, url, payload, headers, timeout, method="POST"):
        is_token_call = "tenant_access_token" in url
        self.calls.append({
            "url": url, "payload": payload,
            "headers": headers, "method": method,
            "is_token": is_token_call,
        })
        if is_token_call:
            return _TOKEN_RESPONSE
        if self.responses:
            return self.responses.pop(0)
        return self.default


def _token_response() -> str:
    """取 token 的响应 —— ``tenant_access_token`` 在**顶层**，不在 data 里。"""
    return '{"code":0,"msg":"ok","tenant_access_token":"t-test","expire":7200}'


_TOKEN_RESPONSE = _token_response()


def _config() -> FeishuConfig:
    return FeishuConfig(
        app_id="cli_test",
        app_secret=as_secret("t", label="FEISHU_APP_SECRET")
        or SecretStr("", label="FEISHU_APP_SECRET"),
        allowed_users=frozenset({WHO}),
        domain="feishu",
        reminder_poll=60.0,
    )


def _make_sender(transport: _Transport) -> FeishuSender:
    return FeishuSender(_config(), transport=transport)


# --------------------------------------------------------------------------- #
# 卡片结构：必须是 2.0
# --------------------------------------------------------------------------- #
def test_progress_card_is_not_exclusive():
    """``update_multi`` 必须显式为 True —— **省略是危险的默认值**。

    ## 为什么这条测试钉死一个「本可以省略」的字段

    真机踩过（2026-10-12）：卡片建得出来、也投递出去了（message_id 有），
    但**收卡人看不到内容** —— 现象是「飞书只有权限审批、没有进展」。
    而权限卡走 ``im/v1/messages`` 普通消息路径，不受影响。

    官方对建卡接口的使用限制原文：

        「不支持将卡片设置为**独享卡片模式**。即不支持将卡片 JSON 数据中的
         ``update_multi`` 属性设置为 ``false``。」

    ``update_multi: false`` 即独享卡片模式。**不写这个字段时飞书按独享处理**
    —— 文档没写「省略等价于 true」，所以省略就是踩坑。三份独立参考实现
    （HKUDS/nanobot、Vibe-Trading、CowAgent）建卡时都显式带
    ``update_multi: True``，不是巧合。

    这类 bug 的恶劣之处在于**每一层都显示成功**：建卡 code=0、投递 code=0、
    流式更新 code=0、日志零 ERROR。只有人眼睛能看到卡片里有没有东西。
    """
    card = _progress_card("标题", "D:/proj")
    assert card["config"].get("update_multi") is True, (
        "update_multi 必须显式为 True，否则卡片进独享模式、收卡人看不到"
    )


def test_progress_card_is_wide_screen():
    """进度卡是给人盯着的，宽屏比窄屏可读。同一处补的另一个字段。"""
    card = _progress_card("标题", "D:/proj")
    assert card["config"].get("wide_screen_mode") is True


def test_progress_card_summary_is_not_empty():
    """``summary`` 不能是空串。

    空 summary 时聊天列表里那条消息**没有可读的预览行**，而进度卡的
    全部价值就在于「不用点开就知道它在动」。
    """
    card = _progress_card("改个东西", "D:/proj")
    assert card["config"]["summary"]["content"].strip(), (
        "空 summary 会让聊天列表那条没有预览"
    )
    assert "改个东西" in card["config"]["summary"]["content"]


def test_progress_card_is_json_2_0():
    """进度卡片必须是 JSON 2.0 —— 与本项目其它卡片（1.0）故意不同。"""
    card = _progress_card("标题", "D:/proj")
    assert card["schema"] == "2.0", "没声明 2.0 就是 1.0，飞书会拒"
    assert "body" in card and "elements" in card["body"], (
        "2.0 的组件在 body.elements 里，不在顶层 elements"
    )
    # 1.0 的标志**不许**出现
    assert "elements" not in card, "顶层 elements 是 1.0 形状"


def test_progress_card_enables_streaming_mode():
    """``streaming_mode`` 必须在创建时开，否则后续流式更新报 300309。"""
    card = _progress_card("标题", "D:/proj")
    assert card["config"]["streaming_mode"] is True


def test_progress_card_has_a_stable_element_id():
    """markdown 组件要有固定 ``element_id`` —— 流式更新按它定位。"""
    card = _progress_card("标题", "D:/proj")
    elements = card["body"]["elements"]
    assert len(elements) == 1
    assert elements[0]["tag"] == "markdown"
    assert elements[0]["element_id"] == "progress_text"


def test_progress_card_initial_content_names_the_project():
    """初始内容要带项目名 —— 用户点开就知道是哪一条。"""
    card = _progress_card("标题", "D:/PycharmProjects/xiaoyuan")
    assert "D:/PycharmProjects/xiaoyuan" in card["body"]["elements"][0]["content"]


# --------------------------------------------------------------------------- #
# 发送：创建卡片实体
# --------------------------------------------------------------------------- #
def test_send_progress_card_returns_card_id():
    """返回 :class:`ProgressCard` —— card_id 与 message_id **都要拿到**。

    ``card_id`` 给流式更新用（按它定位组件），``message_id`` 给授权卡
    挂回复用。两个都是 ``str``，混了不报错 —— 所以用 dataclass 绑成
    一个，让「拿错」变成「属性不存在」。
    """
    tr = _Transport(
        '{"code":0,"data":{"card_id":"7355439197428236291"}}',
        '{"code":0,"data":{"message_id":"om_1"}}',
    )
    sender = _make_sender(tr)
    card = sender.send_progress_card(
        open_id=WHO, title="标题", project="D:/proj", chat_id=CHAT,
    )
    assert card.card_id == "7355439197428236291"
    assert card.message_id == "om_1", "授权卡要靠它挂回复"
    # 建实体是**第一步**，发进会话是第二步（见下面的回归测试）
    assert tr.business_calls[0]["url"].endswith("/open-apis/cardkit/v1/cards")


def test_send_progress_card_rejects_empty_open_id():
    """既没会话也没收件人必须当场拒 —— 不建一张不知道去哪的卡。

    判据是「两者都空」，不再单看 open_id：有 chat_id 时 open_id 可以是空的
    （那是合法的一对），而 open_id 与 chat_id 都空就真的没有落点。
    """
    tr = _Transport()
    sender = _make_sender(tr)
    with pytest.raises(ValueError):
        sender.send_progress_card(
            open_id="", title="t", project="p", chat_id="",
        )
    assert not tr.calls, "被拒了却发了请求"


def test_send_progress_card_missing_card_id_is_an_error():
    """响应里没有 card_id 要响亮失败 —— 静默返回空串会让后续更新
    全打到空 id 上，而报错指不到真正的原因。"""
    tr = _Transport('{"code":0,"data":{}}')
    sender = _make_sender(tr)
    with pytest.raises(FeishuError):
        sender.send_progress_card(
            open_id=WHO, title="t", project="p", chat_id=CHAT,
        )


# --------------------------------------------------------------------------- #
# 流式更新：PUT + 目标组件
# --------------------------------------------------------------------------- #
def test_update_progress_text_hits_the_element_endpoint():
    """更新要打到 ``elements/progress_text/content`` —— 飞书的
    流式更新文本接口。"""
    tr = _Transport()
    sender = _make_sender(tr)
    sender.update_progress_text(card_id="c1", text="新内容", sequence=2)
    call = tr.business_calls[0]
    assert call["url"].endswith("/open-apis/cardkit/v1/cards/c1"
                                "/elements/progress_text/content")
    assert call["payload"]["content"] == "新内容"
    assert call["payload"]["sequence"] == 2


def test_update_progress_text_uses_put():
    """流式更新文本是 PUT，不是 POST。"""
    tr = _Transport()
    sender = _make_sender(tr)
    sender.update_progress_text(card_id="c1", text="x", sequence=1)
    assert tr.business_calls[0]["method"] == "PUT"


def test_update_progress_text_failure_raises():
    """飞书业务码非 0 要抛 —— 调用方（delegate）靠它记日志。"""
    tr = _Transport('{"code":300309,"msg":"Card streaming closed"}')
    sender = _make_sender(tr)
    with pytest.raises(FeishuError):
        sender.update_progress_text(card_id="c1", text="x", sequence=1)


# --------------------------------------------------------------------------- #
# 收尾：先关流式，再设最终内容
# --------------------------------------------------------------------------- #
def test_close_stream_sets_text_then_disables_streaming():
    """**顺序：先 PUT 最终内容，再 PATCH 关流式** —— 真机定的（2026-10-12）。

    ## 这条断言为什么是这个顺序而不是反过来

    我原先按「先关流式、再设内容」写，理由是「关掉之后 PUT 就是普通更新」。
    真机一跑就报 ``code=300309 ErrMsg: streaming mode is closed`` ——
    ``PATCH /settings`` 一关，**元素内容接口就不再受理了**。

    所以这不是「哪个顺序更优雅」，是「另一个顺序根本跑不通」。
    离线测试原先全绿，正是因为它只验参数、不验飞书接不接受这个顺序。
    """
    tr = _Transport()
    sender = _make_sender(tr)
    sender.close_progress_stream(card_id="c1", final_text="完成", sequence=5)

    calls = tr.business_calls
    assert len(calls) == 2, "应当两步：设内容、关流式"
    put, patch = calls
    assert put["method"] == "PUT", "最终内容走元素内容接口"
    assert put["payload"]["content"] == "完成"
    assert put["payload"]["sequence"] == 5
    assert patch["method"] == "PATCH", "关流式走 settings 接口"
    assert patch["url"].endswith(f"/open-apis/cardkit/v1/cards/c1/settings")
    # 关流式的 sequence 必须接在最后那次 PUT 之后
    assert patch["payload"]["sequence"] == 6


def test_close_stream_sequence_continues():
    """最终内容的 sequence 必须接续，不许回到 1 —— 否则飞书报
    「时序倒流」而内容静默丢失。"""
    tr = _Transport()
    sender = _make_sender(tr)
    sender.close_progress_stream(card_id="c1", final_text="x", sequence=100)
    assert tr.business_calls[0]["payload"]["sequence"] == 100
    assert tr.business_calls[1]["payload"]["sequence"] == 101


# --------------------------------------------------------------------------- #
# 真机诊断：HTTP 错误必须带出飞书的 code/msg
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# 授权卡聚拢到进度卡下（设计文档 11.16，方向 A）
# --------------------------------------------------------------------------- #
class TestToolCardThreadsUnderProgressCard:
    """授权卡走 ``reply`` 而不是 ``send`` —— 一次委派只多一张卡。"""

    def test_reply_to_uses_the_reply_endpoint(self) -> None:
        """给了锚点就必须打 ``/messages/{id}/reply``。"""
        tr = _Transport('{"code":0,"data":{"message_id":"om_r"}}')
        sender = _make_sender(tr)
        sender.send_tool_card(
            open_id=WHO, subject="改 x.py", detail="+1",
            credential="cred-1", ttl_seconds=600, reply_to="om_anchor",
        )
        call = tr.business_calls[-1]
        assert "/open-apis/im/v1/messages/om_anchor/reply" in call["url"]

    def test_reply_path_drops_receive_id_type(self) -> None:
        """reply 路径**不能**带 ``receive_id_type``。

        收件人由被回复的那条消息决定，带着这个 query 飞书会拒 —— 而拒的
        表现是「授权卡发不出去」，那正是闸门失效的形态。
        """
        tr = _Transport('{"code":0,"data":{"message_id":"om_r"}}')
        sender = _make_sender(tr)
        sender.send_tool_card(
            open_id=WHO, subject="s", detail="d", credential="c",
            ttl_seconds=600, reply_to="om_anchor",
        )
        url = tr.business_calls[-1]["url"]
        assert "receive_id_type" not in url
        # 也**不该**有 receive_id —— 那一栏在 reply 路径没有意义。
        assert "receive_id" not in tr.business_calls[-1]["payload"]

    def test_reply_in_thread_is_false(self) -> None:
        """``reply_in_thread`` 给 False。

        true 会把**被回复的那条也拖进话题串**，而私聊没有话题 —— 于是
        进度卡自己被卷进话题，用户要找它反而更麻烦。给 False 才是普通的
        「回复」，卡片留在原位、位置不动。
        """
        tr = _Transport('{"code":0,"data":{"message_id":"om_r"}}')
        sender = _make_sender(tr)
        sender.send_tool_card(
            open_id=WHO, subject="s", detail="d", credential="c",
            ttl_seconds=600, reply_to="om_anchor",
        )
        assert tr.business_calls[-1]["payload"]["reply_in_thread"] is False

    def test_empty_reply_to_falls_back_to_send(self) -> None:
        """**空锚点必须退回原路径** —— 闸门不许因为想好看而失效。

        没有进度卡可挂时（没配 progress_sender、或发卡失败降级），
        授权卡照发到会话，那条路仍然是通的。
        """
        tr = _Transport('{"code":0,"data":{"message_id":"om_s"}}')
        sender = _make_sender(tr)
        sender.send_tool_card(
            open_id=WHO, subject="s", detail="d", credential="c",
            ttl_seconds=600, reply_to="",
        )
        call = tr.business_calls[-1]
        assert "/reply" not in call["url"]
        assert "receive_id_type=" in call["url"]
        assert call["payload"]["receive_id"] == WHO

    def test_question_card_also_threads(self) -> None:
        """提问卡与授权卡**同一套判定** —— 复制两份的话，改一处就会出现
        「授权卡聚拢了、提问卡没有」，而那看起来像功能半成品，不像 bug。"""
        tr = _Transport('{"code":0,"data":{"message_id":"om_q"}}')
        sender = _make_sender(tr)
        sender.send_question_card(
            open_id=WHO, subject="q", detail="d", credential="c",
            ttl_seconds=600, reply_to="om_anchor",
        )
        assert "/open-apis/im/v1/messages/om_anchor/reply" in \
            tr.business_calls[-1]["url"]

    def test_question_card_falls_back_too(self) -> None:
        tr = _Transport('{"code":0,"data":{"message_id":"om_q"}}')
        sender = _make_sender(tr)
        sender.send_question_card(
            open_id=WHO, subject="q", detail="d", credential="c",
            ttl_seconds=600, reply_to="",
        )
        assert "/reply" not in tr.business_calls[-1]["url"]

    def test_reply_failure_names_the_anchor(self) -> None:
        """reply 失败要在报错里说清**锚点**。

        两种发法的排查方向不同：reply 失败通常是锚点那条消息被撤回了，
        而 send 失败是收件人/权限问题。报错文案混起来会让人查错地方。
        """
        tr = _Transport('{"code":230002,"msg":"message not found"}')
        sender = _make_sender(tr)
        with pytest.raises(FeishuError) as ei:
            sender.send_tool_card(
                open_id=WHO, subject="s", detail="d", credential="c",
                ttl_seconds=600, reply_to="om_gone",
            )
        assert "om_gone" in str(ei.value)


class TestHttpErrorKeepsFeishuReason:
    """飞书的 HTTP 400 **必然**带 JSON 错误体说明原因。

    ## 为什么这条测试重要

    真机踩过（2026-10-12）：创建进度卡片返回 HTTP 400，而传输层只把状态码
    写进异常，报的是「飞书接口 HTTP 400」—— 分不清是**没开权限**（99991672）
    还是**卡片结构不合法**（200830）。前者要去开发者后台申请权限，后者要改代码，
    两条完全不同的路，而「400」两边都不指向。

    真正的答案一直躺在响应体的 ``code``/``msg`` 里。丢掉它就是把唯一有用的
    线索扔掉，所以这条测试守的就是「不许再丢」。

    ## 为什么直接测 :func:`urllib_transport`

    HTTPError → FeishuError 的转换**就在这个函数里面**。注入一个假 transport
    会整段绕过它 —— 那样测出来的绿毫无意义（第一版就是这么写的，红了才发现）。
    所以这里 mock 的是它**下面**那一层 ``urlopen``。
    """

    def test_card_entity_is_actually_sent_to_a_chat(self) -> None:
        """**建实体 ≠ 发出去。** 少发这一步，卡片永远不在聊天里。

        真机踩过（2026-10-12）：只调了 ``POST /cardkit/v1/cards`` 拿到 card_id
        就当「已发进度卡片」，而那张卡**从未出现在任何会话** ——
        现象是「飞书只有权限审批、没有进展」，日志却干净得看不出问题。
        官方原文：「后续可通过**发送消息接口**传入卡片实体 ID 发送卡片」。

        这条钉住「建完实体必须紧跟一次 im/v1/messages」，且 content 里装的是
        card_id 引用（不是卡片内容）—— 只有引用才能让后续流式更新作用在
        同一条消息上。
        """
        tr = _Transport(
            '{"code":0,"data":{"card_id":"7355439197428236291"}}',
            '{"code":0,"data":{"message_id":"om_1"}}',
        )
        sender = _make_sender(tr)
        sender.send_progress_card(
            open_id=WHO, title="t", project="p", chat_id=CHAT,
        )
        send = tr.business_calls[-1]
        assert "/open-apis/im/v1/messages" in send["url"], (
            "建完卡片实体必须调 im/v1/messages 把它发出去"
        )
        assert "receive_id_type=chat_id" in send["url"]
        assert send["payload"]["receive_id"] == CHAT
        assert send["payload"]["msg_type"] == "interactive"
        content = json.loads(send["payload"]["content"])
        assert content == {"type": "card",
                           "data": {"card_id": "7355439197428236291"}}, (
            f"content 必须是卡片引用，实际 {content}"
        )

    def test_card_without_chat_falls_back_to_dm(self) -> None:
        """没有会话（Web 建的委派）时退化成私聊，不能就此不发。

        这正是「Web 派的活永远没有进度卡」的修法：权限卡一直走私聊能发出去，
        进度卡却要求会话，于是只有 Web 派的那条没有进展。
        """
        tr = _Transport(
            '{"code":0,"data":{"card_id":"c9"}}',
            '{"code":0,"data":{"message_id":"om_2"}}',
        )
        sender = _make_sender(tr)
        sender.send_progress_card(
            open_id=WHO, title="t", project="p", chat_id="",
        )
        send = tr.business_calls[-1]
        assert "receive_id_type=open_id" in send["url"]
        assert send["payload"]["receive_id"] == WHO

    def test_no_recipient_at_all_is_refused(self) -> None:
        """既没会话也没收件人必须当场拒 —— 不建一张不知道去哪的卡。"""
        tr = _Transport()
        sender = _make_sender(tr)
        with pytest.raises(ValueError):
            sender.send_progress_card(
                open_id="", title="t", project="p", chat_id="",
            )
        assert not tr.calls, "被拒了却发了请求"

    def test_send_failure_is_raised(self) -> None:
        """发送失败要抛 —— 调用方据此降级成「无卡片」，而不是以为发出去了。"""
        tr = _Transport(
            '{"code":0,"data":{"card_id":"c9"}}',
            '{"code":230001,"msg":"bot not in chat","data":{}}',
        )
        sender = _make_sender(tr)
        with pytest.raises(FeishuError):
            sender.send_progress_card(
                open_id=WHO, title="t", project="p", chat_id=CHAT,
            )

    def _boom(self, monkeypatch, code: int, body: str, *,
              read_raises: bool = False) -> str:
        import urllib.error
        from freeagent.feishu import sender as sender_mod

        class _Err(urllib.error.HTTPError):
            """**自己实现 read**，不调 ``super().read()``。

            ``HTTPError`` 的 ``read`` 是靠 ``addinfourl.__getattr__`` 转发到
            ``self.file`` 的，而 ``__init__`` 从没设过 ``.file``（真 urlopen
            传的 fp 走的是另一条路）。所以 ``super().read()`` 抛
            AttributeError，被发送层「读不到错误体也要报码」的兜底吃掉 ——
            测试于是拿到裸的「HTTP 400」，看起来像**发送层没生效**，
            实际是替身不会读。第一版就这么栽的。
            """

            def read(self, *a, **kw):
                if read_raises:
                    raise OSError("连接被重置")
                return body.encode()

        def _urlopen(request, timeout=None):
            # ``hdrs`` 收的是 ``email.message.Message``，不是裸 dict ——
            # 传 ``{}`` 运行期没问题（读错误体不走它），检查器却会报
            # 「dict 不能赋给 Message」。所以给个真的空 Message。
            raise _Err(request.full_url, code, "Bad Request",
                       email.message.Message(), None)

        monkeypatch.setattr(sender_mod.urllib.request, "urlopen", _urlopen)
        with pytest.raises(sender_mod.FeishuError) as ei:
            sender_mod.urllib_transport(
                "https://open.feishu.cn/open-apis/cardkit/v1/cards",
                {"type": "card_json"}, {}, 10.0,
            )
        return str(ei.value)

    def test_permission_error_is_reported_with_code(self, monkeypatch) -> None:
        """没开 ``cardkit:card:write`` 时的真机错误（code=99991672）。"""
        got = self._boom(monkeypatch, 400, json.dumps({
            "code": 99991672,
            "msg": "Access denied. One of the following scopes is required: "
                   "[cardkit:card:write]",
        }))
        assert "400" in got
        assert "99991672" in got, f"丢了飞书错误码，只剩状态码：{got}"
        assert "cardkit:card:write" in got, f"丢了权限名，查不动：{got}"

    def test_bad_request_card_structure_is_reported(self, monkeypatch) -> None:
        """卡片结构不合法（200830）也要看得见 —— 与权限不足的处置完全不同。"""
        got = self._boom(monkeypatch, 400, json.dumps({
            "code": 200830, "msg": "card json is invalid",
        }))
        assert "200830" in got

    def test_status_code_still_reported_when_body_is_not_json(
            self, monkeypatch) -> None:
        """响应体不是 JSON（网关错误页）时**不能炸**，仍要报出状态码。"""
        got = self._boom(monkeypatch, 502, "<html>bad gateway</html>")
        assert "502" in got

    def test_unreadable_body_still_reports_status(self, monkeypatch) -> None:
        """错误体读不出来也要报 HTTP 码 —— 不能因为诊断增强而把错变成崩溃。"""
        got = self._boom(monkeypatch, 400, "", read_raises=True)
        assert "400" in got

    def test_token_never_leaks_into_the_message(self, monkeypatch) -> None:
        """错误消息里**不许**出现 token —— 它只进请求头，不进响应体，
        但这条断言守的是「别哪天顺手把整段 body 带出来」。"""
        got = self._boom(monkeypatch, 400, json.dumps({
            "code": 99991672, "msg": "Access denied",
        }))
        assert "t-test" not in got, f"异常里混进了 token：{got}"
        assert "99991672" in got, f"丢了飞书错误码，只剩状态码：{got}"
