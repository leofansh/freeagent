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
    """创建成功要返回 card_id —— 后续更新靠它定位。"""
    tr = _Transport('{"code":0,"data":{"card_id":"7355439197428236291"}}')
    sender = _make_sender(tr)
    card_id = sender.send_progress_card(
        open_id=WHO, title="标题", project="D:/proj", chat_id=CHAT,
    )
    assert card_id == "7355439197428236291"
    call = tr.business_calls[-1]
    assert call["url"].endswith("/open-apis/cardkit/v1/cards")


def test_send_progress_card_rejects_empty_open_id():
    """没有收件人必须当场拒 —— 不发一张不知道去哪的卡。"""
    tr = _Transport()
    sender = _make_sender(tr)
    with pytest.raises(ValueError):
        sender.send_progress_card(
            open_id="", title="t", project="p", chat_id=CHAT,
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
