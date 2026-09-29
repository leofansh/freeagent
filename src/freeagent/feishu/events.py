"""把飞书事件解析成「一条待处理的文字消息」。**纯函数，不碰 SDK。**

刻意做成纯函数：事件形状是飞书定的、随时可能变，把它和业务逻辑分开，
就能在**不联网、不装 SDK** 的情况下用真实 payload 测。测试里那些 fixture
是从飞书文档抄的真实结构，不是编的。

## 三个真实的坑

1. ``event.message.content`` 是**被 JSON 编码过一次的字符串**：
   整个事件是 JSON，里面的 ``content`` 又是一个 JSON 字符串。
   ```json
   {"message": {"content": "{\\"text\\": \\"@_user_1 帮我记一下\\"}"}}
   ```
   只解一层会拿到 ``'{"text":...}'`` 这么一坨字。

2. **@机器人 会以占位符形式留在正文里**（``@_user_1``）。不剥掉的话，
   用户打 ``@bot 记一下买牛奶`` 会被当成 ``帮我记一下@_user_1 记一下买牛奶`` ——
   角色推断和意图识别全被污染。

3. **「@ 了谁」在 ``mentions`` 里，不在正文里。** 占位符只是个 key，
   身份（``id.open_id``）和显示名（``name``）都得从 ``mentions`` 里取。
   所以群聊门控能做成「只认 @ bot 自己」而不是「@ 了什么都回」——
   详见 :func:`parse_event` 与设计方案 11.9.1。取名字时**别把别人的 @ 也删掉**：
   删掉等于抹掉句子的语义主体。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

__all__ = [
    "IncomingMessage",
    "Ignored",
    "parse_event",
    "from_sdk_event",
    "describe_sdk_shape",
    "MESSAGE_EVENT",
]

#: 只处理收消息这一个事件。其余（进群、成员变动）本项目用不上。
MESSAGE_EVENT = "im.message.receive_v1"

#: 私聊。
CHAT_P2P = "p2p"

#: 正文里 @ 占位符的形状。用来兜底清理「mentions 里没列出来」的漏网之鱼。
_PLACEHOLDER_RE = re.compile(r"@_[a-zA-Z0-9_]+")


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    """一条可以交给命令路由的文字消息。"""

    event_id: str
    chat_id: str
    sender_open_id: str
    text: str
    is_group: bool
    #: 群里是否 **@ 了机器人本人**。私聊恒为 True（不需要 @）。
    #:
    #: 判据是「@ 的对象里有没有 bot 自己的 open_id」，**不是**「有没有 @ 别人」。
    #: 见 :func:`parse_event` 与设计方案 11.9.1。
    mentioned: bool
    #: 门控未通过时的人话说明，供日志说清「为什么没回」。空 = 已通过。
    mention_note: str = ""
    #: **非空**表示这条消息不是文字（图片/文件/富文本），值是实际的
    #: ``message_type``。``text`` 此时是空的、**不可派发**，桥接用它回一句
    #: 「我只处理文字」而不是让用户对着沉默的 bot 猜（设计方案 11.9.1）。
    #:
    #: 刻意**不**复用 :class:`Ignored`：丢掉的消息和「要回一句提示的消息」
    #: 走的是两条不同的路 —— 前者只记日志，后者必须先过白名单再过群聊门控
    #: 才回话，混在一起就容易把提示发到不该发的地方。
    unsupported_type: str = ""
    #: 飞书的消息级标识（``message.message_id``）。
    #:
    #: 它存在是因为 ``event_id`` **可能为空**，而去重需要一个稳定的键 ——
    #: 详见 :attr:`dedup_key` 与设计方案 11.9.2 的去重键规则。
    message_id: str = ""
    #: 发送者的 **user_id**（租户级，换飞书应用不变）。
    #:
    #: 为什么要有它：``open_id`` 是**应用级**的，换一个飞书应用同一个人就变成
    #: 另一个值 —— 白名单得跟着重填。而 ``user_id`` 稳定得多，Hermes 就优先用它
    #: 做会话键（其 ``feishu.py:3916`` 的 ``user_id or open_id``）。
    #:
    #: **需要应用申请对应权限**才有值；没申请时事件里就不带它。
    sender_user_id: str = ""
    #: 发送者的 ``union_id``（开发者级）。飞书目前基本不给，留着是为了
    #: 「哪天开始给了」不用再改结构。
    sender_union_id: str = ""

    @property
    def sender_ids(self) -> frozenset[str]:
        """这条消息携带的**全部**发送者身份标识。

        白名单拿它求交集（见 :meth:`freeagent.services.channel.ChannelRouter.is_allowed`），
        而不是只比 ``open_id``。理由：白名单是「谁能指挥本机」的边界，
        事件**实际带了什么**就是能验证什么；人为只留一层会让「配了却不灵」
        变成查代码才能定位的问题 —— 那正是 ``ou_f4f1…`` 换应用后失效的由来。
        """
        return frozenset(
            i for i in (self.sender_open_id, self.sender_user_id, self.sender_union_id) if i
        )

    @property
    def sender_label(self) -> str:
        """给日志/错误提示用的一句话，含**每一层**标识。

        不匹配时要能一眼看出「事件里是 ou_xxx / user_id=yyy，白名单里是
        ou_zzz」—— 只说「不在白名单」等于把排查成本全推给用户。
        """
        parts = [f"open_id={self.sender_open_id or '(空)'}"]
        if self.sender_user_id:
            parts.append(f"user_id={self.sender_user_id}")
        if self.sender_union_id:
            parts.append(f"union_id={self.sender_union_id}")
        return " ".join(parts)

    @property
    def dedup_key(self) -> str:
        """去重该用哪个键。**空串表示无键可用**，调用方必须显式处理。

        顺序是 ``event_id`` → ``message_id``（设计方案 11.9.2）。
        两者都空时**不能静默跳过去重** —— 那样同一条消息被重投就会重复处理，
        而日志里什么都不打，排查只能靠猜。
        """
        return self.event_id or self.message_id


@dataclass(frozen=True, slots=True)
class Ignored:
    """事件被丢弃，以及**为什么**。

    保留原因是必须的：出问题时（「bot 一直不回我」）得能立刻分辨
    是没收到、被白名单拦了、还是消息类型不支持。
    """

    reason: str


def _mention_open_id(mention: Any) -> str:
    """从一个 mention 对象里取出 ``open_id``，取不到返回空串。

    兼容两种形态：真实 payload 里是 dict（``{"id": {"open_id": …}}``），
    而 :func:`from_sdk_event` 拼出来的也是 dict；但直接喂 SDK 对象时
    ``id`` 可能是类型化对象，所以两种都试。
    """
    ident = mention.get("id") if isinstance(mention, dict) else getattr(mention, "id", None)
    if isinstance(ident, dict):
        value = ident.get("open_id")
    else:
        value = getattr(ident, "open_id", None)
    return value.strip() if isinstance(value, str) else ""


def _mention_name(mention: Any) -> str:
    """被 @ 者的显示名。取不到返回空串。"""
    value = mention.get("name") if isinstance(mention, dict) else getattr(mention, "name", None)
    return value.strip() if isinstance(value, str) else ""


def _replace_mentions(
    text: str, mentions: list[dict[str, object]], bot_open_id: str | None
) -> str:
    """把 ``@_user_1`` 这类占位符按**身份**处理掉。

    飞书不在 ``content`` 里写「@某人」，而是留一个 key，正文里就是
    ``@_user_1``。不处理会污染意图识别和角色推断。但**处理方式要分身份**
    （设计方案 11.9.1）：

    - **@ 的是 bot 自己** → 删掉。留个「bot」在句子里反而碍事。
    - **@ 的是别人** → **替换成显示名**。只删不替会把「@张三 你看下这个」
      变成「你看下这个」，语义主体被抹掉了；替换后是「张三 你看下这个」，
      意图识别拿到的仍是完整句子。
    - 显示名也取不到 → 才退化为删掉。**绝不能把 ``@_user_2`` 这种占位符
      留在正文里**，那正是本函数要消灭的东西。
    """
    for mention in mentions:
        if not isinstance(mention, dict):
            continue
        key = mention.get("key")
        if not isinstance(key, str) or not key:
            continue
        is_self = bool(bot_open_id) and _mention_open_id(mention) == bot_open_id
        if is_self:
            text = text.replace(key, " ")
        else:
            name = _mention_name(mention)
            text = text.replace(key, f" {name} " if name else " ")
    # 兜底扫一遍**没在 mentions 里出现**的占位符。
    # 飞书正常情况下 mentions 覆盖正文里所有占位符，但「文档说的」不等于
    # 「实测过的」：一旦对不上，``@_user_2`` 就会原样进正文，污染意图识别，
    # 而且**不留任何痕迹** —— 日志里看不出异常，消息也照发，只是答非所问。
    # 与其赌形状，不如按正则清干净。
    text = _PLACEHOLDER_RE.sub(" ", text)
    return " ".join(text.split())


def parse_event(
    payload: Mapping[str, object], *, bot_open_id: str | None = None
) -> IncomingMessage | Ignored | None:
    """解析一条飞书事件。

    返回 ``None`` 表示「不是我要处理的事件」（比如进群事件），静默跳过。
    返回 :class:`Ignored` 表示收到了但不该处理，**带原因**。

    ## ``bot_open_id`` 决定群里回不回应

    ``bot_open_id`` 是 bot 自己的 ``open_id``，由
    :func:`freeagent.feishu.identity.resolve_identity` 在启动时探到。

    - **私聊**：不需要 @，恒为「被叫到」。
    - **群聊**：只有 ``mentions`` 里**有某个对象的 open_id 等于
      ``bot_open_id``** 才算被叫到。``@张三 你看下这个`` 里 @ 的是别人，
      所以**不响应** —— 这与设计方案 11.9.1 边界 2 一致。
    - **``bot_open_id`` 未知**（探测失败）：门控**失败关闭**，即群里带 @ 的
      消息一律不响应。和 11.9.1 边界 1 同一个原则 —— 判不准的时候朝「关」偏。
      代价是新装完还没探到身份时群里会安静，**所以桥接必须在启动时显著告警**，
      ``mention_note`` 会把原因带给日志。
    """
    if not isinstance(payload, dict):
        return Ignored("payload 不是 dict")
    header = payload.get("header")
    if not isinstance(header, dict):
        return Ignored("缺 header")
    if header.get("event_type") != MESSAGE_EVENT:
        return None

    event = payload.get("event")
    if not isinstance(event, dict):
        return Ignored("缺 event")

    message = event.get("message")
    if not isinstance(message, dict):
        return Ignored("缺 message")
    sender = event.get("sender")
    if not isinstance(sender, dict):
        return Ignored("缺 sender")
    sender_id = sender.get("sender_id")
    if not isinstance(sender_id, dict):
        return Ignored("缺 sender_id")

    open_id = sender_id.get("open_id")
    if not isinstance(open_id, str):
        open_id = ""
    # 三个 ID 都收，缺哪个算哪个 —— **不再因为「缺 open_id」就整条丢掉**。
    #
    # 踩过的坑：原先这里 `return Ignored("缺 open_id")`。而 user_id 是租户级、
    # 跨应用稳定的那一个；一旦某条消息只带 user_id（比如应用没配齐 open_id
    # 权限、或将来飞书调整字段），消息会在**解析阶段**就被丢掉，日志只留一句
    # 「缺 open_id」，看起来像「什么都没收到」—— 而真相是收到了、只是没读。
    user_id = sender_id.get("user_id")
    if not isinstance(user_id, str):
        user_id = ""
    union_id = sender_id.get("union_id")
    if not isinstance(union_id, str):
        union_id = ""
    if not (open_id or user_id or union_id):
        return Ignored("发送者没有任何身份标识")

    chat_id = message.get("chat_id")
    if not isinstance(chat_id, str) or not chat_id:
        return Ignored("缺 chat_id")

    mentions = message.get("mentions")
    if not isinstance(mentions, list):
        mentions = []

    # 群里必须 @ **bot 自己** 才响应。判据是身份，不是「@ 了某个东西」。
    # 踩过的坑：原来只判 ``mentions`` 非空，于是群里 ``@张三 你看下这个``
    # 也会插嘴 —— 它对 @ 的人类说话，而且拿到的是一句被剥掉占位符的残句，
    # 意图识别必然跑偏。详见设计方案 11.9.1。
    #
    # 顺序要紧：门控**先于**「这是不是文字」判断。否则群里一条没 @ bot 的
    # 图片会先走「不支持类型」那条路而回一句提示 —— 那等于把「不抢答」
    # 这条规矩又破了一次。参见设计方案 11.9.1 的回复顺序硬约束。
    is_p2p = message.get("chat_type") == CHAT_P2P
    mentioned, note = _gate_group(is_p2p, mentions, bot_open_id)

    #: 消息级标识。``event_id`` 可能为空，所以去重要靠它兜底（设计方案 11.9.2）。
    message_id = message.get("message_id")
    message_id = message_id.strip() if isinstance(message_id, str) else ""

    message_type = message.get("message_type")
    if message_type != "text":
        # 非文字**不回一句「暂只处理」就完事** —— 那是实现细节，用户不需要知道。
        # 交给桥接去发那句「我只处理文字」（见 ``IncomingMessage.unsupported_type``）：
        # 那条回复必须先过白名单再过门控，放在这里发就绕过了白名单。
        return IncomingMessage(
            event_id=str(header.get("event_id") or ""),
            chat_id=chat_id,
            sender_open_id=open_id,
            sender_user_id=user_id,
            sender_union_id=union_id,
            text="",                       # 不可派发，桥接见到空 text + 类型就回提示
            is_group=not is_p2p,
            mentioned=mentioned,
            mention_note=note,
            unsupported_type=str(message_type or "未知"),
            message_id=message_id,
        )

    raw_content = message.get("content")
    if not isinstance(raw_content, str):
        return Ignored("缺 content")
    try:
        # 坑 1：content 本身是 JSON **字符串**，要解两次。
        inner = json.loads(raw_content)
    except json.JSONDecodeError as exc:
        return Ignored(f"content 不是 JSON：{exc}")
    if not isinstance(inner, dict):
        return Ignored("content 解出来不是 dict")
    text = inner.get("text")
    if not isinstance(text, str):
        return Ignored("content 里没有 text")

    text = _replace_mentions(text, mentions, bot_open_id)
    if not text:
        return Ignored("去掉 @ 之后没有正文")

    return IncomingMessage(
        event_id=str(header.get("event_id") or ""),
        chat_id=chat_id,
        sender_open_id=open_id,
        sender_user_id=user_id,
        sender_union_id=union_id,
        text=text,
        is_group=not is_p2p,
        mentioned=mentioned,
        mention_note=note,
        message_id=message_id,
    )


def _gate_group(
    is_p2p: bool, mentions: list[object], bot_open_id: str | None
) -> tuple[bool, str]:
    """群里「@ 的到底是不是 bot 自己」。返回 ``(是否被叫到, 人话原因)``。

    三种「不响应」的原因必须分开说。踩过的坑：原先只分两种，于是群里
    一个人都没 @ 时，日志却说「群里 @ 的是别人不是 bot」—— 结论没错，
    但原因是**假的**，照着日志排查的人会被带偏（真去查「谁 @ 了 bot」，
    而实际上根本没人 @）。同理「群里带 @ 但身份未知」也不该在没有
    @ 的消息上出现。note 是给人和给日志看的，不是装饰。
    """
    if is_p2p:
        return True, ""                    # 私聊不需要 @
    if not mentions:
        return False, "群里没有 @ bot，不响应"
    if not bot_open_id:
        return False, (
            "群里 @ 了但 bot 身份未知，门控失败关闭不响应"
            "（**私聊不受影响**；桥接启动时应告警，跑 doctor 可查）"
        )
    if any(_mention_open_id(m) == bot_open_id for m in mentions):
        return True, ""
    return False, "群里 @ 的是别人不是 bot，不响应"


def _get(obj: Any, name: str) -> Any:
    """安全取属性：SDK 对象和测试里的假对象都吃得下。"""
    return getattr(obj, name, None)


#: 只往下看这么多层。再深也用不上，而且日志会变得没法读。
_SHAPE_DEPTH = 3

#: 这些属性名一眼就知道是框架的，不值得出现在结构报告里。
_SHAPE_SKIP = frozenset({"builder", "parent", "to_dict", "dict", "_dict"})


def describe_sdk_shape(event: Any, depth: int = _SHAPE_DEPTH) -> str:
    """描述 SDK 事件对象**有哪些属性**，**只列名字，绝不列值**。

    为什么需要这个：``from_sdk_event`` 是**照文档写的**，从没在真实飞书事件上
    验证过。万一真实结构和我假设的不一样，机器人会安静地不理人，
    而日志里只有一句「缺 message」—— **没有任何线索能让人发现是结构不对**。

    有这份描述，一次失败的日志就能看出真实结构长什么样，剩下的是改几行字段名。

    刻意不打印值：事件里有消息正文和 ``open_id``，那是用户的隐私。
    排查「形状对不对」只需要知道**有哪些键**。

    踩过的坑：一开始下钻的字段名是**写死**的（``message`` / ``sender`` 之类），
    结果「结构对不上」这种最需要它的场景反而最没用 —— 真实结构用了别的字段名时，
    报告只会显示顶层就停了。**改成遍历全部非原始类型属性**，
    这样字段名跟我猜的不同时才真的能看出差异。
    """
    _PRIMITIVES = (str, bytes, int, float, bool)

    def walk(obj: Any, level: int, path: str) -> list[str]:
        if obj is None or level <= 0:
            return []
        try:
            names = sorted(
                n for n in dir(obj)
                if not n.startswith("_") and n not in _SHAPE_SKIP
            )
        except Exception:
            return [f"{path}[?]"]
        if not names:
            return []
        out = [f"{path}[{','.join(names)}]"]
        if level == 1:
            return out
        for name in names:
            try:
                child = getattr(obj, name, None)
            except Exception:
                # 属性可能是会抛异常的 property —— 跳过，不能因为它连带崩掉
                continue
            if child is None or isinstance(child, _PRIMITIVES):
                continue
            out.extend(walk(child, level - 1, f"{path}.{name}"))
        return out

    try:
        lines = walk(event, depth, "event")
    except Exception as exc:                       # 描述失败不该连带出事
        return f"(无法描述：{type(exc).__name__})"
    return " ".join(lines) if lines else "(空对象)"


def _sdk_mention(mention: Any) -> dict[str, Any]:
    """把 SDK 的 mention 对象压成 :func:`parse_event` 要的 dict 形状。

    **踩过的坑：只取 ``key`` 就够了 —— 错。** 早期实现只把 ``key`` 搬过来，
    ``id.open_id`` 和 ``name`` 全丢了，于是 :func:`parse_event` 根本没法判断
    「@ 的是不是 bot 自己」：手上只有一个占位符字符串，没有身份。
    结果就是 SDK 路径上 @ 门控**静默失效**，而用真实 dict payload 写的测试
    全绿 —— 因为那条路径的 mentions 是完整的。修 bug 时必须同时修适配器，
    否则只改一边等于没改。
    """

    def pick(name: str) -> Any:
        if isinstance(mention, dict):
            return mention.get(name)
        return getattr(mention, name, None)

    key = pick("key")
    if not isinstance(key, str) or not key:
        return {}
    ident = pick("id")
    open_id = (
        ident.get("open_id")
        if isinstance(ident, dict)
        else getattr(ident, "open_id", None)
    )
    name = pick("name")
    out: dict[str, Any] = {"key": key}
    if isinstance(open_id, str) and open_id:
        out["id"] = {"open_id": open_id}
    if isinstance(name, str) and name:
        out["name"] = name
    return out


def from_sdk_event(
    event: Any, *, bot_open_id: str | None = None
) -> IncomingMessage | Ignored | None:
    """把 SDK 的类型化事件归一化成 dict，再交给 :func:`parse_event`。

    ## 为什么要多这一层

    长连接回调收到的是 ``P2ImMessageReceiveV1`` **类型化对象**，不是 dict。
    实测（lark-oapi 1.7.3）确认它**拿不回原始 dict** ——
    ``_dict`` / ``raw`` / ``d`` 全是 ``None``，所以没法直接把原始 JSON
    喂给解析器。

    但字段是直接暴露的（``ev.event.message.chat_id`` 等）。于是这里把需要的
    字段**重新拼成同样的 dict 形状**，再复用同一个 :func:`parse_event`。

    这样做的价值：解析逻辑（校验、去 @ 占位符、剥壳）**只有一份**，
    而且那份逻辑是用**真实 payload 的 dict** 测的 —— 不需要装 SDK 就能测。
    若改成直接读 SDK 对象，就得装 SDK 才能测，核心包也就跟着沾上依赖了。

    本函数**刻意不 import lark_oapi**：用鸭子类型，
    测试传一个只有那几个属性的假对象即可。
    """
    header = _get(event, "header")
    data = _get(event, "event")
    message = _get(data, "message")
    sender = _get(data, "sender")
    sender_id = _get(sender, "sender_id")

    # 连 header 都没有 = 结构就不对，不是「不是我要处理的事件」。
    # 踩过的坑：一开始这里让这种对象走到 parse_event，靠 event_type 不匹配
    # 返回 None —— 而 None 的含义是「静默跳过、不记日志」。结果结构异常被
    # 无声吞掉，排查时连一条线索都没有。缺 header 必须是 Ignored。
    if header is None:
        return Ignored("事件连 header 都没有，结构异常")

    mentions = _get(message, "mentions")
    normalized: list[dict[str, Any]] = []
    if isinstance(mentions, list):
        for mention in mentions:
            shaped = _sdk_mention(mention)
            if shaped:
                normalized.append(shaped)

    payload = {
        "header": {
            "event_id": _get(header, "event_id"),
            "event_type": _get(header, "event_type"),
        },
        "event": {
            # **三个 ID 都要带上。** 原先只抄 open_id，user_id / union_id
            # 在这一步就被丢掉了 —— 而它们恰恰是「换飞书应用之后白名单失效」
            # 的解药（见 IncomingMessage.sender_user_id 的说明）。
            "sender": {
                "sender_id": {
                    "open_id": _get(sender_id, "open_id"),
                    "user_id": _get(sender_id, "user_id"),
                    "union_id": _get(sender_id, "union_id"),
                }
            },
            "message": {
                "chat_id": _get(message, "chat_id"),
                "chat_type": _get(message, "chat_type"),
                "message_type": _get(message, "message_type"),
                "content": _get(message, "content"),
                "mentions": normalized,
                # ``event_id`` 可能为空，去重要靠这个兜底（设计方案 11.9.2）。
                # 漏掉它 = 真实事件里 ``dedup_key`` 永远拿不到 ``message_id``，
                # 而这个洞在离线测试里看不出来 —— 离线 fixture 的
                # ``event_id`` 一直是有的。
                "message_id": _get(message, "message_id"),
            },
        },
    }
    return parse_event(payload, bot_open_id=bot_open_id)
