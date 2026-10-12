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
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol, Sequence

from .config import FeishuConfig

__all__ = [
    "FeishuSender", "FeishuError", "urllib_transport", "Transport",
    "ProgressCard",
]

#: ``(url, payload, headers, timeout, method="POST") -> 响应体文本``
#: 与 ``DeepSeekProvider`` 同一个签名，方便同一套测试替身复用。
#: token 走 headers 而不是 payload —— payload 更容易被日志意外打印。
#: ``payload=None`` 表示发 **GET**（见 :func:`urllib_transport`）。
#:
#: ``method`` 是 CardKit 加上去的：它要 PUT/PATCH，而「payload 空不空」
#: 只能表达 GET/POST。
#:
#: **刻意用 Protocol 而不是 ``Callable[...]``**：``Callable`` 没法表达
#: 「最后一个参数有默认值」，而 ``method`` **就是**有默认值
#: （``urllib_transport`` 里写着 ``method: str = "POST"``）。写成
#: ``Callable`` 且把 5 个参数都标成必填的话，每一个 4 参数的老调用点
#: 都会被报「还差 1 个位置参数」—— 而那些调用点传 4 个**完全正确**。
#: 报这种错只会逼人加 ``# type: ignore``，而本项目禁止那玩意。
class Transport(Protocol):
    """传输层的契约。``method`` 可省，默认 POST。"""

    def __call__(
        self,
        url: str,
        payload: Mapping[str, object] | None,
        headers: Mapping[str, str],
        timeout: float,
        method: str = "POST",
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class ProgressCard:
    """一张已投递的进度卡。**两个 id 各管一件事，缺一不可。**

    - ``card_id`` — CardKit **卡片实体** id。流式更新按它定位组件
      （``PUT /cards/{card_id}/elements/{element_id}/content``）。
    - ``message_id`` — 这张卡在**聊天里**那条消息的 id。授权卡要挂到它
      下面靠它（``POST /messages/{message_id}/reply``）。

    ## 为什么不能只留一个、只返回 ``card_id``

    一开始 :meth:`FeishuSender.send_progress_card` 只返回 ``card_id``，
    而「授权卡聚拢到进度卡下」需要的是 ``message_id``。两个都是 ``str``，
    **类型上完全分不出来** —— 传错了不会报错，只会让授权卡挂错地方或者
    挂不上，而那个症状（「授权卡没聚拢」）看起来像功能没做，不像 bug。

    用 :class:`ProgressCard` 把两者绑在一起，让「拿错」变成「属性不存在」。
    """

    card_id: str
    message_id: str


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
    method: str = "POST",
) -> str:
    """标准库 HTTP。``payload=None`` 发 GET，否则按 ``method`` 发。出错时抛
    :class:`FeishuError`，**不含 token**。

    刻意用「payload 为空」表达 GET，而不是给 Transport 加一个 ``method``
    参数 —— 那个签名是与 ``DeepSeekProvider`` 共享的，加参数会波及
    每一个测试替身。

    ⚠️ 上面那句是**旧注释，已不成立**：CardKit 需要 PUT/PATCH，所以
    ``method`` 最终还是加上了。保留「payload 为空 = GET」的旧约定只是
    为了既有调用点不用改；新代码一律显式传 ``method``。
    """
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", **dict(headers)},
        method="GET" if body is None else method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        # **保留 code / msg，丢掉响应体其余部分**（真机踩过，见下）。
        #
        # 原先只留状态码，于是 CardKit 创建卡片失败时报的是
        # 「飞书接口 HTTP 400」—— 而飞书的 HTTP 400 **必然**带一个
        # JSON 体说明原因（错误码 9999… 权限不足、200830 卡片结构不合法、
        # 参数校验失败……）。把它扔掉等于把唯一有用的线索扔掉，
        # 剩下的「400」查不动：既分不清是没加 CardKit 权限，
        # 还是卡片 JSON 写错了，更不知道飞书把哪个字段当非法。
        #
        # 为什么只挑 code/msg 而不整段带出来：响应体其它字段可能回显
        # 提交内容。这里要的是**可诊断**，不是把请求内容倒进日志。
        #
        # 仍然**绝不带 token**：token 只在请求头里，不在响应体里。
        try:
            raw_error = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - 读不到错误体也要报 HTTP 码
            raw_error = ""
        detail = ""
        try:
            parsed = json.loads(raw_error)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict) and parsed.get("code") is not None:
            detail = f" code={parsed.get('code')} msg={parsed.get('msg')}"
        raise FeishuError(f"飞书接口 HTTP {exc.code}{detail}") from exc
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

    def _cardkit(
        self, path: str, payload: Mapping[str, object], method: str, what: str,
    ) -> dict[str, object]:
        """CardKit（PUT / PATCH）请求。**走注入的 transport**。

        刻意不复用 :meth:`_transport`：那个签名是
        ``(url, payload, headers, timeout)``、用「payload 空不空」区分
        GET/POST，而 CardKit 要 PUT/PATCH。所以 method 单独一个参数，
        由这里显式传。

        仍然走 ``self._transport`` 而不是另起 urllib —— **测试全程离线**
        这条性质对本类同样成立（离线测试里真发一次网络请求，会以
        HTTP 400 的形式失败，而那报的是飞书的错、不是我们的错）。
        """
        raw = self._transport(
            f"{self.config.base_url}{path}",
            payload,
            {"Authorization": f"Bearer {self.token()}"},
            self.timeout,
            method=method,
        )
        body = _decode_twice(raw)
        if body.get("code") != 0:
            raise FeishuError(
                f"{what}失败：code={body.get('code')} msg={body.get('msg')}"
            )
        return body

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
        reply_to: str = "",
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

        ## ``reply_to``：把授权卡挂到进度卡下面（设计文档 11.16）

        给了 ``message_id`` 就走 ``POST /messages/{id}/reply``，于是
        一次委派的全部授权卡聚成**一张进度卡下面的一条回复串**。

        为什么值得这么做 —— 一次委派会产生 N+1 张卡（N 次授权 + 1 张进度），
        而聊天流是**按发送时刻固定位置**的：进度卡必须最早发（否则没法流式
        更新），于是它必然被 N 张后发的授权卡压在下面。翻上去才能看到
        「这次委派到哪了」。

        聚拢之后聊天流里只多 1 张卡，其余在它的回复里，**天然按顺序、
        天然不被别的东西盖住**，而进度卡原地更新、位置不动。

        **空串时保持原路径**（``send`` 到会话）—— 没有进度卡可挂的场景
        （没配 ``progress_sender``、发卡失败）必须还能正常发授权卡。
        闸门不许因为「想好看」而失效。
        """
        if not open_id:
            raise ValueError("发执行期确认卡必须给收件人标识")
        card = _tool_card(subject, detail, credential)
        url, payload, id_type = self._card_delivery(
            open_id=open_id, content=card, reply_to=reply_to,
        )
        raw = self._transport(
            url, payload,
            {"Authorization": f"Bearer {self.token()}"},
            self.timeout,
        )
        body = _decode_twice(raw)
        if body.get("code") != 0:
            # 说清是哪种发法失败了 —— 两者的排查方向不同：reply 路径
            # 失败通常是锚点那条消息被撤回了。
            how = "回复到进度卡" if reply_to else "发到会话"
            raise FeishuError(
                f"{how}执行期确认卡失败：code={body.get('code')} "
                f"msg={body.get('msg')}"
                + (f"（锚点={reply_to!r}）" if reply_to
                   else f"（收件人={open_id!r} 形态={id_type}）")
            )
        return str(((body.get("data") or {}).get("message_id")) or "")

    def send_question_card(
        self,
        *,
        open_id: str,
        subject: str,
        detail: str,
        credential: str,
        ttl_seconds: int,
        reply_to: str = "",
    ) -> str:
        """发一张**提问卡**（agent 在问你），返回 ``message_id``。

        ``reply_to`` 与 :meth:`send_tool_card` 同义：挂到进度卡下面聚拢。
        这里是**同一段逻辑的第二处复制**，不是遗漏 —— 提问卡与授权卡形状
        不同（这个没有按钮），但「回复还是新发」的判定完全一致。

        判定抽成 :meth:`_card_delivery`，因为它必须**在两处完全一致**：
        复制两份的话，改了一处就会出现「授权卡聚拢了、提问卡没有」，
        而那看起来像功能半成品，不像 bug。

        与前两张卡的关键差别：**没有按钮**。

        因为提问的答复是**打字**，不是点钮。而且若把
        ``allow_once`` 挂在提问卡上，点一下就会调 ``resolve()`` 落到
        一行 **question** 记录上 —— 那是答复不是得出的判定。

        因此这张卡的唯一职责是**告诉人问了什么**；答案走普通文本消息，
        由桥接路由到 ``put_answer``。

        卡上**必须写出退出通道**（下面那个 note）。只说「回答这个」
        而不说「不回答也行」的卡，就是一条死路 —— agent 等着，TTL 走完，
        而人根本不知道自己可以拒绝。
        """
        if not open_id and not reply_to:
            raise ValueError("发提问卡必须给收件人标识")
        card = _question_card(subject, detail, ttl_seconds)
        url, payload, id_type = self._card_delivery(
            open_id=open_id, content=card, reply_to=reply_to,
        )
        raw = self._transport(
            url, payload,
            {"Authorization": f"Bearer {self.token()}"},
            self.timeout,
        )
        body = _decode_twice(raw)
        if body.get("code") != 0:
            tail = (f"（锚点={reply_to!r}）" if reply_to
                    else f"（收件人={open_id!r} 形态={id_type}）")
            raise FeishuError(
                f"发提问卡失败：code={body.get('code')} "
                f"msg={body.get('msg')}{tail}"
            )
        return str(((body.get("data") or {}).get("message_id")) or "")

    # -- CardKit 2.0 流式进度卡片（设计文档 11.16） ---------------------- #
    #
    # 为什么用 CardKit 2.0 而不是 PUT 更新已发消息：
    #   PUT /im/v1/messages/{id} 是全量替换，内容瞬间变化，没有流式效果。
    #   CardKit 2.0 是飞书原生流式卡片协议，支持"打字机"效果，
    #   且频率限制更宽松（50 次/秒 vs PUT 的 50 次/分钟）。
    #
    # 流式模式关键约束（飞书文档实测）：
    #   - sequence 必须严格递增
    #   - 流式模式下用户点击按钮会阻塞更新（需先关闭流式再处理回调）
    #   - 流式模式 10 分钟后自动关闭（建议手动关闭）
    #   - 卡片 JSON 必须 2.0 结构（schema + body.elements）

    def send_progress_card(
        self,
        *,
        open_id: str,
        title: str,
        project: str,
        chat_id: str,
    ) -> "ProgressCard":
        """发一张 CardKit 2.0 流式进度卡片，返回 ``card_id``。

        ## 两步，缺一不可（真机踩过，2026-10-12）

        1. ``POST /cardkit/v1/cards`` 建**卡片实体** —— 注意这一步
           **不带任何收件人**，它只在飞书侧造出一个 card_id。
        2. ``POST /im/v1/messages`` 以 ``{"type":"card","data":{"card_id":…}}``
           把这张卡**发进会话**。

        我原先只做了第 1 步就返回 card_id，而调用方把它记成
        「已发进度卡片」—— 卡片却从未出现在任何会话里。真机表现为
        「权限卡正常、进度卡一条也没有」，而日志干净得看不出问题：
        没报错，只是那张卡根本不存在于聊天中。

        官方原文：「后续可通过**发送消息接口**传入卡片实体 ID 发送卡片」，
        且「**一个卡片实体，仅支持发送一次**」—— 所以实体只建一次、
        只发一次，之后全程靠 card_id 做流式更新。

        :param chat_id: 有值就发进会话；为空则退化为私聊发给 ``open_id``
            （Web 建的委派没有飞书会话，进度卡得有个落点）。
        """
        if not open_id and not chat_id:
            raise ValueError("发进度卡片必须给收件人（chat_id 或 open_id）")
        card = _progress_card(title, project)
        raw = self._transport(
            f"{self.config.base_url}/open-apis/cardkit/v1/cards",
            {
                "type": "card_json",
                "data": json.dumps(card, ensure_ascii=False),
                "uuid": str(uuid.uuid4()),
                "sequence": 1,
            },
            {"Authorization": f"Bearer {self.token()}"},
            self.timeout,
        )
        body = _require_ok(raw, "创建进度卡片")
        # ``body.get("data")`` 的类型是 ``object``，不能直接 ``.get()``。
        # **窄化成 dict 再取** —— 而不是 ``(body.get("data") or {}).get(...)``：
        # 那种写法在 ``data`` 为 ``None`` 时靠运行期兜底，检查器却看到
        # ``object`` 没有 ``get``，两条路都别扭。
        data = body.get("data")
        card_id = str(data.get("card_id") or "") if isinstance(data, dict) else ""
        if not card_id:
            raise FeishuError("创建进度卡片响应里没有 card_id")

        # 第 2 步：真正把它送出去。
        if chat_id:
            message_id = self._send_card_to_chat(chat_id, card_id)
        else:
            message_id = self._send_card_to_user(
                self._receive_id_type(open_id), open_id, card_id,
            )
        return ProgressCard(card_id=card_id, message_id=message_id)

    def _send_card_to_chat(self, chat_id: str, card_id: str) -> str:
        """把卡片实体发进会话，返回 ``message_id``。

        ``content`` 是 **JSON 字符串**（和本模块其它发消息一致），里面装的是
        卡片引用而不是卡片内容 —— 卡片内容由 card_id 指回 CardKit，
        这样后续的流式更新才会作用在**同一条消息**上。
        """
        return self._post_card_message(chat_id, "chat_id", card_id)

    def _send_card_to_user(self, id_type: str, open_id: str, card_id: str) -> str:
        """发私聊。Web 建的委派没有会话，退化走这里。"""
        return self._post_card_message(open_id, id_type, card_id)

    def _card_delivery(
        self, *, open_id: str, content: dict[str, Any], reply_to: str,
    ) -> tuple[str, dict[str, object], str]:
        """算出一张卡的**投递目标**：``(url, payload, id_type)``。

        ## 为什么抽出来

        授权卡与提问卡都要「挂到进度卡下」或「发到会话」这个二选一。
        复制两份的话，改了一处就会出现「授权卡聚拢了、提问卡没有」——
        而那看起来像功能半成品，不像 bug，所以很难被发现。

        ## 端点差在哪

        - **reply**：``POST /messages/{message_id}/reply``。**不带
          ``receive_id_type``** —— 收件人由被回复的那条消息决定，
          带着它飞书会拒。``reply_in_thread: False`` 是因为私聊没有话题，
          true 会把被回复的那条也拖进话题串。
        - **send**：``POST /messages?receive_id_type=…``，收件人在
          ``receive_id`` 里。

        ``id_type`` 只在 send 路径有意义；reply 路径返回空串 —— 它**只用于
        报错文案**，而那一支根本不会走到（reply 失败报的是锚点）。
        """
        if reply_to:
            return (
                f"{self.config.base_url}/open-apis/im/v1/messages"
                f"/{reply_to}/reply",
                {
                    "msg_type": "interactive",
                    "content": json.dumps(content, ensure_ascii=False),
                    "reply_in_thread": False,
                },
                "",
            )
        id_type = self._receive_id_type(open_id)
        return (
            f"{self.config.base_url}/open-apis/im/v1/messages"
            f"?receive_id_type={id_type}",
            {
                "receive_id": open_id,
                "msg_type": "interactive",
                "content": json.dumps(content, ensure_ascii=False),
            },
            id_type,
        )

    def _post_card_message(
        self, receive_id: str, id_type: str, card_id: str,
    ) -> str:
        raw = self._transport(
            f"{self.config.base_url}/open-apis/im/v1/messages"
            f"?receive_id_type={id_type}",
            {
                "receive_id": receive_id,
                "msg_type": "interactive",
                "content": json.dumps(
                    {"type": "card", "data": {"card_id": card_id}},
                    ensure_ascii=False,
                ),
            },
            {"Authorization": f"Bearer {self.token()}"},
            self.timeout,
        )
        body = _decode_twice(raw)
        if body.get("code") != 0:
            raise FeishuError(
                f"发送进度卡片失败：code={body.get('code')} "
                f"msg={body.get('msg')}（收件人={receive_id!r} 形态={id_type}）"
            )
        return str(((body.get("data") or {}).get("message_id")) or "")

    def update_progress_text(
        self,
        *,
        card_id: str,
        text: str,
        sequence: int,
    ) -> None:
        """流式更新进度卡片的文本内容。

        ``sequence`` 必须严格递增 —— 飞书用它保证多次更新的时序性。
        调用方负责维护这个计数器（从 1 开始，每次 +1）。

        频率限制 50 次/秒。调用方负责节流，不要每个事件都调。
        """
        raw = self._cardkit(
            f"/open-apis/cardkit/v1/cards"
            f"/{card_id}/elements/progress_text/content",
            {
                "uuid": str(uuid.uuid4()),
                "content": text,
                "sequence": sequence,
            },
            method="PUT",
            what="流式更新进度文本",
        )

    def close_progress_stream(
        self,
        *,
        card_id: str,
        final_text: str,
        sequence: int,
    ) -> None:
        """设最终内容并关闭流式模式。**顺序是真机定的，颠倒不行。**

        ## 顺序：先 PUT 内容，再 PATCH 关流式

        1. 先 PUT 设最终内容 —— **此时流式还开着**。
        2. 再 PATCH 关 ``streaming_mode``。

        ## 为什么不能反过来（真机踩过，2026-10-12）

        我原先按「先关流式、再设内容」写，理由是「关掉之后 PUT 就是普通
        更新」。真机一跑就报：

            code=300309  ErrMsg: streaming mode is closed

        也就是说 ``PATCH /settings`` 把流式关掉之后，**元素内容接口
        就不再受理了** —— `elements/{id}/content` 只在流式期间可用。
        所以关流式必须放在**最后**一步：它是收尾，不是前置。

        ## 为什么不干脆不关

        流式模式 10 分钟后会被飞书自动关闭，而期间**用户点按钮会被阻塞**
        —— 进度卡上虽然不放按钮，但它占着流式通道。主动关掉是确定的
        收尾，省得把卡片状态交给一个 10 分钟后的定时器。
        """
        # 1. 设最终内容（流式还开着，所以这个 PUT 有效）
        self.update_progress_text(
            card_id=card_id, text=final_text, sequence=sequence
        )
        # 2. 关流式（必须最后 —— 关掉之后元素内容接口就不再受理）
        self._cardkit(
            f"/open-apis/cardkit/v1/cards/{card_id}/settings",
            {
                "settings": json.dumps(
                    {"config": {"streaming_mode": False}}, ensure_ascii=False
                ),
                "uuid": str(uuid.uuid4()),
                # 接在第 1 步那个 PUT 之后 —— sequence 严格递增。
                "sequence": sequence + 1,
            },
            method="PATCH",
            what="关闭进度卡片流式模式",
        )

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


def _progress_card(title: str, project: str) -> dict[str, Any]:
    """CardKit 2.0 流式进度卡片。

    **必须是 JSON 2.0 结构**（``schema: "2.0"`` + ``body.elements``），
    且 ``streaming_mode: true`` 必须在创建时设置 —— 否则后续流式更新
    会报 ``300309 Card streaming closed``。

    卡片只有一个 ``markdown`` 组件（``element_id = "progress_text"``），
    后续通过 :meth:`FeishuSender.update_progress_text` 流式更新它的内容。

    ``streaming_config`` 控制打字机效果的速度：
    - ``print_frequency_ms: 70`` — 每 70ms 渲染一帧
    - ``print_step: 1`` — 每帧输出 1 个字符
    - ``print_strategy: "fast"`` — 快速策略

    这些值是飞书文档推荐的默认值，实测效果流畅。

    ## ``update_multi: True`` 不能省（真机踩过，2026-10-12）

    漏掉它时卡片**建得出来、也投递出去了**，但收卡人看不到内容 ——
    现象是「飞书只有权限审批、没有进展」，而权限卡走普通消息路径不受影响。

    官方对建卡接口的使用限制写得很直接：

        「不支持将卡片设置为**独享卡片模式**。即不支持将卡片 JSON 数据中的
         ``update_multi`` 属性设置为 ``false``。」

    ``update_multi: false`` 就是独享卡片模式。**不写这个字段时飞书按
    独享处理**，于是卡片进了只有签发方可见的状态。

    三份独立的参考实现（HKUDS/nanobot、Vibe-Trading、CowAgent）在建卡时
    都显式带 ``update_multi: True`` + ``wide_screen_mode: True`` ——
    不是巧合，是这个字段必须显式给。官方文档没有把「省略等价于 false」
    写在明面上，所以**省略是危险的默认值**，不能靠试。

    ``wide_screen_mode`` 同理：进度卡是要给人盯着的，宽屏比窄屏可读。
    """
    return {
        "schema": "2.0",
        "header": {
            "title": {"tag": "plain_text", "content": title},
        },
        "config": {
            # **必须显式为 True**（独享卡片模式 = 只有签发方看得见）。
            "update_multi": True,
            "wide_screen_mode": True,
            "streaming_mode": True,
            "summary": {"content": f"{title}（进行中）"},
            "streaming_config": {
                "print_frequency_ms": {
                    "default": 70, "android": 70, "ios": 70, "pc": 70,
                },
                "print_step": {
                    "default": 1, "android": 1, "ios": 1, "pc": 1,
                },
                "print_strategy": "fast",
            },
        },
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "element_id": "progress_text",
                    "content": f"**项目**：{project}\n\n正在启动...",
                },
            ],
        },
    }


def _question_card(subject: str, detail: str, ttl_seconds: int) -> dict[str, Any]:
    """agent 的提问卡。**故意不包含 ``action`` 元素**。

    为什么不包含：可点的卡在飞书里会被人当成可执行的动作，
    而这里没有“批准答案”这个动作。挂一个按钮存在即使它没有意义。

    额外说明：``ttl_seconds`` 只用于把“你有多少时间”写在卡上。
    它不决定任何判定（判定在 ``ApprovalStore``）——但必须在卡上说出来，
    否则用户会以为不限时。
    """
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "turquoise",
            "title": {"tag": "plain_text", "content": subject},
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": detail}},
            {"tag": "note", "elements": [{"tag": "plain_text", "content":
                  "这不是确认卡，没有允许/拒绝按钮。"
                  f"直接回消息回答就行，{ttl_seconds}s 内不答按超时处理。"}]},
            {"tag": "note", "elements": [{"tag": "plain_text", "content":
                  "不想回答可以回 `/skip-question` 跳过。"}]},
        ],
    }


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
            # 三个按钮，**顺序与语义都要说清**（设计文档 12.7.2）：
            #   允许 / 拒绝 —— 只针对**这一个动作**，agent 还能换个方向继续
            #   停止这条委派 —— 中止**整条**，会话作废
            #
            # 「拒绝」与「停止」是两件事，混为一谈会让用户以为自己已经
            # 停掉了，而 agent 还在继续改代码 —— 那比没有这个按钮糟得多。
            # 所以第三个按钮**必须有**。
            {"tag": "action", "actions": [
                {"tag": "button",
                 "text": {"tag": "plain_text", "content": "允许这一次"},
                 "type": "primary",
                 "value": {"action": "allow_once", "id": credential}},
                {"tag": "button",
                 "text": {"tag": "plain_text", "content": "拒绝这一步"},
                 "type": "default",
                 "value": {"action": "deny", "id": credential}},
                {"tag": "button",
                 "text": {"tag": "plain_text", "content": "停止这条委派"},
                 "type": "danger",
                 "value": {"action": "stop_delegation", "id": credential}},
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


#: 菜单按钮的回传动作名。
#:

#: Plan 模式的**确认卡**（设计文档 12.7.2）。点它才会**真的执行**。
#:
#: 刻意与 :data:`VIEW_CHOICE_ACTION` 分开：那个是**只读视图**，点了跑个查询；
#: 这个点了**建事务 / 发起委派**。两者混用的话，「点一下会不会动手」就说不清了
#: —— 而这正是 12.7.2 要定的核心问题。
PLAN_CONFIRM_ACTION = "plan_confirm"


def plan_confirm_card(
    subject: str, plan_lines: Sequence[str], *, chat_id: str
) -> dict[str, Any]:
    """Plan → Build 的确认卡。**无状态**（与选项卡同一思路）。

    按钮的 ``value`` 只带 ``choice``（``ok`` / ``cancel``）与 ``chat``，
    **不带计划内容** —— 计划在 :class:`Repl` 的 ``_plan`` 里。刻意如此：
    把内容塞进按钮 payload 就等于让卡片成为第二份真源，而它会被转发、
    被缓存、会在会话淘汰后与真正的那份不一致。

    「方案」也印在卡面上，因为 12.7.2 要求**用户能核对**再批准；而点击时
    重新读 :attr:`Repl._plan`，**不**信卡面 —— 卡面只是给人看的。
    """
    if not plan_lines:
        raise ValueError("确认卡至少要一条计划内容")
    body = "\n".join(f"{i}. {line}" for i, line in enumerate(plan_lines, 1))
    def _btn(label: str, choice: str, primary: bool) -> dict[str, Any]:
        return {
            "tag": "button",
            "text": {"tag": "plain_text", "content": label},
            "type": "primary" if primary else "default",
            "value": {"action": PLAN_CONFIRM_ACTION, "choice": choice,
                      "chat": chat_id},
        }

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": subject},
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md",
                                    "content": "确认后才会动手："}},
            {"tag": "div", "text": {"tag": "lark_md", "content": body}},
            {"tag": "action", "actions": [
                _btn("确认执行", "ok", True),
                _btn("再想想", "cancel", False),
            ]},
        ],
    }


def send_plan_confirm_card(
    sender: Any,
    *,
    open_id: str,
    subject: str,
    plan_lines: Sequence[str],
    chat_id: str,
) -> str:
    """发确认卡，返回 message_id。

    与 :func:`send_view_choice_card` **同一套发卡实现**（同版本 JSON 1.0、
    同错误处理），不新造第二个接口 —— 踩过的坑：照着批准卡写成模块级函数、
    ``from .sender import send_approval_card``，而那个名字**是 sender 上的
    方法**，于是 ``ImportError`` 发生在真正的发卡之前。
    """
    if not open_id:
        raise ValueError("发确认卡必须给收件人标识")
    id_type = sender._receive_id_type(open_id)
    card = plan_confirm_card(subject, plan_lines, chat_id=chat_id)
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
            f"发确认卡失败：code={body.get('code')} msg={body.get('msg')}"
            f"（收件人={open_id!r} 形态={id_type}）"
        )
    return str(((body.get("data") or {}).get("message_id")) or "")


#: 卡片按钮回传的**命令名**（设计文档 11.9.8「卡片按钮 → 合成命令」）。
#:
#: ⚠️ **载荷里刻意不带 JSON**：``Repl._command`` 是 ``line.split()`` 之后
#: 取第一段当命令名，JSON 里的空格会把一条命令切成好几段 —— 症状是
#: 「点了没反应」，而日志里什么错都没有。所以这里只带**两个短字段**
#: （``cmd`` 与 ``id``），由 bridge 拼成 ``"<cmd> <id>"`` 再交给
#: ``ChannelService.handle``。
#:
#: ## 为什么它**无凭据**
#:
#: 与 :data:`MENU_ACTION` / :data:`SELECT_ACTION` 同族：点了就执行，
#: 不落库、不等回复。但**刻意不叫 ``allow_once`` 那一套** —— 那套是给
#: 「授权有副作用的动作」用的，带 TTL 与过期即拒。而 ``/done`` 这类
#: 状态迁移**已经有**自己的闸门（白名单 + 幂等 + 状态迁移表），
#: 再套一层审批凭据等于让同一次点击被记两次，且凭空多出「卡片过期了
#: 但用户还没点」这类失败模式。
COMMAND_ACTION = "command"


def task_action_card(
    title: str, task_id: str, *, chat_id: str, state: str = ""
) -> dict[str, Any]:
    """事务的三个状态迁移按钮：开始 / 做完 / 放一放。

    **无状态**（同 :func:`menu_card` 的思路）：按钮 value 只带要执行的
    命令与那条事务的 id 前缀，不带任何业务内容。

    刻意**不**把 ``"开始做"`` 这种中文翻译放在这张卡里：命令名是
    ``/start``，中文只印在按钮**文案**上。载荷里放中文就得在 bridge
    侧再翻译回去，而那份翻译表与命令表一漂移就是「点了报错」。
    """
    def _btn(label: str, cmd: str) -> dict[str, Any]:
        return {
            "tag": "button",
            "text": {"tag": "plain_text", "content": label},
            "type": "default",
            "value": {"action": COMMAND_ACTION, "cmd": cmd,
                      "id": task_id, "chat": chat_id},
        }

    elements: list[dict[str, Any]] = [
        {"tag": "div", "text": {"tag": "lark_md",
                                "content": f"**{title}**" + (
                                    f"（当前：{state}）" if state else "")}},
    ]
    elements.append({"tag": "action", "actions": [
        _btn("开始做", "/start"),
        _btn("做完啦", "/done"),
        _btn("放一放", "/pause"),
    ]})
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": title},
        },
        "elements": elements,
    }


def send_task_action_card(
    sender: Any,
    *,
    open_id: str,
    title: str,
    task_id: str,
    chat_id: str,
    state: str = "",
) -> str:
    """发事务动作卡，返回 message_id。发卡实现与 :func:`send_plan_confirm_card` 同一套。"""
    if not open_id:
        raise ValueError("发事务动作卡必须给收件人标识")
    id_type = sender._receive_id_type(open_id)
    card = task_action_card(title, task_id, chat_id=chat_id, state=state)
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
            f"发事务动作卡失败：code={body.get('code')} msg={body.get('msg')}"
            f"（收件人={open_id!r} 形态={id_type}）"
        )
    return str(((body.get("data") or {}).get("message_id")) or "")


def pairing_card(code: str, *, ttl_seconds: int) -> dict[str, Any]:
    """配对卡（设计文档 11.9.8）。**一个按钮都没有** —— 这是刻意的。

    这张卡**不发指令**，所以没有按钮可点：对方要做的事是「把码抄到终端」，
    而抄码这个动作**无法也不该**由飞书的点击事件代替 —— 一张能被点的卡
    会让「点一下就算配对」成立，那等于给陌生人一个自助入口。

    因此卡上**只有码和怎么用**，且**不回显任何白名单内容**。

    为什么给陌生人发这张卡不削弱白名单：白名单判定一个字都没动，
    配对解决的是「**怎么拿到 open_id**」，而白名单为空仍然拒绝启动
    （见 ``feishu/config.py::check_ready``）。
    """
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": "配对码"},
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md",
                                    "content": f"你的配对码（{ttl_seconds // 60} 分钟内有效）："}},
            {"tag": "div", "text": {"tag": "lark_md",
                                    "content": f"**{code}**"}},
            {"tag": "div", "text": {"tag": "lark_md", "content": (
                "在**这台机器的终端**里敲：\n"
                f"`/pair {code}`\n\n"
                "配对只做一件事：把这个对话的你的身份写进白名单。"
                "它**不会**替你执行任何命令。")}},
        ],
    }


def send_pairing_card(
    sender: Any, *, open_id: str, code: str, ttl_seconds: int
) -> str:
    """发配对卡，返回 message_id。"""
    if not open_id:
        raise ValueError("发配对卡必须给收件人标识")
    id_type = sender._receive_id_type(open_id)
    card = pairing_card(code, ttl_seconds=ttl_seconds)
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
            f"发配对卡失败：code={body.get('code')} msg={body.get('msg')}"
            f"（收件人={open_id!r} 形态={id_type}）"
        )
    return str(((body.get("data") or {}).get("message_id")) or "")


#: 与 :data:`VIEW_CHOICE_ACTION` **同族**：都是「点了就执行，不落库、
#: 不等回复」的即时动作。菜单不需要 pending 记录，因为菜单项本身
#: 没有「过期即拒」的安全含义 —— 点错了顶多跑一次只读视图或重发一张卡。
#: 真要审批的那类动作走 :func:`_question_card` / 批准卡，两条路不混。
MENU_ACTION = "menu"


def menu_card(
    subject: str,
    items: Sequence[tuple[str, str]],
    *,
    note: str = "",
    chat_id: str = "",
) -> dict[str, Any]:
    """入口菜单卡。**无状态**，与 :func:`view_choice_card` 同一套理由。

    ``items`` 是 ``(按钮文案, 回传 choice)`` 对。刻意做成参数而不是
    把菜单内容写死在这里：菜单项依赖运行时状态（角色名、项目白名单），
    而卡片结构是固定的 —— 分开之后同一个结构能被入口菜单、角色选择、
    委派向导三处复用，不至于三份JSON 各写一遍。

    ``chat_id`` 会进 ``value``，让点击处理知道**回哪个会话**。
    与 :func:`view_choice_card` 同一做法：不依赖回调载荷里的
    ``open_chat_id``（那字段在实测载荷里存在，但把它当唯一来源
    就等于把「点了有反应」押在一个我们没验过的字段上）。

    ## 为什么是 JSON 1.0 而不是 2.0

    官方错误码 200830 规定 **2.0 卡不能更新成 1.0，反之亦然**。
    批准卡与选项卡这一族全是 1.0（顶层 ``elements`` + ``tag:"action"``），
    菜单会走同一条更新回写路径，所以**必须**是 1.0。
    2.0 的 ``behaviors`` 更好看，但在这里换不来任何东西，只会让
    「点一下按钮」直接失败。
    """
    if not items:
        raise ValueError("菜单卡至少要一个菜单项")
    buttons = []
    for label, choice in items:
        # 按钮文案有长度上限，超了会被飞书截成看不懂的样子。
        buttons.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": str(label)[:40]},
                "type": "primary" if not buttons else "default",
                "value": {
                    "action": MENU_ACTION,
                    "choice": str(choice),
                    "chat": str(chat_id),
                },
            }
        )
    elements: list[dict[str, Any]] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": note or "点一个就行。"}}
    ]
    elements.append({"tag": "action", "actions": buttons})
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": subject},
        },
        "elements": elements,
    }


def send_menu_card(
    sender: Any,
    *,
    open_id: str,
    subject: str,
    items: Sequence[tuple[str, str]],
    note: str = "",
    chat_id: str = "",
) -> str:
    """发菜单卡，返回 message_id。

    发卡失败**由调用方决定**怎么办 —— 这里只抛，与
    :func:`send_view_choice_card` 同一口径：静默降级会让「点了没反应」
    比「多点一次」糟得多，而降级成什么文字是路由层的判断，不是发卡层的。
    """
    if not open_id:
        raise ValueError("发菜单卡必须给收件人标识")
    id_type = sender._receive_id_type(open_id)
    card = menu_card(subject, items, note=note, chat_id=chat_id)
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
            f"发菜单卡失败：code={body.get('code')} msg={body.get('msg')}"
            f"（收件人={open_id!r} 形态={id_type}）"
        )
    return str(((body.get("data") or {}).get("message_id")) or "")


#: 选择类动作。与 :data:`MENU_ACTION` 同族（无状态、点了就执行），
#: 但**回传的东西不是命令** —— 是「选了哪个选项」，由 bridge 决定下一步发哪张卡。
#:
#: 刻意**不开新 action**，也复用不了 menu：menu 的 choice 是**命令**（打给
#: ChannelService），而这里的 choice 是**数据**（option.value）。混用的话，
#: 「哪个 choice 是能直接当命令执行的」这件事就不再有单一判据。
SELECT_ACTION = "oc_select"

#: 四段选择的阶段名。顺序即流程顺序，**不是随便定的**：
#: 模型依赖 connected provider（要发查询），档位依赖模型（要读该模型的 variants）。
#: 所以 project → agent → model → variant，中间跳不过去。
SELECT_STAGES = ("project", "agent", "model", "variant")

#: 阶段的中文名，给「现在到第几步了」用。
_STAGE_LABELS = {
    "project": "项目",
    "agent": "工作模式",
    "model": "模型",
    "variant": "推理档位",
}


def select_card(
    stage: str,
    options: Sequence[Any],
    *,
    chat_id: str,
    page: int = 0,
    page_size: int = 0,
    selected: dict[str, str] | None = None,
    note: str = "",
) -> dict[str, Any]:
    """某一阶段的选择卡。**JSON 1.0**，与菜单卡同版本。

    ## 为什么 ``value`` 里带 ``page``

    翻页必须**无状态**：按钮载荷里带页码，点「下一页」时 bridge 直接
    知道该渲染第几页，不必在进程里存「谁翻到第几页」。带状态的做法在
    桥接重启、多端同时点、两个人在同一个会话里操作时都会错位 ——
    而错位的表现是「点了 A 结果选了 B」，那是最坏的一种。

    ## 为什么 ``selected`` 用来标「当前值」而不是禁用其它项

    Desktop 是下拉框，选中项就在框里。按钮列表没有那个概念，所以
    **把当前选中项标成 primary**（高亮）。不把其它项灰掉：飞书的
    ``disabled`` 按钮点不动，而用户可能要改主意 —— 灰掉等于替他做了取舍。

    :param options: :class:`~freeagent.services.oc_selection.Option` 序列。
        这里只读 ``value`` / ``label`` / ``hint`` / ``disabled``，
        刻意**不 import** 那个类 —— 卡片层不该知道数据层的类型，
        否则两个模块一起改就一起坏。
    """
    if stage not in SELECT_STAGES:
        raise ValueError(f"未知的选择阶段：{stage}")
    if not options:
        raise ValueError("选择卡至少要一个选项")
    size = page_size or len(options)
    total_pages = max(1, -(-len(options) // size))   # 向上取整
    page = max(0, min(page, total_pages - 1))
    window = options[page * size:(page + 1) * size]

    chosen = {k: str(v) for k, v in (selected or {}).items() if v}
    buttons: list[dict[str, Any]] = []
    for opt in window:
        value = str(getattr(opt, "value", "") or "")
        label = str(getattr(opt, "label", "") or "")
        hint = str(getattr(opt, "hint", "") or "")
        is_current = value == chosen.get(stage, "\0") if value else False
        text = f"{label} · {hint}" if hint and not is_current else label
        buttons.append({
            # 按钮文案上限 40 字，超了被飞书截成看不懂的东西。
            "tag": "button",
            "text": {"tag": "plain_text", "content": text[:40]},
            # 当前选中项高亮 —— 替代下拉框的「框里那个值」。
            "type": "primary" if is_current else "default",
            # ``disabled`` 的按钮在飞书里**不能点**，而「点了没反应」
            # 比「明确告诉你不能点」更让人困惑，所以这里**不**置灰，
            # 只把 note 里说明（见下方 hint 的用法）。
            "value": {
                "action": SELECT_ACTION,
                "stage": stage,
                "value": value,
                "page": page,
                "chat": str(chat_id),
            },
        })

    elements: list[dict[str, Any]] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": note or "点一个就行。"}}
    ]
    if total_pages > 1:
        elements.append({
            "tag": "note",
            "elements": [{"tag": "plain_text",
                          "content": f"第 {page + 1}/{total_pages} 页"}],
        })
    elements.append({"tag": "action", "actions": buttons})
    if total_pages > 1:
        elements.append({"tag": "action", "actions": [
            {
                "tag": "button",
                "text": {"tag": "plain_text",
                         "content": "上一页" if page > 0 else "«"},
                "type": "default",
                "value": {"action": SELECT_ACTION, "stage": stage,
                          "value": "", "page": page - 1, "nav": 1,
                          "chat": str(chat_id)},
            },
            {
                "tag": "button",
                "text": {"tag": "plain_text",
                         "content": "下一页" if page + 1 < total_pages else "»"},
                "type": "default",
                "value": {"action": SELECT_ACTION, "stage": stage,
                          "value": "", "page": page + 1, "nav": 1,
                          "chat": str(chat_id)},
            },
        ]})

    idx = SELECT_STAGES.index(stage) + 1
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text",
                      "content": f"选{_STAGE_LABELS[stage]}（{idx}/{len(SELECT_STAGES)}）"},
        },
        "elements": elements,
    }


def send_select_card(
    sender: Any,
    *,
    open_id: str,
    stage: str,
    options: Sequence[Any],
    chat_id: str,
    page: int = 0,
    page_size: int = 0,
    selected: dict[str, str] | None = None,
    note: str = "",
) -> str:
    """发选择卡，返回 message_id。与 :func:`send_menu_card` 同结构。"""
    if not open_id:
        raise ValueError("发选择卡必须给收件人标识")
    id_type = sender._receive_id_type(open_id)
    card = select_card(stage, options, chat_id=chat_id, page=page,
                       page_size=page_size, selected=selected, note=note)
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
            f"发选择卡失败：code={body.get('code')} msg={body.get('msg')}"
            f"（收件人={open_id!r} 形态={id_type}）"
        )
    return str(((body.get("data") or {}).get("message_id")) or "")


#: 委派简报确认卡的点击。与 :data:`SELECT_ACTION` **同族**（无状态、点了就执行），
#: 但它**消费**桥接进程内存里的一条待填简报 —— 点完这条简报就没了。
#:
#: ## 为什么**必须新开**一个 action，不能复用现成的
#:
#: 若复用 :data:`COMMAND_ACTION`，其载荷里的 ``cmd`` 会被 bridge 当成命令
#: **直接执行**（见 ``bridge._run_card_command`` 的设计）。而按钮 ``value``
#: 是飞书客户端回传的、**用户可以伪造**（转发卡片、手改 JSON）—— 于是
#: 「点一下」就可能触发一条本人没打算发的命令，包括危险的 ``/delegate``。
#:
#: 所以这张卡的载荷**只说点了哪个按钮**，具体要委派什么由桥接自己从内存
#: 状态里取。这也正是它与 :data:`SELECT_ACTION` 的区别：选卡回传的是
#: 「选了哪个选项」（数据），这张卡回传的是「点了哪个按钮」（意图），
#: 内容一律不进载荷 —— 把内容塞进载荷就等于让卡片成为第二份真源，
#: 而它会被转发、被缓存、会在会话淘汰后与真正的那份不一致。
BRIEF_CONFIRM_ACTION = "brief_confirm"


@dataclass(frozen=True, slots=True)
class BriefPreview:
    """确认卡要回显的那几行 —— **一个字段一行**。

    刻意做成一个具名值对象，而不是往 ``brief_confirm_card`` 上堆六个位置
    参数：这六个值同属一件事（「按当前选择会建出什么」），分开传会让调用点
    变成一长串猜不出顺序的实参。``frozen=True`` 保证卡片构造期间读到的
    就是调用时的快照 —— 确认卡的意义是「印出来的就是要执行的」，中途被改
    就失了这句承诺。

    注意：这是**给人看的预览**，不是载荷。它**不会**进按钮 ``value``
    （见 :data:`BRIEF_CONFIRM_ACTION` 的说明）。
    """

    project: str
    agent: str
    model: str
    variant: str
    role: str
    brief: str


def brief_prompt_card(subject: str, *, chat_id: str) -> dict[str, Any]:
    """交简报（第 5 步）的**提示卡**：请用户用一句话说清要做的事。

    ⚠️ **必须是 JSON 1.0**（顶层 ``config``/``header``/``elements``），与
    选卡那一族同版本。官方错误码 200830：2.0 卡不能更新成 1.0，反之亦然，
    而这一族的卡会走同一条更新回写路径（``_card_action_response``）。

    正文里刻意写明「**一句话**」并附例子，因为本仓有一条硬约束：
    委派简报**必须单行** —— 提示里只要有换行，opencode 就会判定为
    「复杂任务」并升级到主 agent 的强模型、**无视 ``--model``**，于是必然
    失败。把这条约束放在用户点得到的地方，比事后丢回一个看不懂的失败有用。

    只有一个按钮「先不做了」，``choice="cancel"``：中途退出的通道**必须**
    给，否则用户被卡在第 5 步，只能硬编一句话才能脱身。
    """
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": subject},
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": (
                "请用**一句话**说清要做的事，例如：\n"
                "「修一下登录页在窄屏下的按钮错位」。\n"
                "必须是**单行** —— 多行简报会被 opencode 判成复杂任务，"
                "升级到强模型、无视你选的模型，必然失败。")}},
            {"tag": "action", "actions": [
                {"tag": "button",
                 "text": {"tag": "plain_text", "content": "先不做了"},
                 "type": "default",
                 "value": {"action": BRIEF_CONFIRM_ACTION,
                           "choice": "cancel", "chat": str(chat_id)}},
            ]},
        ],
    }


def brief_confirm_card(
    subject: str, preview: BriefPreview, *, chat_id: str
) -> dict[str, Any]:
    """收到简报后的**确认卡**：回显将建的是什么，并给两个按钮。

    ⚠️ **必须是 JSON 1.0**（同 :func:`select_card`），理由见
    :func:`brief_prompt_card`。

    ## 载荷里''一个字段都不带''

    两个按钮的 ``value`` 都**只含** ``{action, choice, chat}`` 三个键 ——
    项目 / 模型 / 工作模式 / 推理档位 / 简报**一个都不进载荷**。理由整个设计
    的要害见 :data:`BRIEF_CONFIRM_ACTION`：载荷是客户端回传、**可伪造**的，
    所以只说「点了哪个按钮」，要委派什么由桥接从内存状态里取。载荷里放
    项目就等于让任何人伪造一个项目路径，绕过项目白名单那唯一一道闸门。

    ## 「脉络」写成**预览**而不是确定归属

    角色推断发生在桥接侧，这里只是把**当前**会怎么归先亮出来，并在括号里
    注明「可改」——用户点「改一下」就能重说。写成确定归属等于替用户的
    取舍背书，而那条规则（``infer_roles`` 之类）本就是启发式的、可反驳的。
    """
    lines = [
        f"**项目**：{preview.project}",
        f"**模型**：{preview.model}",
        f"**工作模式**：{preview.agent}",
        f"**推理档位**：{preview.variant}",
        f"**脉络**：按现有规则会归到「{preview.role}」（可改）",
        f"**要做的事**：{preview.brief}",
    ]

    def _btn(label: str, choice: str, primary: bool) -> dict[str, Any]:
        return {
            "tag": "button",
            # 按钮文案上限 40 字，超了被飞书截成看不懂的东西。
            "text": {"tag": "plain_text", "content": label[:40]},
            "type": "primary" if primary else "default",
            "value": {"action": BRIEF_CONFIRM_ACTION, "choice": choice,
                      "chat": str(chat_id)},
        }

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": subject},
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md",
                                    "content": "\n".join(lines)}},
            {"tag": "action", "actions": [
                _btn("建这条委派", "create", True),
                _btn("改一下", "retry", False),
            ]},
        ],
    }


def send_brief_prompt_card(
    sender: Any, *, open_id: str, subject: str, chat_id: str
) -> str:
    """发简报提示卡，返回 message_id。与 :func:`send_select_card` 同一套发卡实现。"""
    if not open_id:
        raise ValueError("发简报提示卡必须给收件人标识")
    id_type = sender._receive_id_type(open_id)
    card = brief_prompt_card(subject, chat_id=chat_id)
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
            f"发简报提示卡失败：code={body.get('code')} msg={body.get('msg')}"
            f"（收件人={open_id!r} 形态={id_type}）"
        )
    return str(((body.get("data") or {}).get("message_id")) or "")


def send_brief_confirm_card(
    sender: Any, *, open_id: str, subject: str, preview: BriefPreview,
    chat_id: str,
) -> str:
    """发简报确认卡，返回 message_id。与 :func:`send_select_card` 同一套发卡实现。"""
    if not open_id:
        raise ValueError("发简报确认卡必须给收件人标识")
    id_type = sender._receive_id_type(open_id)
    card = brief_confirm_card(subject, preview, chat_id=chat_id)
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
            f"发简报确认卡失败：code={body.get('code')} msg={body.get('msg')}"
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


def _urllib_cardkit(
    url: str,
    payload: Mapping[str, object],
    headers: Mapping[str, str],
    timeout: float,
    method: str,
) -> str:
    """真实发 PUT / PATCH 的兜底 transport。

    :func:`urllib_transport` 刻意只支持 GET/POST —— 它的签名与
    :class:`DeepSeekProvider` 共享，加 ``method`` 参数会波及每一个
    测试替身。所以 CardKit 走这一个，不复用那个签名。

    ``method`` 显式传，不靠 ``payload`` 是否为空推断 —— PUT/PATCH
    带 body 是正常的，用「空 body = GET」的约定会把它们全判成 GET。
    """
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", **dict(headers)},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise FeishuError(f"飞书 CardKit {method} HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise FeishuError(f"连不上飞书：{exc.reason}") from exc
