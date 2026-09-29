"""远程通道（飞书等）共用的消息路由层 —— **不含任何传输代码**。

这个模块刻意**不碰飞书**：不 import 飞书 SDK、不做 WebSocket、不发 HTTP。
它只回答一个问题 ——「一条聊天消息进来，应该回什么文字」。

传输层（长连接、事件解析、卡片、去重投递）由外面接上。这样分层的好处是
核心包继续保持零依赖：装不装飞书都不影响 ``freeagent`` 本身。

## 为什么不重写一套命令，而是包住终端那一套

本项目的核心承诺是「共用服务层，规则不会漂移」。如果通道另写一套命令解析，
那么终端能做的事和飞书能做的事迟早分叉 —— 而且分叉的方向通常是**权限悄悄变大**：
终端上要敲 `/accept` 才能采纳的草稿，通道里可能就顺手接了。

所以这里把 :class:`~freeagent.cli.app.Repl` 的 ``out`` 换成缓冲区，
直接复用它的全部派发逻辑。**业务规则只有一份**，通道只是换了个输出目的地。

## 四条从实测/事故里学来的约束

1. **提醒不能顺带渲染。** ``reminders.due()`` 是**纯查询**（不落去重令牌），
   所以一条普通消息不会再把提醒**消费掉**；但沿用终端 ``handle()`` 仍会
   把到期提醒**渲染进不相干的回复里** —— 用户问「今天该做什么」却收到
   一坨提醒，答非所问。所以这里一律 ``check_reminders=False``，
   提醒改由 :meth:`due_reminders` 按节奏推送。
2. **白名单默认为空 = 全部拒绝。** 和 ``delegate.projects`` 同一个原则：
   链路终点是「在你机器上执行代码」，所以默认必须是**关**的，不是开的。
3. **按 chat 保留对话状态。** 终端的「这是放到哪个脉络里？」追问是**有状态**的；
   聊天天然是持久会话，所以每个 chat 保留一个 ``Repl``，而不是每次新建。
4. **白名单检查必须排在去重之前。** 反过来的话，未授权者的 ``event_id``
   会先进去重表 —— 陌生人只要狂发消息就能把表撑大。生产里那张表是落盘的，
   那等于让别人消耗你的磁盘。顺序见 :meth:`handle`。
"""

from __future__ import annotations

import io
import time
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Protocol, runtime_checkable

if TYPE_CHECKING:  # 避免运行期导入 app（app 会导入 services，会成环）
    from ..app import App

__all__ = [
    "ChannelReply",
    "ChannelService",
    "Deduplicator",
    "InMemoryDeduplicator",
]

#: 回复里明确告知「这条没生效」时的统一措辞。宁可啰嗦也不能让用户
#: 以为操作成功了 —— 通道是异步的，没有终端那声回车给他确认。
DENIED = "这条没有执行。"

#: 超过这个长度就截断。飞书单条消息有上限，而「静默截断」比「说被截断了」
#: 危险得多：用户会以为看到的是全部。
MAX_REPLY_CHARS = 3500

#: 内存去重表的默认参数。与 ``feishu/dedup.py`` 的同名常量**必须一致** ——
#: 两份数值是刻意重复的：``services`` 层不能 import ``feishu``（那是唯一
#: 依赖 SDK 的一层），所以这里复制一份，并由
#: ``tests/test_feishu_dedup.py`` 断言两者相等，防止悄悄漂移。
DEFAULT_DEDUP_TTL_SECONDS = 24 * 3600
DEFAULT_DEDUP_MAX_ENTRIES = 2048


#: ``@runtime_checkable`` 是刻意的：``isinstance`` 只校验方法**存在**，
#: 不校验签名 —— 签名靠类型检查器。于是这个协议有了一个可执行的形状断言
#: （见 ``tests/test_channel.py``），而落盘实现和内存实现的签名漂移由
#: LSP 兜住。两者配合才完整，只靠其中一个都会漏。
@runtime_checkable
class Deduplicator(Protocol):
    """「这个事件见过吗」。

    刻意做成协议而不是具体类：落盘实现在 ``feishu/dedup.py``，而本模块
    **不能** import 它（那会顺着 SDK 依赖爬进核心包）。默认给内存实现，
    桥接再把落盘那个注入进来。见设计方案 11.9.2 与 12.3 分层纪律。
    """

    def is_duplicate(self, event_id: str) -> bool:
        """见过（含本次）返回 ``True``。"""
        ...


class InMemoryDeduplicator:
    """进程内去重。**默认实现**，也是测试用的那个。

    和 ``feishu/dedup.py`` 的落盘版**同语义**（同样有 TTL 与条数上限），
    只差持久性。这样两条路径的行为不会因为「换了实现」而悄悄变宽或变严。
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_DEDUP_TTL_SECONDS,
        max_entries: int = DEFAULT_DEDUP_MAX_ENTRIES,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._ttl = float(ttl_seconds)
        self._max = int(max_entries)
        self._now = now
        self._seen: dict[str, float] = {}

    def is_duplicate(self, event_id: str) -> bool:
        if not event_id:
            return False
        now = self._now()
        cutoff = now - self._ttl
        # 顺带剪掉过期的：只留一个 dict 就会随时间单调增长
        for key in [k for k, t in self._seen.items() if t < cutoff]:
            del self._seen[key]
        if event_id in self._seen:
            self._seen[event_id] = now
            return True
        self._seen[event_id] = now
        overflow = len(self._seen) - self._max
        if overflow > 0:
            for key in sorted(self._seen, key=self._seen.get)[:overflow]:
                del self._seen[key]
        return False

    def __len__(self) -> int:
        return len(self._seen)


@dataclass(frozen=True, slots=True)
class ChannelReply:
    """一条消息的回复。"""

    text: str
    #: 是否被拒（不在白名单 / 被去重丢弃）。用于日志与测试。
    denied: bool = False
    #: **只读视图选项**。非空时，飞书侧应渲染成**按钮卡**而不是纯文字。
    #:
    #: 为什么需要它：低置信度时 :class:`ChatService` 返回 ``CLARIFY``
    #: （见设计文档 12.1.1「写侧兜底：默认问，不默认记」），而那个 kind
    #: 在这一层被丢掉了 —— 桥接拿到的只有一串文字，于是**只能回纯文本**，
    #: A 方案（点一下就好）就没法做。
    #:
    #: 只装**只读**视图。只读没有副作用，因此**不需要审批凭据** ——
    #: 按钮的 value 直接带视图名即可，那张卡是**无状态**的。
    #: 写操作走文字反问（B 方案），不要塞进这张卡。
    choices: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ReminderBatch:
    """一次推送的**两半**：要发的话，和还没落库的凭据。

    刻意不把确认藏在取提醒里面 —— 传输层拿到文字时才刚开始发，
    发没发成功只有它知道。取的时候就把令牌落了，失败那条就永远丢了
    （设计文档 9.2）。

    这是**纯数据**，不持有任何协作者也不自己落库。确认走
    :meth:`ChannelService.confirm_reminders`，与 :meth:`due_reminders`
    对称 —— 两个方法都在这一层碰 reminders，调用方只管发。
    """

    #: 合并摘要正文；空串表示这轮没有到期提醒。
    text: str
    #: 确认用的凭据；``text`` 为空时为 ``None``。
    digest: ReminderDigest | None = None


class ChannelService:
    """把聊天消息路由到与终端**完全一致**的命令语义。"""

    def __init__(
        self,
        app: App,
        *,
        allowed_senders: frozenset[str] = frozenset(),
        max_chats: int = 64,
        dedup: Deduplicator | None = None,
    ) -> None:
        """``allowed_senders`` 是飞书 ``open_id`` 集合。

        **留空 = 谁都不许指挥。** 这是有意的：白名单是「谁能触发本地执行」
        的信任边界，默认必须朝关的方向。往里加人要显式，不能靠漏配。

        ``dedup`` 默认内存表；桥接会注入落盘版（跨重启记得住）。
        刻意允许替换 —— 跨重启去重要落盘，可本模块不能碰文件系统与 SDK。
        """
        self.app = app
        self.allowed = allowed_senders
        self.max_chats = max_chats
        self.dedup: Deduplicator = dedup if dedup is not None else InMemoryDeduplicator()
        # 标注成 ``Any`` 而不是 ``object``：这里存的是 :class:`Repl` 实例，
        # 而 ``object`` 会让下面每一次 ``repl.out = …`` / ``repl.handle(…)``
        # 都变成类型错误（``object`` 上没有那些属性）。类型标注写窄一点，
        # 换来的是**真实**的检查而不是一堆假错误。
        self._repls: OrderedDict[str, Any] = OrderedDict()
        #: 上一条消息附带的只读视图选项。飞书侧据此渲染按钮卡。
        self._last_choices: tuple[str, ...] = ()

    # -- 白名单 ------------------------------------------------------------- #
    def is_allowed(self, sender_ids: str | Iterable[str]) -> bool:
        """这个发送者在白名单里吗。

        收**一个或多个**标识，逐一比对。理由：飞书同一个人有三层 ID
        （``open_id`` 应用级 / ``user_id`` 租户级 / ``union_id`` 开发者级），
        事件**实际带了哪几层**取决于应用申请了哪些权限。白名单只认一层的话，
        「配了却不灵」就只能靠翻代码定位 —— 这正是换飞书应用后 ``open_id``
        变化、白名单静默失效的那次。

        事件带了什么就用什么：**不**降级到名字匹配（名字可重名、可冒用，
        不能作为「谁能指挥本机执行代码」的判据）。
        """
        if isinstance(sender_ids, str):
            candidates = {sender_ids}
        else:
            candidates = {i for i in sender_ids if i}
        return bool(candidates & self.allowed)

    def allowlist_mismatch(self, sender_ids: str | Iterable[str]) -> str:
        """给日志/提示用的一句话：双方各是什么。

        「不在白名单」这四个字是排查的死路 —— 用户不知道自己那个 ID 变没变。
        所以把**事件里的每一层**和**白名单内容**都摆出来。
        """
        if isinstance(sender_ids, str):
            seen = {sender_ids}
        else:
            seen = {i for i in sender_ids if i}
        seen_text = ", ".join(sorted(seen)) or "空"
        allow_text = ", ".join(sorted(self.allowed)) or "空"
        return f"事件侧=[{seen_text}]，白名单=[{allow_text}]"

    # -- 路由 --------------------------------------------------------------- #
    def handle(
        self,
        chat_id: str,
        sender_ids: str | Iterable[str],
        text: str,
        *,
        event_id: str | None = None,
    ) -> ChannelReply:
        """处理一条消息，返回要回给用户的内容。

        ``sender_ids`` 收一个或多个标识，见 :meth:`is_allowed`。
        """
        if not self.is_allowed(sender_ids):
            # 回给对方的仍然只有「没有权限」—— 说清「你差哪个 ID」等于给
            # 陌生人一个可探测的开关。详细对照只进日志（见 allowlist_mismatch）。
            return ChannelReply(text="没有权限。", denied=True)

        if event_id is not None and self._is_duplicate(event_id):
            return ChannelReply(text="", denied=True)

        text = text.strip()
        if not text:
            return ChannelReply(text="说点什么吧。", denied=True)

        # 通道没有「会话结束」这回事，/quit 在这里是噪音。
        if text in ("/quit", "/exit", "/q"):
            return ChannelReply(
                text="这里退不出我 —— 想收尾去终端敲 /quit。", denied=True
            )

        with self.app.lock:
            reply = self._run(chat_id, self._canonical(sender_ids), text)
        return ChannelReply(text=self._clip(reply), choices=self._last_choices)

    @staticmethod
    def _canonical(sender_ids: str | Iterable[str]) -> str:
        """记进 ``_ChannelCtx`` 的那一个标识。

        取值顺序 ``open_id`` → ``user_id`` → ``union_id``：优先用**应用级**
        的那个，因为它一定是飞书发的消息里最权威、最常用的标识；实在没有才
        退化。这里只影响「结果推回哪个会话」的记账，**不是**授权判据 ——
        授权在 :meth:`is_allowed`，那边看全部三层。
        """
        if isinstance(sender_ids, str):
            return sender_ids
        ids = [i for i in sender_ids if i]
        for wanted in ("ou_",):
            for candidate in ids:
                if candidate.startswith(wanted):
                    return candidate
        return ids[0] if ids else ""

    def _run(self, chat_id: str, sender_id: str, text: str) -> str:
        from ..cli.app import Repl, _ChannelCtx  # 局部导入：cli 层较重

        buffer = io.StringIO()
        repl = self._repls.get(chat_id)
        if repl is None:
            repl = Repl(self.app, out=buffer)
            self._repls[chat_id] = repl
            while len(self._repls) > self.max_chats:
                self._repls.popitem(last=False)   # 丢最久没用的那个会话
        else:
            self._repls.move_to_end(chat_id)

        # **每条消息都要重绑 out**。踩过的坑（真在飞书里撞出来的）：
        # 上面只在**首次**创建 Repl 时把 ``buffer`` 传进构造函数，而 Repl 把它
        # 存成 ``self.out``。于是复用的 Repl 仍然往**第一次那个** buffer 写 ——
        # 那个对象早就没人引用了 —— 而本条消息新建的 buffer 永远是空的。
        # 后果不是「偶尔丢一句」，而是「**同一个会话只有第一条有回复，之后全沉默**」。
        #
        # 复用 Repl 本身是对的：每个 chat 保留一个 Repl 才有连续对话（待追问的
        # 半截输入、命令历史都在里面）。所以修法是重绑输出目标，而不是每次新建。
        repl.out = buffer
        # 清空上一轮的选项：每条消息都要重设，否则上一句触发的按钮卡
        # 会**粘**到这一句上 —— 而这两句可能毫无关系。
        repl._last_choices = ()
        # 每条消息都重设来源：一个群里多个被授权的人，发送者是会变的。
        # 有了它，``/delegate`` 才能把「结果推回哪个会话」记在事务上 ——
        # 闭环靠的就是这个。
        repl.channel_ctx = _ChannelCtx(chat_id=chat_id, sender_open_id=sender_id)

        # check_reminders=False：提醒由 due_reminders() 单独推。
        # 顺带消费的话，一条无关消息就能把提醒吞掉（见模块 docstring）。
        repl.handle(text, check_reminders=False)
        self._last_choices = tuple(getattr(repl, "_last_choices", ()) or ())
        return buffer.getvalue().strip()

    @staticmethod
    def _clip(text: str) -> str:
        if len(text) <= MAX_REPLY_CHARS:
            return text
        dropped = len(text) - MAX_REPLY_CHARS
        return text[:MAX_REPLY_CHARS] + f"\n…（还有 {dropped} 字被截断）"

    # -- 提醒推送 ----------------------------------------------------------- #
    def due_reminders(self) -> ReminderBatch:
        """取出当前所有到期提醒，**不落去重令牌**。

        **由传输层按节奏调用**（比如每分钟一次）。送达成功后必须调
        :meth:`confirm_reminders` —— 失败不调，那条提醒下一轮还会来。

        以前这里是「取走即消费」，于是通道必须自己保证调用频率，
        否则提醒会丢（那句警告还写在 docstring 里）。现在取与确认分开了。
        """
        from ..cli import render

        with self.app.lock:
            digest = self.app.reminders.due()
            text = render.render_digest(digest)
        return ReminderBatch(text=text, digest=digest if text else None)

    def confirm_reminders(self, batch: ReminderBatch) -> None:
        """确认这批提醒已送达，落去重令牌。**送达成功之后才调。**"""
        if batch.digest is None:
            return
        with self.app.lock:
            self.app.reminders.acknowledge(batch.digest)

    # -- 事件去重 ----------------------------------------------------------- #
    def _is_duplicate(self, event_id: str) -> bool:
        """飞书会重投事件。不去重的话「记一下买牛奶」会变成两条事务。

        委托给注入进来的 :class:`Deduplicator`。**白名单检查必须排在它前面**
        （见 :meth:`handle`）—— 否则陌生人可以用垃圾事件把去重表撑爆，
        而这张表在生产里是落盘的，那是别人家的磁盘。

        默认实现只在本进程内有效，重启就忘；桥接注入的是 ``feishu/dedup.py``，
        跨重启记得住 —— 因为长连接重连会重投近期事件。详见设计方案 11.9.2。
        """
        return self.dedup.is_duplicate(event_id)
