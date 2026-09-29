"""给飞书发回复。**只用标准库** —— 取 token 和发消息都是普通 REST 调用。

刻意不用 SDK：这两步就是两个 HTTPS POST，用 ``urllib`` 完全够，
而省掉一份依赖是实打实的。第三方那份 ``lark_oapi`` 只在
:mod:`~freeagent.feishu.bridge` 里用来看门 —— 长连接握手、心跳、自动重连
自己手搓不划算。

传输层可注入（``transport``），所以测试全程离线，一次请求都不发。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Mapping, Sequence

from .config import FeishuConfig

__all__ = ["FeishuSender", "FeishuError", "urllib_transport", "Transport"]

#: ``(url, payload, headers, timeout) -> 响应体文本``
#: 与 ``DeepSeekProvider`` 同一个签名，方便同一套测试替身复用。
#: token 走 headers 而不是 payload —— payload 更容易被日志意外打印。
#: ``payload=None`` 表示发 **GET**（见 :func:`urllib_transport`）。
Transport = Callable[
    [str, "Mapping[str, object] | None", Mapping[str, str], float], str
]

#: 飞书 tenant_access_token 有效期 2 小时。留 10 分钟余量，
#: 免得边界上发出一个「已过期」的请求再失败一次。
_TOKEN_TTL = 7200
_TOKEN_MARGIN = 600

#: 单条消息最大长度。飞书超长会直接拒（错误码 230002 之类），
#: 与其收一个看不懂的错，不如自己截并说明。
MAX_TEXT = 4000


class FeishuError(RuntimeError):
    """调用飞书接口失败。**不含任何凭据**。"""


def urllib_transport(
    url: str,
    payload: Mapping[str, object] | None,
    headers: Mapping[str, str],
    timeout: float,
) -> str:
    """标准库 HTTP。``payload=None`` 发 GET，否则发 POST。出错时抛
    :class:`FeishuError`，**不含 token**。

    刻意用「payload 为空」表达 GET，而不是给 Transport 加一个 ``method``
    参数 —— 那个签名是与 ``DeepSeekProvider`` 共享的，加参数会波及
    每一个测试替身。
    """
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", **dict(headers)},
        method="GET" if body is None else "POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        # 只留状态码：错误体里可能带敏感内容，不往异常里塞。
        raise FeishuError(f"飞书接口 HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise FeishuError(f"连不上飞书：{exc.reason}") from exc


class FeishuSender:
    """发消息，带 token 缓存。线程安全。

    为什么要缓存：``tenant_access_token`` 每次现取会白白多一次请求，
    而且飞书对取 token 有频率限制。缓存用锁保护 —— 提醒定时推送和消息
    队列可能同时发。
    """

    def __init__(
        self,
        config: FeishuConfig,
        *,
        transport: Transport | None = None,
        timeout: float = 15.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.timeout = timeout
        self._transport = transport or urllib_transport
        self._clock = clock
        self._token: str = ""
        self._expires_at: float = 0.0
        self._lock = threading.Lock()

    # -- token -------------------------------------------------------------- #
    def _fetch_token(self) -> str:
        raw = self._transport(
            f"{self.config.base_url}/open-apis/auth/v3/tenant_access_token/internal",
            {
                "app_id": self.config.app_id,
                # 唯一该拿原文的地方：要把它发去换 token。
                "app_secret": self.config.app_secret.use(),
            },
            {},
            self.timeout,
        )
        body = _require_ok(raw, "取 token")
        # 坑：``tenant_access_token`` 在**响应顶层**，不在 ``data`` 里。
        # 飞书 auth 接口的形状是
        # ``{"code":0,"msg":"ok","tenant_access_token":"t-…","expire":7200}``。
        # 之前这里只取 ``data``，于是永远拿不到 token —— 而报错是
        # 「响应里没有 tenant_access_token」，完全指不到真正的原因。
        token = body.get("tenant_access_token")
        if not isinstance(token, str) or not token:
            raise FeishuError("响应里没有 tenant_access_token")
        return token

    def token(self) -> str:
        with self._lock:
            if self._token and self._clock() < self._expires_at:
                return self._token
            token = self._fetch_token()
            self._token = token
            self._expires_at = self._clock() + _TOKEN_TTL - _TOKEN_MARGIN
            return token

    # -- 带鉴权的 GET -------------------------------------------------------- #
    def get_json(self, path: str) -> dict[str, object]:
        """带 token 的 GET，返回校验过的**完整**响应体。

        给 :mod:`~freeagent.feishu.identity` 之类「同一次连接里还要再问一句」
        的用途用，所以复用同一份 token 缓存，不额外换一次 token。
        """
        raw = self._transport(
            f"{self.config.base_url}{path}",
            None,
            {"Authorization": f"Bearer {self.token()}"},
            self.timeout,
        )
        return _require_ok(raw, f"GET {path}")

    # -- 发消息 ------------------------------------------------------------- #
    def send_text(self, chat_id: str, text: str) -> None:
        """发到**会话**。``chat_id`` 是 ``oc_`` 开头的那种。"""
        self._send(chat_id, text, id_type="chat_id")

    def send_to_user(self, open_id: str, text: str) -> None:
        """发到**某人私聊**。

        踩过的坑：提醒推送原本直接调 ``send_text(open_id, …)``，而那个方法
        把 ``receive_id_type`` 写死成 ``chat_id``。可飞书的 ``open_id`` 和
        私聊会话 ``chat_id`` **是两个不同的东西**（``ou_`` vs ``oc_``）——
        这么发必然被接口拒掉，也就是说定时提醒在真实环境是**坏的**，
        而离线测试全绿：FakeSender 只记录传了什么，压根不校验 id 类型。
        """
        self._send(open_id, text, id_type="open_id")

    @staticmethod
    def _receive_id_type(entry: str) -> str:
        """按**形状**判断该用哪种 ``receive_id_type``。

        踩过的坑（实测）：白名单接受两种标识 —— ``ou_`` 开头的 ``open_id``
        和租户级裸 ``user_id``（形如 ``2d1b7bec``）。我原先在
        ``send_approval_card`` 里把 ``receive_id_type`` 写死成 ``open_id``，
        于是取到 user_id 时飞书回 **HTTP 400** —— 表现是「确认卡发不出去」，
        而根因只是 id 形态不匹配。

        复用 :meth:`send_to_allowlist_entry` 那套判断，不另写一份 ——
        抄一份就会漂移，而漂移的表现正是「有的路径发得出、有的发不出」。
        """
        entry = (entry or "").strip()
        if entry.startswith("ou_"):
            return "open_id"
        if entry.startswith("oc_"):
            raise ValueError(
                f"{entry} 看起来是会话 id（oc_ 开头），不是人的 id，"
                "发不出确认卡。"
            )
        return "user_id"

    def send_approval_card(
        self,
        *,
        open_id: str,
        subject: str,
        detail: str,
        credential: str,
        ttl_seconds: int,
    ) -> str:
        """发一张**确认卡**，返回 ``message_id``。

        卡片形状是**实测**出来的（见 docs 11.9.4）：按钮 ``value`` 里放
        ``{"action": ..., "id": 凭据}``，飞书会把它**原样**带回
        ``card.action.trigger`` 事件 —— 这一点前期验证过，``value`` 里的
        嵌套对象不会丢。

        卡片上刻意写明三件事：做什么、影响哪个范围、这次授权多久。
        少任何一项，用户就只能凭「信任机器人」点按钮 —— 那不叫确认。

        ⚠️ ``msg_type`` 用 ``interactive`` 而不是老的 ``card.json``：
        后者是旧版格式，新应用发出去会被拒。
        """
        if not open_id:
            raise ValueError("发确认卡必须给收件人标识")
        id_type = self._receive_id_type(open_id)
        card = _approval_card(subject, detail, credential)
        raw = self._transport(
            f"{self.config.base_url}/open-apis/im/v1/messages"
            f"?receive_id_type={id_type}",
            {
                "receive_id": open_id,
                "msg_type": "interactive",
                # 坑：content 也要是 **JSON 字符串**，不是嵌套对象
                "content": json.dumps(card, ensure_ascii=False),
            },
            {"Authorization": f"Bearer {self.token()}"},
            self.timeout,
        )
        # 坑：传输层回来的是**双重编码的 JSON 字符串**，不是 dict。
        # 直接 raw.get("code") 永远是 None —— 会把成功读成失败。
        body = _decode_twice(raw)
        if body.get("code") != 0:
            # 把 id 形态一起报出来 —— HTTP 400 最常见的原因就是
            # receive_id_type 和值不匹配（实测踩过），只报「400」查不动。
            raise FeishuError(
                f"发确认卡失败：code={body.get('code')} msg={body.get('msg')}"
                f"（收件人={open_id!r} 形态={id_type}）"
            )
        return str(((body.get("data") or {}).get("message_id")) or "")

    def send_tool_card(
        self,
        *,
        open_id: str,
        subject: str,
        detail: str,
        credential: str,
        ttl_seconds: int,
    ) -> str:
        """发一张**执行期**确认卡，返回 ``message_id``。

        形状**刻意与 :meth:`send_approval_card` 完全一致**（同样的
        ``allow_once``/``deny`` 按钮、同样的 ``{"action", "id"}`` value、
        同样的 JSON 1.0 卡片结构）。因此桥接的 ``_card_action`` 处理它
        **一行都不用改** —— 审批的判定与落库只有一处，这是设计文档 11.9.4
        「一份能力一份实现」的直接收益。

        那为什么不干脆复用 ``send_approval_card``？因为它写死了
        「不点就算拒绝 —— 沉默不等于同意」这句 note，而执行期要说的是
        **「只批准这一个动作，下一个动作还会再问」**。同一张卡说两套话
        会让人以为「我允许了这次，后面就都行了」—— 而那恰恰不是事实
        （V1 的 ``always`` 是会话级，我们刻意不映射过去）。
        """
        if not open_id:
            raise ValueError("发执行期确认卡必须给收件人标识")
        id_type = self._receive_id_type(open_id)
        card = _tool_card(subject, detail, credential)
        raw = self._transport(
            f"{self.config.base_url}/open-apis/im/v1/messages"
            f"?receive_id_type={id_type}",
            {
                "receive_id": open_id,
                "msg_type": "interactive",
                "content": json.dumps(card, ensure_ascii=False),
            },
            {"Authorization": f"Bearer {self.token()}"},
            self.timeout,
        )
        body = _decode_twice(raw)
        if body.get("code") != 0:
            raise FeishuError(
                f"发执行期确认卡失败：code={body.get('code')} msg={body.get('msg')}"
                f"（收件人={open_id!r} 形态={id_type}）"
            )
        return str(((body.get("data") or {}).get("message_id")) or "")

    def send_to_allowlist_entry(self, entry: str, text: str) -> None:
        """按白名单条目的**形状**选 ``receive_id_type`` 发私聊。

        白名单现在接受两类标识（见 :meth:`ChannelService.is_allowed`）：
        ``ou_`` 开头的 ``open_id``，以及租户级 ``user_id``（裸串，形如
        ``2d1b7bec``）。飞书的 ``receive_id_type`` **必须**和值对得上，
        猜错就是「请求被拒」，而表现是「提醒永远送不到」。

        判据只能靠形状 —— 飞书不给接口反查「这个串是哪一种」。

        ⚠️ 踩过的坑（Hermes 上游也是这样）：``oc_`` 开头的 ``chat_id``
        **长得像 ID 但不是人的 ID**。用户顺手把会话 id 粘进白名单是很自然的
        （界面上「会话」就显示它），而它会被当成 user_id 发出去 → 被拒 →
        「提醒收不到」且看不出原因。所以这里显式拒绝并说明。
        """
        entry = (entry or "").strip()
        if entry.startswith("ou_"):
            self._send(entry, text, id_type="open_id")
        elif entry.startswith("oc_"):
            raise ValueError(
                f"{entry} 看起来是会话 id（oc_ 开头），不是人的 id。"
                "把 oc_ 填进白名单，提醒会发不出去。"
                "请填 ou_ 开头的 open_id，或租户级 user_id。"
            )
        else:
            self._send(entry, text, id_type="user_id")

    def _send(self, receive_id: str, text: str, *, id_type: str) -> None:
        """空文本直接跳过 —— 飞书会拒，而且没必要。"""
        text = (text or "").strip()
        if not text:
            return
        if len(text) > MAX_TEXT:
            text = text[:MAX_TEXT] + "\n…（内容过长已截断）"
        raw = self._transport(
            f"{self.config.base_url}/open-apis/im/v1/messages"
            f"?receive_id_type={id_type}",
            {
                "receive_id": receive_id,
                "msg_type": "text",
                # 坑：发消息时 content **也是** JSON 字符串，和收消息时对称。
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
            {"Authorization": f"Bearer {self.token()}"},
            self.timeout,
        )
        _require_ok(raw, "发消息")


def _approval_card(subject: str, detail: str, credential: str) -> dict[str, Any]:
    """待确认的卡片。**按钮**还在，可点。

    卡上刻意写明三件事：做什么、影响哪个范围、这次授权多久。
    少任何一项，用户就只能凭「信任机器人」点按钮 —— 那不叫确认。
    """
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": subject},
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": detail}},
            {"tag": "note", "elements": [{"tag": "plain_text", "content":
                  "不点就算拒绝 —— 沉默不等于同意。"}]},
            {"tag": "action", "actions": [
                {"tag": "button",
                 "text": {"tag": "plain_text", "content": "允许一次"},
                 "type": "primary",
                 "value": {"action": "allow_once", "id": credential}},
                {"tag": "button",
                 "text": {"tag": "plain_text", "content": "拒绝"},
                 "type": "danger",
                 "value": {"action": "deny", "id": credential}},
            ]},
        ],
    }


#: 按钮 value 里的 ``action``。与批准卡的 ``allow_once`` / ``deny`` 并列。
#:
#: ⚠️ **刻意的区别：这张卡没有凭据。** 批准卡要审批凭据是因为它授权的是
#: **有副作用**的动作（执行代码、访问目录）。而这里的按钮只切换**只读视图**
#: —— 没有副作用，因此**没有东西需要授权**，也就**不需要**落库等点击。
#:
#: 这一点很重要：一旦给只读选项也套上 pending 记录，就得给它 TTL、过期即拒、
#: 不可篡改……而那整套机制是为「授权」设计的，用在这里是**错配**，
#: 而且凭空多出「卡片过期了但用户还没点」这类失败模式。
VIEW_CHOICE_ACTION = "run_view"


def _tool_card(subject: str, detail: str, credential: str) -> dict[str, Any]:
    """执行期（agent 想动手）的确认卡。

    ⚠️ **必须是 JSON 1.0**（顶层 ``config``/``header``/``elements``），
    与 :func:`_approval_card` 发出去的**同一版本**。官方错误码 200830：
    2.0 卡不能更新成 1.0，反之亦然（实测踩过）。
    """
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": subject},
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": detail}},
            {"tag": "note", "elements": [{"tag": "plain_text", "content":
                  "只批准**这一个**动作 —— 下一个动作还会再问。"
                  "不点就算拒绝，沉默不等于同意。"}]},
            {"tag": "action", "actions": [
                {"tag": "button",
                 "text": {"tag": "plain_text", "content": "允许这一次"},
                 "type": "primary",
                 "value": {"action": "allow_once", "id": credential}},
                {"tag": "button",
                 "text": {"tag": "plain_text", "content": "拒绝"},
                 "type": "danger",
                 "value": {"action": "deny", "id": credential}},
            ]},
        ],
    }


def view_choice_card(
    subject: str, choices: Sequence[str], *, chat_id: str
) -> dict[str, Any]:
    """「我没把握，你要看哪个？」的选项卡。**无状态**。

    按钮的 ``value`` 直接带 ``view`` 与 ``chat``，点击即执行并回话 ——
    不查库、不等回复、不落任何东西。理由见 :data:`VIEW_CHOICE_ACTION`。

    刻意与批准卡**同一个版本**（JSON 1.0，顶层 ``elements``）：
    官方错误码 200830 规定 2.0 卡不能更新成 1.0，反之亦然。
    这里虽是首次发送、不是更新，但同一个 :func:`_card_action_response`
    家族会把它包进应答，所以版本必须与那一侧一致。
    """
    if not choices:
        raise ValueError("选项卡至少要一个选项")
    buttons = []
    for pick in choices:
        # 只带前 40 字：飞书按钮文案有长度上限，超了会被截成看不懂的样子。
        label = pick[:40]
        buttons.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": label},
                "type": "primary" if len(buttons) == 0 else "default",
                "value": {
                    "action": VIEW_CHOICE_ACTION,
                    "view": pick,
                    "chat": chat_id,
                },
            }
        )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": subject},
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md",
                                    "content": "点一个就行；都不是就直接说那件事。"}},
            {"tag": "action", "actions": buttons},
        ],
    }


def send_view_choice_card(
    sender: Any,
    *,
    open_id: str,
    subject: str,
    choices: Sequence[str],
    chat_id: str,
) -> str:
    """发选项卡，返回 message_id。

    ⚠️ ``sender`` 是**一个 :class:`FeishuSender` 实例**，不是模块 ——
    踩过的坑：照着 ``send_approval_card`` 的样子写成模块级函数、还
    ``from .feishu.sender import send_approval_card``，而那个名字
    **是 sender 上的方法、模块里根本没有** ⇒ ``ImportError`` 发生在真正的
    发卡之前。与 11.9.4 复用**同一套发卡实现**，不新造第二个接口。
    """
    if not open_id:
        raise ValueError("发选项卡必须给收件人标识")
    id_type = sender._receive_id_type(open_id)
    card = view_choice_card(subject, choices, chat_id=chat_id)
    raw = sender._transport(
        f"{sender.config.base_url}/open-apis/im/v1/messages"
        f"?receive_id_type={id_type}",
        {
            "receive_id": open_id,
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False),
        },
        {"Authorization": f"Bearer {sender.token()}"},
        sender.timeout,
    )
    body = _decode_twice(raw)
    if body.get("code") != 0:
        raise FeishuError(
            f"发选项卡失败：code={body.get('code')} msg={body.get('msg')}"
            f"（收件人={open_id!r} 形态={id_type}）"
        )
    return str(((body.get("data") or {}).get("message_id")) or "")


def _decided_card(subject: str, line: str, *, granted: bool) -> dict[str, Any]:
    """**已处理完**的卡片：绿头/红头，**且没有按钮**。

    为什么必须去掉按钮：留着可点的按钮等于骗人 —— 用户会以为还能再选，
    而点第二下在旧实现里真的会改写决策（已修，见 ApprovalStore.resolve）。
    「不能点」比「点了告诉你不行」更诚实。

    ``granted`` **显式传**，不从 ``line`` 里嗅。
    踩过的坑：第一版写 ``line.startswith("已允许")`` 判绿，而正文是
    ``**已允许**（…）`` —— 开头是 ``*``，于是**每张允许的卡都渲染成红色**。
    从渲染好的文案里反推语义，格式一改就悄悄错，且测试只查文案查不出来。

    ⚠️ **必须是 1.0 结构**（顶层 ``config``/``header``/``elements``），
    与 :func:`_approval_card` 发出去的**同一版本**。官方错误码 200830：
    2.0 卡不能更新成 1.0，反之亦然。我这里一度写成 2.0
    （``schema`` + ``body.elements``），而发出去的是 1.0 —— 版本对不上，
    飞书整条应答判失败，用户看到「出错了」。
    """
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "green" if granted else "red",
            "title": {"tag": "plain_text", "content": subject},
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": line}},
        ],
    }


def _card_action_response(
    card: dict[str, Any], toast_type: str, toast_text: str
) -> dict[str, Any]:
    """卡片 action 的应答体。

    形状由**飞书文档**定死（不是 SDK 的模型 —— ``CallBackCard._types``
    确实只有 ``{type, data}``，与文档一致）::

        {"toast": {...}, "card": {"type": "raw", "data": {<卡片 JSON>}}}

    ``card.data`` 就是「就地替换这张卡」，所以**不需要额外调一次更新消息的
    API**，也不需要那个权限。这是常规做法。

    ⚠️ **踩过的坑（实测）**：我原先直接返回**裸卡片**
    ``{config, header, elements}`` —— 那是**发送**卡片的形状，不是**应答**
    的形状。用户点一下就看到「出错了」，而我们侧一切正常（handler 跑完、
    记了日志、回了一个 1226 字节的应答）。飞书那边收到一个没有 ``type``
    的 ``card``，整条应答判失败。

    两条独立的错都在这里（对照官方「卡片回传交互」文档）：

    1. **少 ``{type, data}`` 包装** —— 文档：``card.type`` 必填，
       选 JSON 响应时必须是 ``raw``；``card.data`` 才是卡片 JSON。
    2. **版本必须与发出的卡一致**（错误码 200830：2.0 卡不能更新成 1.0，
       反之亦然）。我们**发出的是 1.0**（``config.wide_screen_mode`` +
       顶层 ``elements``），所以 ``data`` 里也必须是 1.0 结构。
    """
    return {
        "toast": {"type": toast_type, "content": toast_text},
        "card": {"type": "raw", "data": card},
    }


def _decode_twice(raw: object) -> dict[str, Any]:
    """把传输层的返回值解成 dict。**最多解两层。**

    踩过的坑（实测）：发卡片时这里回来的是**双重编码的 JSON 字符串**。
    我先前写 ``raw.get("code")`` —— 永远 ``None`` —— 于是把
    ``code=0 msg=success`` 判成「卡片发不出去」，而真实 message_id 就在
    同一份输出里。**证据在，判读错了。**
    """
    value = raw
    for _ in range(2):
        if isinstance(value, (bytes, bytearray)):
            value = value.decode("utf-8", errors="replace")
        if not isinstance(value, str):
            break
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            break
    return value if isinstance(value, dict) else {}


def _require_ok(raw: str, what: str) -> dict[str, object]:
    """校验飞书响应，**返回完整响应体**。

    返回完整体而不是只返回 ``data``：飞书不同接口的字段位置并不统一 ——
    ``tenant_access_token`` 在顶层，而消息接口的 ``message_id`` 在 ``data`` 里。
    只返回 ``data`` 会让顶层字段凭空消失，且报错完全指不到原因。

    坑：飞书**HTTP 200 不代表成功**。业务码在 body 的 ``code`` 里，
    非 0 才是失败。只看 HTTP 状态码会把「权限不足」当成成功，
    然后表现为「bot 什么都不回」——极难排查。
    """
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise FeishuError(f"{what}：响应不是 JSON（{exc}）") from exc
    if not isinstance(parsed, dict):
        raise FeishuError(f"{what}：响应不是对象")
    code = parsed.get("code", 0)
    if code != 0:
        msg = str(parsed.get("msg") or "（无 msg）")
        raise FeishuError(f"{what}失败：code={code} msg={msg}")
    return parsed
