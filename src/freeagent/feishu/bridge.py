"""飞书长连接桥接。**本模块是唯一需要 ``lark-oapi`` 的地方。**

为什么只有这里要：长连接的握手、心跳、自动重连自己手搓不划算。
而收消息之后的解析、发消息都已经在 :mod:`events` / :mod:`sender` 里
用标准库做完了。所以 SDK 依赖被限制在这一个文件、且是**可选依赖**。

## 结构：为什么必须「先入队再处理」

飞书要求事件在 **3 秒内**确认，否则会重投。而本地命令可能很慢 ——
``/today`` 会触发顺延并写库，``/delegate`` 会过白名单。所以：

    回调线程：解析 → 入队 → 立刻返回（秒回，不阻塞）
    工作线程：单线程 FIFO 串行取队列 → 执行 → 回消息

单线程而不是线程池，是为了**保序 + 免并发冲突**：同一个 chat 里
``/new`` 之后紧跟一条自然语言，第二条要看到第一条建出的角色。
多个线程并发处理就会串台，而且 SQLite 连接不是线程安全的。
"""

from __future__ import annotations

import argparse
import logging
import os
import queue
import re
import socket
import sys
import threading
from pathlib import Path
from typing import Any

from .config import (
    ConfigError,
    FeishuConfig,
    load_config,
    merged_env,
    state_home,
)
from .events import (
    IncomingMessage,
    Ignored,
    describe_sdk_shape,
    from_sdk_event,
)
from .identity import resolve_identity
from .sender import (  # noqa: E402
    _approval_card,
    _card_action_response,
    _decided_card,
    VIEW_CHOICE_ACTION,
    MENU_ACTION,
    send_menu_card,
)
from .status import (
    SEEN_SENDERS_KEY,
    StatusReporter,
    config_revision,
    record_sender,
    status_path,
)

__all__ = [
    "FeishuBridge",
    "PortLock",
    "AlreadyRunning",
    "lock_port_from_env",
    "DEFAULT_LOCK_PORT",
    "ENV_LOCK_PORT",
    "main",
]

log = logging.getLogger("freeagent.feishu")

#: 队列上限。满了就**丢最旧的**而不是无限增长 —— 助手落后几十条消息
#: 毫无价值，但把内存吃光会影响机器上别的事。
_QUEUE_MAX = 256

#: 身份探测失败后的重试退避（秒）。取值抄 openclaw 的
#: ``monitor.bot-identity.ts``：1min → 2min → 5min → 10min → 15min。
#:
#: 为什么要有：探测失败的**表现**是「群里 @ 它永远不回」，而启动日志里
#: 只有一行告警 —— 用户很难联想到「重启一下就好了」，于是要么一直等，
#: 要么去查完全无关的方向（权限？网络？白名单？）。
#: 网络抖动、飞书临时故障这类**瞬时**原因，重试就能自愈。
#: 配置错误（Secret 填错）重试确实没用，但那种情况跑 ``doctor`` 一眼就看到，
#: 不该由这个线程替用户猜。
#:
#: 刻意不设成「密集重试」：探测是网络请求，短间隔既容易撞上飞书的频率限制，
#: 也会在日志里刷出一片失败，看着像出了大事。
_IDENTITY_RETRY_DELAYS: tuple[float, ...] = (60.0, 120.0, 300.0, 600.0, 900.0)


class FeishuBridge:
    """把飞书消息接到本地助手上。**不直接持有 SDK 客户端**，只管路由。"""

    def __init__(
        self,
        channel,
        sender,
        config: FeishuConfig,
        *,
        bot_open_id: str | None = None,
        reporter: StatusReporter | None = None,
    ) -> None:
        """
        ``bot_open_id`` 是 bot 自己的 ``open_id``，**群聊 @ 门控的唯一依据**。

        传 ``None``（探测失败）时门控**失败关闭**：群里带 @ 的消息一律不响应。
        这时必须由调用方在启动时**显著告警**，否则用户会对着沉默的 bot 猜。
        见设计方案 11.9.1。

        ``reporter`` 用来把「最近见过谁」写进状态文件，供控制面显示 ——
        白名单最难的就是「不知道该填什么」，界面直接列出实际 ID 就绕过了。
        不传则不记（离线测试与 FakeBridge 都不需要）。
        """
        self.channel = channel
        self.sender = sender
        self.config = config
        self.bot_open_id = bot_open_id or None
        self.reporter = reporter
        self._seen_senders: dict[str, dict[str, object]] = {}
        self._queue: queue.Queue[IncomingMessage | None] = queue.Queue(
            maxsize=_QUEUE_MAX
        )
        self._worker: threading.Thread | None = None
        self._reminder: threading.Thread | None = None
        self._stop = threading.Event()

    # -- 入口 --------------------------------------------------------------- #
    def start_worker(self) -> None:
        self._worker = threading.Thread(
            target=self._run_worker, name="feishu-worker", daemon=True
        )
        self._worker.start()

    def stop(self) -> None:
        self._stop.set()
        self._queue.put(None)          # 叫醒正在阻塞的 take()

    # -- 回调：必须秒回 ----------------------------------------------------- #
    def on_message(self, event: Any) -> None:
        """长连接回调。**必须 3 秒内返回**，所以只解析 + 入队。"""
        try:
            parsed = from_sdk_event(event, bot_open_id=self.bot_open_id)
        except Exception:                # 解析器也不许把回调搞崩
            log.exception("解析飞书事件失败")
            return
        if parsed is None:
            return                      # 不是收消息事件，静默跳过
        if isinstance(parsed, Ignored):
            log.info("忽略一条消息：%s", parsed.reason)
            # **同时把实际结构打出来。** from_sdk_event 是照文档写的，
            # 从没在真实事件上验证过 —— 万一结构对不上，光有「缺 message」
            # 这句话是查不出根因的，机器人只会安静地不理人。
            # 只列属性名不列值：事件里有消息正文和 open_id，那是隐私。
            log.info("实际结构：%s", describe_sdk_shape(event))
            return
        # 群里必须 @ **bot 自己** 才响应 —— 否则整群聊天都会被我插嘴。
        # 判据是身份比对，不是「@ 了某个东西就算」（见设计方案 11.9.1）。
        # ``mention_note`` 带着具体原因，@ 了别人和身份未知要能分开看：
        # 前者是正常的，后者是配置问题、不修就一直不响应。
        if parsed.is_group and not parsed.mentioned:
            log.info(parsed.mention_note or "群里未 @ 机器人，不响应")
            return
        # 记下「这个人在事件里带了哪些 ID」，供界面显示（见 record_sender）。
        #
        # 刻意放在**群聊门控之后**：这里只记「主动找过 bot 的人」，不记
        # 群里所有成员。否则这张表会变成一份群成员名单 —— 既没用到
        # （要授权的是找过它的人），又让人不必要地知道自己被记了。
        #
        # 必须在**白名单之前**记：白名单没配好的人正是最需要看到自己 ID
        # 的那批人 —— 漏了他们，用户就只能去日志里翻。
        if self.reporter is not None:
            record_sender(self._seen_senders, parsed)
            self.reporter.update(**{SEEN_SENDERS_KEY: dict(self._seen_senders)})
        try:
            self._queue.put_nowait(parsed)
        except queue.Full:
            dropped = self._get_nowait()
            log.warning("队列满，丢掉最旧的一条：%s", dropped.text[:20] if dropped else "")

    def _get_nowait(self) -> IncomingMessage | None:
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            return None

    # -- 工作线程 ----------------------------------------------------------- #
    def _run_worker(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if item is None:
                break
            try:
                self._process(item)
            except Exception:
                # 一条消息处理失败不能拖垮整个通道。
                log.exception("处理消息时出错")
            finally:
                self._queue.task_done()

    def _route_answer(self, msg) -> str | None:
        """这条消息是否应该当作提问的**答案**。不是则返回 ``None``。

        返回值一律是“要回给人的话”（空字符串 = 已记下，不回话）。

        ## 这里是**新增的远程输入路径** —— 所以两个硬约束

        1. **命令永远优先**：以 ``/`` 开头的不当答案。否则用户被逼着
           回答问题，连命令都打不上 —— 而 ``/skip-question`` 这个退出通道
           依赖它能被识别。
        2. **只能写问答槽位**：调用的是 ``put_answer``，它只填答案槽位。
           打字**绝不能**回到 ``resolve``（那才是批准授权）——两路之间
           没有任何连线，所以一条消息最多能「回答问题」，绝不能
           「批准授权」。
        """
        if msg.unsupported_type:
            return None                      # 非文字不当答案
        text = (msg.text or "").strip()
        if not text:
            return None
        if text.startswith("/") and not text.startswith(SKIP_QUESTION):
            return None                      # 命令优先 —— 退出通道

        if _card_conn is None:
            return None                      # 没带库 = 不能落盘，不能猜
        from ..services.approval import ApprovalStore

        store = ApprovalStore(_card_conn, clock=_card_clock)
        who = msg.sender_open_id or ""
        pending = store.pending_question_for(who)
        if pending is None:
            return None                      # 没有挂起的提问 → 不拦

        skipping = text.startswith(SKIP_QUESTION)
        total = len(pending.spec["questions"])
        slot = store.put_answer(
            pending.credential,
            SKIP_ANSWER_TEXT if skipping else text,
            answered_by=who,
        )
        # 跳过必须**填满所有尚空的槽位**。
        #
        # 端到端测试等到的：`put_answer` 只填一个槽，所以两问的提问
        # 跳过一次只留下第 2 个空槽，执行器仍然卡在 `wait_answer(1)`。
        # 而卡上写着「不想回答可以回 /skip-question」—— 三个问题就得打三次，
        # 那不是退出通道，是小型得到。
        while skipping and slot is not None and slot < total - 1:
            slot = store.put_answer(
                pending.credential, SKIP_ANSWER_TEXT, answered_by=who,
            )
        if slot is None:
            log.info("【提问】记不上（凭据=%s）", pending.credential)
            return ("这条答案没记上 —— 可能已过期，"
                    "或不是发起那条委派的人。")

        done = store.is_complete(pending.credential)
        log.info("【提问】凭据=%s 第 %d/%d 问：%s（%s）",
                 pending.credential, slot + 1, total,
                 "跳过" if skipping else "已收到答案", who)
        # 跳过与回答必须**分开说**。
        #
        # `is_complete` 的含义是「没有空槽位」，**不是「人回答过」**。
        # 用它选文案，就会对一个全部跳过的提问说「全部答完」——
        # 那与卡上承诺了退出通道又不给人说是跳过，属于同一类夸大。
        if done:
            if skipping:
                return (f"已跳过这 {total} 个问题，不阻它。"
                        "它会按「没有答案」继续（或自己放弃）。")
            return (f"收到了，这 {total} 个问题全部答完。"
                    "opencode 继续干活。")
        return (f"收到了（第 {slot + 1}/{total} 问）。还有 "
                f"{total - slot - 1} 个问题，直接回消息答。")

    #: 显式要菜单的说法。**刻意不含 ``/help``** —— 它是文档里写明的
    #: 权威命令表，覆盖它等于用一个更差的版本换掉已经能用的东西。
    _MENU_COMMANDS = ("/menu", "/菜单", "/start-menu")

    #: 打招呼 / 问「你能做什么」的说法。
    #:
    #: **匹配方式按语言分开**，这是实测踩出来的：
    #: 中文没有词边界，所以「你好啊，今天有什么」该按**包含**匹配；
    #: 但英文必须按**词边界**匹配 —— 用包含的话，``hi`` 会命中
    #: ``chip``／``this``／``while``，于是「把 chip 寄存器改一下」这种
    #: 正经任务会被当成人打招呼，弹一张菜单卡出来。
    #: 一个把 chip 当成打招呼的助手，比没有菜单更糟。
    _MENU_WORDS_CJK = (
        "你好", "您好", "哈喽", "在吗", "在不在",
        "帮助", "怎么用", "你能做什么", "能做什么", "菜单",
    )
    _MENU_WORDS_ASCII = ("hi", "hello")

    #: 英文词边界用的预编译式。正则里只出现上面那几个字面量，
    #: 所以内联编译一次存在类属性上是安全的（不是逐请求构造）。
    _MENU_RE_ASCII = re.compile(
        r"\b(?:" + "|".join(re.escape(w) for w in _MENU_WORDS_ASCII) + r")\b",
        re.IGNORECASE,
    )

    @classmethod
    def _is_greeting(cls, text: str) -> bool:
        """这句话像不像在打招呼／问「你能做什么」。"""
        if any(w in text for w in cls._MENU_WORDS_CJK):
            return True
        # 纯中文消息里不该出现裸英文词，但混排是常态，所以仍然只按词边界判。
        return cls._MENU_RE_ASCII.search(text) is not None

    def _send_entry_menu(self, msg) -> None:
        """把入口菜单卡发进聊天窗口。发卡失败只记日志，不抛。"""
        try:
            send_menu_card(
                self.sender,
                open_id=msg.sender_open_id,
                subject="要做什么？",
                items=_menu_entries(),
                note="点一个就行；也可以直接把话说给我听。",
                chat_id=msg.chat_id,
            )
            log.info("【菜单】已发入口卡：chat=%s", msg.chat_id)
        except Exception:
            log.warning("【菜单】发入口卡失败", exc_info=True)

    @classmethod
    def _menu_kind(cls, text: str) -> str | None:
        """这句话要不要走菜单。``None`` = 不是菜单消息。

        **纯判断，不碰任何外部状态。** 这一步刻意放在白名单与去重之前：
        若对「不是菜单」的消息也去调 :meth:`Deduplicator.is_duplicate`，
        那条事件就被提前记下了，接着 ``channel.handle`` 会把自己刚记的那条
        当成重投而拒掉 —— 于是**每一条普通消息都会被当重复事件拒绝**。
        所以「先分类，再过闸门」的顺序是硬要求，不是风格问题。
        """
        low = text.lower()
        if low in cls._MENU_COMMANDS:
            return "menu"
        if "角色" in text and any(
            k in text for k in ("有什么", "有哪些", "都有", "什么", "列表", "哪个")
        ):
            return "roles"
        if cls._is_greeting(low):
            return "menu"
        return None

    def _route_menu(self, msg) -> str | None:
        """这句话是否应该在进派发逻辑**之前**被菜单接走。

        返回值口径与 :meth:`_route_answer` 一致：``None`` = 不拦，
        空串 = 已处理且不用再回话，非空 = 已处理且要把这段话回过去。

        ## 为什么必须在这里拦，而不是让 ``channel.handle`` 自己处理

        实测踩到的：用户说「你有什么角色？」，而当时挂着一条待答的提问，
        于是这句话被 :meth:`_route_answer` **当成答案吃掉**了 ——
        用户问的是「有哪些角色」，系统收到的是「答案：你有什么角色？」。
        而「你好」则落到 ``channel.handle``，被判成「请补充角色」，
        把角色名做成按钮发出去，像在填一张它从没申请过的表。

        也就是说这两句用户**没有一次是在派发**，却都被派发 machinery 处理了。
        菜单的第一职责就是给「我没在派发」一个明确的落点。

        ## 白名单与去重：菜单插在前面，所以这两道闸门要自己补

        两道闸门原本都在 :meth:`ChannelService.handle` 里面，而菜单插在它
        **前面** —— 于是菜单路径把它们一起绕过了。两个实测后果：

        1. **白名单外的人发「你好」也会收到菜单卡。** 回一句就等于向陌生人
           确认「这里有个 bot 在跑」，而那正是白名单要挡的泄露面。
        2. **飞书重连重投同一事件时，菜单卡会再发一遍。** 去重表是在
           ``handle`` 里写的，菜单压根没走到那里。

        所以这里显式补上，与 :meth:`_reply_unsupported` 同一把尺子 ——
        不能因为「这条路径不发敏感内容」就省掉闸门：泄露面不取决于内容，
        取决于「对方知道这里有个东西在回话」。
        """
        if msg.unsupported_type:
            return None
        text = (msg.text or "").strip()
        if not text:
            return None

        kind = self._menu_kind(text)
        if kind is None:
            return None                      # 不是菜单消息：一个字都不记

        # ── 到这里已确定要发菜单，以下两道闸门必须在**发任何东西之前** ──
        if not self.channel.is_allowed(msg.sender_ids):
            log.info(
                "发送者不在白名单，菜单不回：%s",
                self.channel.allowlist_mismatch(msg.sender_ids),
            )
            # 交回 ``channel.handle`` 走标准拒绝路径：它会拒，而且**不说话**
            #（不在白名单的人连提示都不该收，见 :meth:`_reply_unsupported`）。
            return None

        key = msg.dedup_key
        if key and self.channel.dedup.is_duplicate(key):
            log.info("重复事件（%r），不重复发菜单", key)
            return ""                        # 已处理过，静默
        if not key:
            # 绝不能静默跳过：没有键就无法去重，重投会把菜单卡再发一遍。
            log.warning(
                "这条菜单消息既无 event_id 也无 message_id，**无法去重**；"
                "若被飞书重投，菜单卡会重复发出（event_id=%r message_id=%r）",
                msg.event_id, msg.message_id,
            )

        plan = _menu_dispatch(kind)
        if plan.get("roles"):
            return self._send_role_card(msg)
        text = plan.get("text")
        if text:
            # 入口卡那一项在表里是空的（它只给按钮、不给正文），所以这里
            # 实际只会有将来的表项落进来。留着是为了加新项时不必改这里。
            return text
        self._send_entry_menu(msg)
        return ""                            # 卡已说明一切，不必再回文字

    def _send_role_card(self, msg, *, open_id=None, chat_id=None) -> str:
        """打字路径的入口。**转手** :func:`_send_role_card_via`，不自己实现。

        刻意薄到只有一次调用：这一层存在的唯一理由是打字路径手里有 ``msg``、
        而那份共享实现要的是 ``open_id`` / ``chat_id``。一旦在这里再写一遍
        发卡逻辑，就又是第二份实现（设计文档 12.1.3）。

        ## 这里必须做一次 ``None`` → ``""`` 的转换

        两层的「不说话」约定**不一样**，而这个差异是有原因的：

        - 按钮路径（:func:`_run_menu`）拿到 ``None`` 就 return，**不回话**
        - 打字路径（:meth:`_route_menu`）用 ``""`` 表示「已处理、不用回话」，
          而 ``None`` 在那一层是**「没拦到」** —— 会继续往下走、交给
          ``channel.handle``

        所以这里若直接把 ``None`` 透传上去，「你有什么角色？」就会**穿透
        菜单回到派发 machinery** —— 也就是 12.1.3 修的那个分叉原样复发。
        两条既有测试当场抓住了它（`test_role_query_becomes_a_role_card`、
        `test_menu_short_circuits_before_dispatch`）。

        教训记下来：**在注释里写下「要做什么」不等于做了。** 上一版我把
        这句转换写进了 docstring 却没实现，而测试抓到的正是这个缺口。
        """
        problem = _send_role_card_via(
            sender=self.sender,
            open_id=open_id if open_id is not None else msg.sender_open_id,
            chat_id=chat_id if chat_id is not None else msg.chat_id,
        )
        return "" if problem is None else problem

    def _process(self, msg: IncomingMessage) -> None:
        if msg.unsupported_type:
            # 不支持的消息类型：回一句提示，但**绝不进派发逻辑**。
            # 见设计方案 11.9.1「不支持的消息类型要回一句提示，不能沉默」。
            self._reply_unsupported(msg)
            return
        # 去重键用 ``msg.dedup_key``（``event_id`` → ``message_id`` 兜底），
        # **不是** ``msg.event_id or None``。
        #
        # 踩过的坑（实测撞出来的，不是推的）：飞书**确实会**投递不带
        # ``event_id`` 的事件。那种情况下传 None 进去，
        # ``ChannelService.handle`` 里的去重整段被跳过 —— 而飞书重连时会
        # **重投**事件，于是同一条消息会**再建一次事务**。
        #
        # 证据：实测收到一条消息、处理成功、也回了话，但去重表条目数没动，
        # 说明那次的 ``event_id`` 是空的、去重压根没跑。
        #
        # 这个坑在非文字那条路径上**已经修过一次**（见 :meth:`_reply_unsupported`
        # 的注释），当时只改了那一处，文字这条漏了 —— 同一个坑要踩两次。
        # ``dedup_key`` 这个属性就是为此存在的（设计方案 11.9.2）。
        key = msg.dedup_key
        if not key:
            # 绝不能静默跳过（和 :meth:`_reply_unsupported` 同一把尺）：
            # 没有键就无法去重，重投会重复建事务，而用户只看到「怎么有两条」。
            log.warning(
                "这条消息既无 event_id 也无 message_id，**无法去重**；"
                "若被飞书重投，会重复处理（chat=%s，event_id=%r message_id=%r）",
                msg.chat_id, msg.event_id, msg.message_id,
            )
        # 先问：这条是不是回答提问的文字。命令优先，非文字不答。
        answered = self._route_answer(msg)
        if answered is not None:
            if answered:
                # 走现有的发送口径，不自己新建一个。
                try:
                    self.sender.send_text(msg.chat_id, answered)
                except Exception:  # noqa: BLE001 - 回复失败不得拖垮处理
                    log.exception("回复提问回执失败（chat=%s）", msg.chat_id)
            return

        # 再问：这句话要不要走菜单。**必须在 channel.handle 之前** ——
        # 「你好」「你有什么角色」都不是派发；让派发 machinery 去处理，
        # 换来的就是一张用户从没申请过的表（详见 :meth:`_route_menu`）。
        menu = self._route_menu(msg)
        if menu is not None:
            if menu:
                try:
                    self.sender.send_text(msg.chat_id, menu)
                except Exception:  # noqa: BLE001 - 回复失败不得拖垮处理
                    log.exception("回菜单提示失败（chat=%s）", msg.chat_id)
            return

        reply = self.channel.handle(
            msg.chat_id, msg.sender_ids, msg.text,
            event_id=key or None,
        )
        if reply.denied:
            # **被拒时必须打出发送者的每一层标识 + 白名单内容。**
            #
            # 理由很实际：白名单填错时的表现是「bot 一句话都不回」，而用户
            # 拿不到任何可操作的信息。原先只打一个 open_id，够用但不够 —— 换过
            # 飞书应用之后 open_id 变了，光看它无法判断「是它变了」还是
            # 「我配错了」。把双方的对照一起打出来，这一行就成了自证。
            #
            # 仍然**不打用户的消息正文** —— 正文可能有隐私内容，而排查
            # 「谁被拒了」只需要身份。
            #
            # 但要把**bot 自己那句回复**带上：``denied`` 有四种来源
            # （白名单 / 重复事件 / 空输入 / 通道不支持 ``/quit``），
            # 而这一行原来一律写成「发送者不在白名单」—— 四种里只有一种
            # 真是白名单。排查空输入时看到「不在白名单」，会误判成白名单
            # 配错了，而那与白名单毫无关系（实测踩过：日志与原因不符，
            # 查的方向整个是错的）。
            #
            # 带上之后一眼可分：``没有权限。``= 白名单，``''``= 重复事件，
            # ``说点什么吧。``= 空输入。记的是**回复**不是用户输入，
            # 所以不违反上面那条隐私规矩。
            detail = self.channel.allowlist_mismatch(msg.sender_ids)
            if msg.event_id:
                log.info(
                    "未派发（bot 回复=%r）：%s（%s，event_id=%s）",
                    reply.text[:40], msg.sender_label, detail, msg.event_id,
                )
            else:
                log.info(
                    "未派发（bot 回复=%r）：%s（%s）",
                    reply.text[:40], msg.sender_label, detail,
                )
            if not reply.text:
                return                    # 重复事件：静默，不打扰
        elif not reply.text:
            # **没被拒、却算出空回复** —— 这是异常，必须出声。
            #
            # 踩过的坑（真在飞书里撞出来的）：原先空回复一律 `return`，和
            # 「重复事件」混在一起。可那正是 ``ChannelService._run`` 少绑一次
            # ``repl.out`` 时的症状 —— **同一会话第 2 条起全部沉默**。
            # 两个缺陷叠在一起：症状是沉默，沉默又不留日志，于是**完全无法
            # 被诊断**，我为此白查了一轮。
            #
            # 所以这里分开：重复事件是**预期**（``denied=True``），空回复是
            # **不该发生**，后者要 warning。
            log.warning(
                "处理了消息却算出**空回复**，已不给用户回任何话（chat=%s，"
                "发送者=%s，event_id=%s）。这是异常不是重复事件 —— "
                "若用户说「发了没反应」，先看这里。",
                msg.chat_id, msg.sender_label, msg.event_id,
            )
            return
        else:
            # 成功也**必须留痕**。原先成功时一行都不打，于是「bot 回了什么 /
            # 到底回了没有」在日志里完全不可见 —— 用户报「没反应」时，
            # 日志是空的，和「消息压根没到」长得一模一样。
            #
            # 只记元信息、**不记正文**：回复里会带用户自己的话（任务标题等），
            # 而这个文件明确不打消息正文（见上面「被拒」分支的说明）。
            # 排查「回了没有 / 回了多长」不需要正文。
            log.info(
                "已回复：chat=%s，发送者=%s，%d 字",
                msg.chat_id, msg.sender_label, len(reply.text),
            )

        # **有只读选项时改发按钮卡**（设计文档 12.1.1 的 A 方案）。
        #
        # 只在 ``CLARIFY`` 时才有选项 —— 那是「我没把握，请选一个」的时刻，
        # 也就是唯一值得花一次交互成本去问的时刻。正常回答照旧发纯文本，
        # 否则飞书会变成「每条消息一张卡」。
        #
        # 卡片里**仍然把正文也发一遍**（``send_text``），因为发卡可能失败，
        # 而「点了没反应」比「多点一次」糟得多。
        if reply.choices:
            from .sender import send_view_choice_card

            try:
                send_view_choice_card(
                    self.sender,
                    open_id=msg.sender_open_id,
                    subject="你要看哪个？",
                    choices=reply.choices,
                    chat_id=msg.chat_id,
                )
                log.info("已发选项卡：chat=%s，%d 个选项", msg.chat_id,
                         len(reply.choices))
            except Exception:  # noqa: BLE001 - 发卡失败不该让消息变没反应
                log.warning("发选项卡失败，回退成纯文本", exc_info=True)
        self.sender.send_text(msg.chat_id, reply.text)

    def _reply_unsupported(self, msg: IncomingMessage) -> None:
        """回一句「我只处理文字」，然后就此打住。

        群聊 @ 门控在 :meth:`on_message` 入队前就过了（没 @ 到 bot 的根本
        不会走到这里），所以这里只剩**白名单**这一道。

        白名单**必须**复用 :meth:`ChannelService.is_allowed`，不在这里自己
        比一遍 allowed 列表 —— 两份白名单逻辑迟早会漂移，而漂移的表现是
        「提示语漏给了陌生人」：那等于向任意人确认「这里有个 bot 在跑」，
        正是白名单要挡的泄露面。

        踩过的坑（**实测发现的**，离线测试全绿）：去重发生在
        :meth:`ChannelService.handle` 里面，而这里刻意不走 ``handle``
        （它只处理可派发的正文）。于是非文字消息**从不写去重表** ——
        飞书重连重投同一张图时，就会**再回一次**提示。
        症状很隐蔽：文字消息不会重复，图片会，去重表里也查不到那条记录。
        所以这里必须显式补记一次，否则「跨重启去重」这个承诺对图片是假的。
        """
        # 先记去重再决定回不回：判定本身就是「处理」，处理过就该被记住。
        #
        # 键用 ``dedup_key``（event_id → message_id 兜底），**不再用裸
        # ``event_id``**：原写法 ``if msg.event_id and ...`` 在 event_id 为空
        # 时把整段跳过了 —— 回了提示却不写表，同一张图重投会再回一次。
        # 而离线 fixture 的 event_id 一直都有，所以测试全绿。
        key = msg.dedup_key
        if key and self.channel.dedup.is_duplicate(key):
            log.info(
                "重复事件（%r），不重复回提示（key=%s）",
                msg.unsupported_type, key,
            )
            return
        if not key:
            # **绝不能静默跳过**（设计方案 11.9.2）。没有键就无法去重，
            # 这条提示在重投时会重复发出 —— 用户只看到「同一张图被回了两次」。
            # 明确报出来，至少能查。
            log.warning(
                "这条 %r 消息既无 event_id 也无 message_id，**无法去重**；"
                "若它被飞书重投，提示会重复发出（event_id=%r message_id=%r）",
                msg.unsupported_type, msg.event_id, msg.message_id,
            )
        if not self.channel.is_allowed(msg.sender_ids):
            log.info(
                "发送者不在白名单，%r 消息不回提示：%s（event_id=%s）",
                msg.unsupported_type,
                self.channel.allowlist_mismatch(msg.sender_ids),
                msg.event_id,
            )
            return
        log.info(
            "收到 %r 消息（发送者 %s），回一句「只处理文字」而不建事务",
            msg.unsupported_type, msg.sender_label,
        )
        self.sender.send_text(
            msg.chat_id,
            f"我只处理文字消息，这条收到的是 {msg.unsupported_type}。"
            "图片、文件、富文本请直接用文字说。",
        )

    # -- 身份恢复 ------------------------------------------------------------ #
    def start_identity_recovery(self, sender, home=None) -> None:
        """身份探测失败后，**后台按退避重试**，成功即让群聊门控生效。

        没有这条时的表现：启动那一瞬网络抖了一下（或飞书临时故障），
        探测失败 → 群里 @ 永远不回 → 用户以为配置坏了，去查权限、查网络、
        查白名单 —— 而真正原因是「你运气不好，重启一下就好了」。

        成功后直接写 ``self.bot_open_id``：``on_message`` 每条消息都现读这个
        属性，所以**下一次消息就按新身份判**，不用重启、不用重新订阅。
        赋值是原子的（``str | None`` 单引用），而读取方只会拿到「旧值或新值」，
        不会出现半截字符串。

        退避用 ``self._stop.wait(delay)`` 而不是 ``time.sleep(delay)``：
        前者被 ``stop()`` 立刻唤醒，桥接退出时不用干等完整个退避周期。
        """
        def _retry() -> None:
            for i, delay in enumerate(_IDENTITY_RETRY_DELAYS, 1):
                if self._stop.wait(delay):
                    return                     # 桥接在等退避期间关了，别再探
                try:
                    identity, note = resolve_identity(sender, home=home)
                except Exception:
                    # 探测本身不该把重试线程带走 —— 一次意外就放弃等于白做。
                    log.exception("身份后台重试 %d 出了意外", i)
                    continue
                if identity is not None:
                    self.bot_open_id = identity.open_id
                    log.warning(
                        "bot 身份已在后台探到（%s，%s）。"
                        "**群里 @ 门控现在生效了**，不用重启。",
                        identity.open_id, note,
                    )
                    return
                log.warning(
                    "身份后台重试 %d/%d 仍失败：%s",
                    i, len(_IDENTITY_RETRY_DELAYS), note,
                )
            log.warning(
                "身份后台重试用尽，群里 @ 门控保持失败关闭直到下次启动。"
                "跑 `python -m freeagent.feishu.doctor` 查这一项 —— "
                "如果每次都失败，多半是凭据或权限配错了，不是网络问题。"
            )

        threading.Thread(
            target=_retry, name="feishu-identity-retry", daemon=True
        ).start()

    # -- 提醒推送 ----------------------------------------------------------- #
    def start_reminders(self) -> None:
        """定时把到期提醒推进「home chat」。

        刻意由通道自己推：若靠用户发消息顺带触发，一条无关消息就能把
        提醒吞掉。
        """
        if self.config.reminder_poll <= 0:
            return
        self._reminder = threading.Thread(
            target=self._run_reminders, name="feishu-reminder", daemon=True
        )
        self._reminder.start()

    def _run_reminders(self) -> None:
        while not self._stop.wait(self.config.reminder_poll):
            try:
                batch = self.channel.due_reminders()
                # 送达失败就不确认 —— 令牌不落库，下一轮还会提醒。
                # 宁可重复提醒，也别静默吞掉一条（设计文档 9.2）。
                if batch.text and self._notify_operators(batch.text):
                    self.channel.confirm_reminders(batch)
            except Exception:
                log.exception("推送提醒时出错")

    def _notify_operators(self, text: str) -> bool:
        """推给每个白名单用户。**返回是否至少有一个送达。**

        这里推**私聊**而不是某个群：提醒是给「你」的，落到工作群里
        等于把私事广播出去。要在群里收提醒，得用 bot 私聊。

        空文本直接返回 ``False``：``FakeSender`` 之类的替身不会像真 sender
        那样自己跳过空消息，那样它们就会收到一条空消息 —— 而真实用户看到的
        是一次毫无意义的「对方发来一条空白」。

        ## 为什么必须返回送达与否
        以前这里把每个用户的发送异常各自吞掉、且不报结果，于是调用方
        无论如何都当成功 —— 全部失败时提醒被永久标记成已送达，用户从没收到。
        一个人收到了就算送达：提醒是私事，转告给白名单里另一个人也算到了。
        """
        text = (text or "").strip()
        if not text:
            return False
        delivered = False
        for user in self.config.allowed_users:
            try:
                # 按条目形状选 receive_id_type：白名单现在同时接受
                # ou_ 开头的 open_id 和裸的租户级 user_id，写死成 open_id
                # 会让后者永远发不出去（见 send_to_allowlist_entry）。
                self.sender.send_to_allowlist_entry(user, text)
            except Exception:
                log.exception("推送给 %s 失败", user)
            else:
                delivered = True
        return delivered


# --------------------------------------------------------------------------- #
# 单实例守卫
# --------------------------------------------------------------------------- #

#: 默认锁端口。刻意避开 Web 的 8770 —— 两个进程撞端口会互相误杀。
DEFAULT_LOCK_PORT = 8771
ENV_LOCK_PORT = "FEISHU_LOCK_PORT"


class AlreadyRunning(RuntimeError):
    """已经有桥接在跑。"""


class PortLock:
    """bind 一个回环端口当单实例锁。

    ## 为什么必须单实例

    同一个 ``app_id`` 上跑两个长连接，飞书只会把事件投给其中一个，
    另一个静默失效。现场表现是「bot 时好时坏」，而且极难定位 ——
    两边日志都正常，没有报错。

    ## 为什么用端口而不是锁文件

    锁文件在进程被强杀时会留下残骸，下次启动就得人工清理；
    端口由操作系统在进程退出（含崩溃）时**自动释放**。
    端口的生命周期就是进程的生命周期，不需要额外的判断逻辑。
    """

    def __init__(self, port: int) -> None:
        self.port = int(port)
        self._sock: socket.socket | None = None

    def acquire(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", self.port))
        except OSError as exc:
            sock.close()
            raise AlreadyRunning(
                f"端口 {self.port} 已被占用 —— 大概率已经有一个桥接在跑了。"
                f"（{exc.strerror}）"
                f"确实要开第二个的话，换一个端口：set {ENV_LOCK_PORT}=8781"
            ) from exc
        # 刻意**不** listen。占端口只需要 bind：第二次 bind 会拿到
        # WSAEADDRINUSE / EADDRINUSE，排他性就已经建立了（``TestPortLock``
        # 逐条锁住这个行为）。listen 只会多开一个毫无用途的监听套接字 ——
        # 别人还能 connect 上来，虽然绑在回环地址不外露，但没必要留着。
        self._sock = sock

    def release(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def __enter__(self) -> "PortLock":
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def lock_port_from_env() -> int:
    """锁端口。默认 8771；``FEISHU_LOCK_PORT`` 可改。

    给 doctor 复用 —— 「端口被占」这条诊断结论必须和桥接实际用的是
    **同一个**解析逻辑，否则会出现「doctor 说没占、桥接说占了」。

    走 :func:`merged_env` 而不是直接读 ``os.environ``：锁端口也在
    ``feishu.env`` 的白名单里，而界面存进去的值要是读不到，那这半个功能
    就是假的（见 12.7）。
    """
    raw = merged_env().get(ENV_LOCK_PORT, "").strip()
    if not raw:
        return DEFAULT_LOCK_PORT
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{ENV_LOCK_PORT} 必须是整数，收到 {raw!r}") from exc


# --------------------------------------------------------------------------- #
# 装配
# --------------------------------------------------------------------------- #
def _build_handler(bridge: FeishuBridge) -> Any:
    """注册消息回调。**这里才 import SDK**，所以不装也不影响其它模块。"""
    import lark_oapi as lark

    builder = lark.EventDispatcherHandler.builder("", "")
    builder.register_p2_im_message_receive_v1(bridge.on_message)
    builder.register_p2_card_action_trigger(_card_action_sdk)
    return builder.build()


#: 卡片动作事件名。SDK 报的错里就是这个串（实测：「processor not found,
#: type: card.action.trigger」），记下来做交叉核对。
CARD_ACTION_EVENT = "card.action.trigger"


#: 卡片处理器用的连接。**由 :func:`main` 显式注入**。
#:
#: 为什么是模块级：SDK 的回调是**零参可调用对象**，不接受额外参数，
#: 而我们要往里传数据库连接。用模块全局是这个 SDK 形状下的常规做法，
#: 但**必须显式设、且未设时明确降级** —— 不能偷偷新建连接，那会开第二个
#: 连接去写同一个库，绕开 app.lock。
#:
#: 踩过的坑（实测）：它挂在**函数**上时，``def`` 语句还没执行完，
#: 同一处的 ``_card_action.conn: Any = None`` 会在名字绑定之前就求值，
#: 直接 ``NameError`` 把整个模块打挂。挂函数属性必须**放在 def 之后**。
_card_conn: Any = None
#: 退出通道。以 ``/`` 开头，并被当成答案，所以它不会被当命令。
SKIP_QUESTION = "/skip-question"
#: 跳过时填入槽位的内容。必须让 agent 能区分「本人答了」与「本人跳过了」。
SKIP_ANSWER_TEXT = "（用户跳过了这个问题，没有作答）"


#: 卡片处理器的钟。``None`` = 用真钟。
#:
#: 为什么单独留一个口：处理器会**自己**建 ``ApprovalStore``，而那个类默认
#: 用 ``datetime.now()``。于是测试注入的 FakeClock 只对它自己那个 store 生效，
#: 处理器这边仍看真时间 —— 结果「新卡」被判成过期，decided_by 写成 expired。
#:
#: 踩过的坑（实测）：4 条测试红了，读起来像「决策被翻掉」「卡片颜色错」，
#: 真正的原因只是**两个 store 用了两个钟**。
_card_clock: Any = None


def _no_card_change(why: str) -> dict[str, Any]:
    """**刻意不动卡片**，但给一个 toast。

    只在「我们连这张卡是什么都不知道」时用（载荷不认、处理崩了）。
    那时替换卡片等于凭空编一张卡片出来 —— 用户会以为之前那张真的处理过。
    留 toast 至少让他看到「点了，但没成」。

    刻意**不带** ``card`` 字段：SDK 那个字段为 None 时不替换。
    """
    return {"toast": {"type": "error", "content": f"未处理：{why}"}}


def _card_action(data: Any = None) -> dict[str, Any]:
    """卡片点击 → 把答复落库。**这里不做任何决定**，只写。

    载荷形状是**实测**出来的（前期验证，见 docs 11.9.4）::

        event.action.value = {"action": "allow_once", "id": "<凭据>"}
        event.operator      = {open_id, user_id, union_id, tenant_key}
        event.context       = {open_message_id, open_chat_id, ...}
        header.event_id     = 去重键

    三个刻意的决定：

    1. **凭据查不到就丢弃，不报错。** 那几乎总是「过期后有人点老卡片」，
       属正常。但**要记日志** —— 否则「我点了没反应」无从排查。
    2. **点击者身份要记下来**（``decided_by``），哪怕不做白名单校验。
       凭据是 uuid4 不可猜，真正要防的是「谁能发起请求」，那在 ask 之前
       就卡住了；而「谁批的」必须在事后查得到。
    3. **不吞异常**，但也不让它掀翻桥接 —— 卡片处理失败不该影响
       消息处理。返回 ``{}`` 表示「不更新卡片」，是最安全的应答。
    """
    import json

    log = logging.getLogger("freeagent.feishu")

    def _plain(obj: Any, depth: int = 0) -> Any:
        """把 SDK 的 model 对象**逐层拆成普通结构**。

        第一版用 ``json.dumps(default=str)``，结果 ``action`` 被打成
        ``<CallBackAction object at 0x...>`` —— 形状拿到了，**值没拿到**，
        于是凭据串在日志里 0 处。所以这里必须真的去读属性。
        """
        if depth > 4:
            return "…"
        if isinstance(obj, (str, int, float, bool)) or obj is None:
            return obj
        if isinstance(obj, dict):
            return {str(k): _plain(v, depth + 1) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [_plain(v, depth + 1) for v in obj]
        if hasattr(obj, "__dict__"):
            return {k: _plain(v, depth + 1) for k, v in vars(obj).items()
                    if not k.startswith("_")}
        # 没有 __dict__ 的 model 对象：按 dir() 试读公开属性
        out: dict[str, Any] = {}
        for name in dir(obj):
            if name.startswith("_"):
                continue
            try:
                v = getattr(obj, name)
            except Exception:
                continue
            if callable(v):
                continue
            out[name] = _plain(v, depth + 1)
        return out or str(obj)

    try:
        payload = _plain(data)
        # 载荷可能压根不是 dict（None / 列表 / 字符串）。不判一下的话
        # 下面 ``payload.get`` 会抛，而那一抛会被下面的 except 兜住 —— 于是
        # 每来一条乱七八糟的帧就往日志里打一段 traceback。
        # **垃圾输入不该产生堆栈。**
        if not isinstance(payload, dict):
            payload = {}
        event = payload.get("event") or {}
        value = (event.get("action") or {}).get("value") or {}
        operator = event.get("operator") or {}
        who = operator.get("open_id") or operator.get("user_id") or "unknown"
        event_id = (payload.get("header") or {}).get("event_id")

        # **只读选项**：不需要审批凭据，点了就跑那个视图并回话。
        #
        # 刻意放在最前面、且**不查库** —— 它是纯只读切换，没有副作用，
        # 因此没有东西需要授权（见 ``sender.VIEW_CHOICE_ACTION`` 的说明）。
        # 放进批准流程会是错配：凭空多出「凭据过期」「决策不可篡改」
        # 这些为「授权」设计的机制，却用在一个根本没有授权的动作上。
        if value.get("action") == VIEW_CHOICE_ACTION:
            # 模块级函数，**没有 self** —— 踩过的坑：这里原先写成
            # ``self._run_view_choice(...)``，类型检查直接报
            # 「self is not defined」，而它要等到**真机上点一下按钮**
            # 才会炸成 NameError。
            return _run_view_choice(value, who=who)

        # **菜单**：同样无状态、同样不查库，但它比只读选项多一步 ——
        # 它要**回一句话或另一张卡**到聊天窗口，所以依赖通道而不是就地渲染。
        # 放在只读选项之后、批准流程之前，理由与它相同：菜单没有
        # 「过期即拒」的安全含义，混进凭据流程是错配。
        if value.get("action") == MENU_ACTION:
            return _run_menu(value, who=who)

        credential = value.get("id")
        choice = value.get("action")

        # ``stop_delegation`` 必须在白名单里，否则点了会被这条静默丢弃 ——
        # 而症状是「点了没反应」，那正好是「停止」最不能有的表现。
        if not credential or choice not in (
            "allow_once", "deny", "stop_delegation",
        ):
            log.info("【卡片动作】载荷不认（credential=%r choice=%r）—— 丢弃",
                     credential, choice)
            return _no_card_change("载荷不认，未处理")

        if _card_conn is None:
            # 没注入连接 = 桥接没带着库起来。这条路不能猜：宁可拒，
            # 也不能在没落盘的情况下报「已允许」。
            log.warning("【卡片动作】没有数据库连接，拒绝写入（凭据=%s）",
                        credential)
            return _card_action_response(
                _decided_card("无法处理", "**已拒绝**：助手没连上数据库，"
                                          "没有记录你的选择。", granted=False),
                "error", "没连上数据库，未能记录",
            )

        from ..services.approval import ApprovalStore

        store = ApprovalStore(_card_conn, clock=_card_clock)
        known = store.get(credential)
        if known is None:
            # 过期后点老卡片。正常，但得留痕 —— 而且要**告诉用户**，
            # 否则他点了什么都没发生，只会觉得「这机器人坏了」。
            log.info("【卡片动作】凭据 %s 不在库里（多半已过期）—— "
                     "点击者=%s 丢弃", credential, who)
            return _card_action_response(
                _decided_card("这张卡已过期",
                              "**已过期，未生效。** 过期后点它不会批准任何操作。",
                              granted=False),
                "warning", "这张卡已过期",
            )

        # ── 「停止这条委派」：两件事都要做，缺一不可 ──────────────────────
        #
        # 1) 先把**当前这一次**回掉（按拒绝）—— 否则执行器会一直等它到
        #    TTL（委派档 1800s = 半小时），而 opencode 那边也挂着一个
        #    没人回答的请求。那不是「停止」，那是「都卡住」。
        # 2) 再记下「停整条」—— 只做 1 的话 agent 只会换个方向继续，
        #    而用户点的是「停止这条委派」。
        #
        # 判定**在 resolve 之前**：停止比允许更强，不该让任何人停掉别人的
        # 委派。注意别把它写成「因为 resolve 之后就晚了」—— 实测
        # ``can_answer`` 只判发起人、不看是否已决定，所以晚一点也能过。
        # 真正的理由是**权限**：这一判定属于「谁有权」，必须在动手前做。
        if choice == "stop_delegation":
            if not store.can_answer(credential, who):
                log.info("【卡片动作】停止：点击者 %s 不是发起人（凭据=%s）—— 丢弃",
                         who, credential)
                return _card_action_response(
                    _decided_card("无法停止", "**只有发起人能停止这条委派。**", granted=False),
                    "warning", "你不是这条委派的发起人",
                )
            store.resolve(credential, "deny", decided_by=who)
            store.request_stop(credential, by=who)
            log.info("【卡片动作】用户叫停（凭据=%s 点击者=%s）", credential, who)
            return _card_action_response(
                _decided_card("已停止", "**已叫停这条委派。**\n"
                              "agent 不会再动手；已经完成的那几步改动仍然有效。"),
                "success", "已停止这条委派",
            )

        wrote = store.resolve(
            credential,
            "allow" if choice == "allow_once" else "deny",
            decided_by=who,
        )
        # **不是发起人**要单独判、单独说 —— 它和「已决定过」「已过期」在
        # :meth:`resolve` 里都是 ``False``，但给用户的话**完全不同**。
        #
        # 混成一句「先前已经 X 过了」的后果是：白名单里的另一个人点了
        # 别人发起的委派，屏幕上写「这张卡先前已被允许」—— 他会以为
        # 系统记错了，而实际上**是他自己没权限**。那条规则
        # （只有发起人能批）在 resolve 里强制，这里只负责说清楚。
        if not store.can_answer(credential, who):
            requester = known.requested_by or "(未记录)"
            log.info("【卡片动作】凭据 %s 的发起人是 %s，点击者=%s —— "
                     "非发起人，丢弃", credential, requester, who)
            return _card_action_response(
                _decided_card(
                    known.subject,
                    f"**这次点击没有生效** —— 只有**发起这条委派的人**能批。"
                    f"发起人是 `{requester}`。",
                    granted=False,
                ),
                "warning", "只有发起人能批这条",
            )

        # **不能拿「resolve 没报错」当成「用户点的那个被记下了」。**
        #
        # 踩过的坑（实测）：``resolve()`` 对**两种**情况都返回 True ——
        # 真的记下了 allow，**或者**已过期于是降级成 deny。于是
        # 「写了就 granted=True」会让**过期的卡点「允许」显示成绿头
        # 「已允许」** —— 恰恰与实际相反的那种谎。
        #
        # 所以：**结论一律以库里那个 decision 为准**，不用返回值、
        # 也不用用户点了哪个按钮来推。
        settled = store.get(credential)
        prior = settled.decided_by if settled is not None else None

        # 已决定过 → 这次点击没写入任何东西，必须**如实说没生效**。
        if not wrote:
            already = settled.decision if settled is not None else None
            label = "允许" if already == "allow" else "拒绝"
            log.info("【卡片动作】凭据 %s 已被决定过（%s by %s）—— 本次点击不生效",
                     credential, label, prior)
            return _card_action_response(
                _decided_card(
                    known.subject,
                    f"**这次点击没有生效** —— 这张卡先前已被**{label}**"
                    f"（{prior or '未知'}）。决策只在第一次点击时定下。",
                    granted=(already == "allow"),
                ),
                "warning", f"先前已经{label}过了",
            )

        granted = bool(settled is not None and settled.decision == "allow")
        expired = prior == "expired"

        if expired:
            log.info("【卡片动作】凭据 %s 已过期，点「%s」不生效", credential, choice)
            return _card_action_response(
                _decided_card(
                    known.subject,
                    "**已过期，未生效。** 这张卡过了有效期，"
                    "点它不会批准任何操作。",
                    granted=False,
                ),
                "warning", "这张卡已过期",
            )

        log.info("【卡片动作】%s 凭据=%s 主题=%r 点击者=%s event_id=%s",
                 "允许" if granted else "拒绝",
                 credential, known.subject, prior, event_id)
        return _card_action_response(
            _decided_card(
                known.subject,
                (f"**已允许**（{prior}）\n\n{known.detail or ''}" if granted else
                 f"**已拒绝**（{prior}）\n\n没有访问任何东西。"),
                granted=granted,
            ),
            "success" if granted else "error",
            "已允许" if granted else "已拒绝",
        )
    except Exception:
        log.exception("【卡片动作】处理失败（不影响消息通道）")
        return _no_card_change("处理时出错")


#: 模块级持有 ChannelService，供 :func:`_run_view_choice` 用。
#: 与 :data:`_card_conn` 同理：卡片回调是**模块级** handler，拿不到实例。
#: 刻意复用**同一个** ChannelService 而不是新建一个 —— 新建会绕开去重表，
#: 而去重是「同一句话重发不会重复回」的唯一保证。
_card_channel: Any = None

#: 模块级持有 sender，供 :func:`_run_view_choice` 把结果发成**聊天消息**。
_card_sender: Any = None


def _role_names() -> list[str]:
    """当前角色名（不含已合并的）。**读不到就返回空，不猜。**"""
    if _card_conn is None:
        return []
    from ..storage.repos import RoleRepo

    try:
        return [r.name for r in RoleRepo(_card_conn).list_all(include_inactive=True)]
    except Exception:
        log.exception("【菜单】列角色失败")
        return []


def _menu_entries() -> list[tuple[str, str]]:
    """入口菜单项。

    **刻意只4 项。** 选项越多越没人选（选择过载），而这四项覆盖了
    「我现在到底想干什么」的全部常见答案。每加一项都要问一句
    「真的会有人点它吗」。
    """
    return [
        ("委派给 opencode", "delegate"),
        ("我有哪些角色", "roles"),
        ("今天该做什么", "today"),
        ("我能做什么", "help"),
    ]


def _run_menu(value: dict[str, Any], *, who: str) -> dict[str, Any]:
    """点了菜单项 → 把对应的内容发回聊天窗口，然后把按钮收掉。

    与 :func:`_run_view_choice` 同构（发气泡 + 收按钮），但多���个分支：
    菜单项不全是视图，有些是「给你一段该发的话」。

    「角色」那一支刻意**转手交给** :func:`_run_view_choice`，而不是自己查库
    渲染 —— 角色视图的语义必须与直接打 ``/role <名字>`` 逐字一致，
    另写一套就是第二份实现（设计文档 12.1.1：一份能力一份实现）。
    """
    choice = str(value.get("choice") or "").strip()
    chat_id = str(value.get("chat") or "").strip()
    if not choice:
        return _no_card_change("选项不完整，未处理")
    if _card_sender is None:
        return _no_card_change("没连上通道，未处理")

    # **白名单必须在这里问一次**（这是我自己的漏洞，不是原有代码的问题）。
    #
    # 下面除 ``role:`` 那一支外，其余分支都直接调 ``_card_sender.send_text``，
    # **不经过 :meth:`ChannelService.handle`** —— 而白名单判定住在那里。
    # 于是菜单卡在**群里**是全员可见的，群里非白名单成员点一下「我能做什么」
    # 就收到了回话。
    #
    # 后果不只是体验差：回一句话就等于向白名单外确认「这里有个 bot 在跑」，
    # 那正是白名单要挡的泄露面 —— 与 :meth:`_reply_unsupported` 同一把尺子。
    #
    # ``role:`` 那支转手 :func:`_run_view_choice`，它内部会过 ``handle()``，
    # 所以本来就安全；这里**照样先问**：白名单只在一处判定，将来改分支才
    # 不会漏掉这一处。
    if _card_channel is None:
        return _no_card_change("没连上通道，未处理")
    if not _card_channel.is_allowed(who):
        log.info("【菜单】点击者不在白名单，不回话也不执行（who=%r）", who)
        return _no_card_change("没权限，未处理")

    if choice.startswith("role:"):
        name = choice[len("role:"):].strip()
        if not name:
            return _no_card_change("选项不完整，未处理")
        return _run_view_choice({"view": f"/role {name}", "chat": chat_id}, who=who)

    # **从这一行往下，查的是 :func:`_menu_dispatch` 那张表**——
    # 与打字路径同一个来源（设计文档 12.1.3）。第一版这里写的是自己的
    # if/else，于是「我有哪些角色」在两条路上长成两副面孔。
    plan = _menu_dispatch(choice)
    if not plan:
        return _no_card_change(f"未知的菜单项：{choice[:20]}")

    if plan.get("roles"):
        # 发角色卡需要 sender，而这里只有模块级的 ``_card_sender``。
        # **不新建第二个发卡实现**——复用 :func:`_send_role_card_via`。
        # 第一版就是在这里「没有卡片能力可用」而退回文本的，代价是同一个
        # 动作两种行为。
        #
        # 返回 ``None`` = 卡已发出。这时候**一个字都不再说**：新卡就在下面，
        # 再补一句「点一下就行」既教用户操作、又指向他已经在看的那张卡。
        # 卡面照旧回写成「已打开」，让点击有反馈、按钮不再可点。
        problem = _send_role_card_via(open_id=who, chat_id=chat_id)
        if problem is None:
            return _card_action_response(
                _decided_card("已打开", "**已收到**，角色列表在下面。",
                              granted=True),
                "success", "已打开",
            )
        text = problem                      # 发卡失败：把原因回给用户
    elif plan.get("view"):
        # 有现成只读渲染的一律转手 _run_view_choice，菜单只是把它送进去 ——
        # 于是输出与「直接打那条命令」逐字一致（12.1.1 的硬约束）。
        return _run_view_choice(
            {"view": plan["view"], "chat": chat_id}, who=who,
        )
    else:
        text = plan.get("text") or ""

    if not text:
        # 表里这一项既没有可点角色也没有正文：说明它该给的是卡片而不是文字，
        # 而卡片已经在上面发过了。刻意**不说话**，别用一句废话填掉。
        return _card_action_response(
            _decided_card("已打开", "**这一项没有可显示的内容。**",
                          granted=True),
            "success", "已打开",
        )

    try:
        _card_sender.send_text(chat_id, text)
    except Exception:
        # 气泡发不出去**不能**掀翻整个点击：卡面回写照旧，用户至少看得见。
        log.warning("【菜单】内容发不进聊天（choice=%r）", choice, exc_info=True)
        return _card_action_response(
            _decided_card("没发出去", "**没能把内容发到聊天里**，请直接发消息。",
                          granted=False),
            "error", "没发出去",
        )
    return _card_action_response(
        _decided_card("已打开", "**已发在上面**，照着做就行。", granted=True),
        "success", "已打开",
    )


def _menu_help_text() -> str:
    """「我能做什么」。**给命令本身，不教命令。**

    规范性约束（设计文档 12.1.3）：菜单项**不许以「发 /xxx ……」结束**——
    菜单存在的意义是「识别优于回忆」，回一句命令让用户自己去打，等于把菜单
    刚省掉的那一步又塞回去。

    所以下面每一行都是**可以直接照着发的那句话**，不是说明书。
    """
    return (
        "常用操作（**照着发就行**）：\n"
        "· 今天该做什么 → 发 `/today`\n"
        "· 记一件事 → 发 `/new <角色> | <描述>`\n"
        "· 有哪些角色 → 点上面「我有哪些角色」那张卡里的角色名\n"
        "· 交给 opencode 改代码 → 点「委派给 opencode」，会给你要改的东西\n"
        "\n完整命令表发 `/help`。"
    )


def _menu_today_text() -> str:
    """「今天该做什么」。

    刻意**不**在这里直接调视图渲染：那份渲染属于 :class:`ChannelService`，
    而 12.1.1 规定同一能力只能有一份实现。这里转成 `/today` 再走
    :func:`_run_view_choice`，于是菜单与「直接打 /today」的输出**逐字一致**——
    分叉的表现极隐蔽（设计文档 12.1.1 记录过一次真实分叉）。
    """
    return "__VIEW__/today"


def _menu_role_names() -> list[str]:
    return _role_names()


def _send_role_card_via(*, open_id: str, chat_id: str, sender=None) -> str | None:
    """发「选一个角色」卡。**唯一一份发卡实现**（设计文档 12.1.3）。

    两条入口都调它：打字路径经 :meth:`FeishuBridge._send_role_card` 转手，
    按钮路径直接调。第一版没有这个函数——发卡逻辑长在 :class:`FeishuBridge`
    上、只能用 ``self.sender``，而按钮路径是模块级函数、手里只有模块级的
    ``_card_sender``，于是那一支「没有卡片能力可用」就退回了纯文本。

    结构性差异值得说明白：``self.sender`` 与模块级 ``_card_sender`` 是**两个
    实例**（第一个进程里由 bridge 注入，第二个由 ``_card_action`` 侧注入）。
    所以这个函数**必须显式收 sender**，不能自己去摸某个全局 —— 否则又变成
    「谁先跑谁说了算」，而那种分叉只在特定启动顺序下出现，极难查。

    返回值刻意用 ``str | None`` **在类型上区分两件事**：

    - ``None`` —— 卡片**已发出**，调用方不该再回任何文字
    - ``str``  —— 发卡失败，这句是要回给用户的话

    ## 为什么不用空串当「成功」

    第一版返回 ``""`` 表示「卡已发出、不用回话」，而调用方写的是
    ``if not text: 回一句兜底``。于是 ``""`` 既是成功哨兵、又被当成
    「意外为空」——**一个值两种含义**，成功路径和处理失败长得一模一样。

    真机上的表现：点「我有哪些角色」后，入口卡回绿并写「卡已经发在上面了，
    点一下就行」，而下面就是那张角色卡。**教用户去点一张他已经在看的卡。**

    而且它违反了本项目自己定的两条规范：菜单不教用户操作、菜单不许把人踢回
    打字。写成 ``None`` 之后，「卡已发出」这件事**根本没法**被误当成需要
    回话的情况。

    两处的约定不同，这里刻意保持清楚：按钮路径要 ``None``（不发言），
    打字路径的 :meth:`FeishuBridge._send_role_card` 转成 ``""``——
    那个位置用 ``""`` 表示「不用回话」是**既有**约定，不是同一层语义。
    """
    target = sender if sender is not None else _card_sender
    if target is None:
        return "没连上通道，暂时发不了卡，稍后再试。"
    names = _role_names()
    if not names:
        return "读不到角色（助手可能没连上数据库），稍后再试。"
    try:
        send_menu_card(
            target,
            open_id=open_id,
            subject="选一个角色",
            items=[(n, f"role:{n}") for n in names[:20]],
            note="按角色看它下面的事务。",
            chat_id=chat_id,
        )
    except Exception:  # noqa: BLE001 - 发卡失败不该让消息变没反应
        log.warning("【菜单】发角色卡失败", exc_info=True)
        return "你的角色：" + "、".join(names[:20])
    return None


def _menu_dispatch(choice: str) -> dict[str, Any]:
    """菜单动作 → **行为**的唯一分派表（规范性，设计文档 12.1.3）。

    打字路径（:meth:`FeishuBridge._route_menu`）与按钮路径
    (:func:`_run_menu`) **查同一张表**。加一个菜单项 = 加一行，不是加两处。

    第一版没有这张表，于是「我有哪些角色」有两副面孔：打字问得到**卡片**，
    点按钮得到**文本 + 一句「发 /role <名字>」**。两条路的单测各自都绿——
    因为各自的测试只覆盖自己那条路。这正是 12.1.1「入口不得自建第二份判流
    逻辑」要防的东西。

    形态只有两种，因为它们对应两件本质不同的事：

    - ``{"view": ...}`` —— 有现成只读渲染，走 :func:`_run_view_choice`。
      菜单只是把它送进去，于是输出与「直接打那条命令」逐字一致。
    - ``{"text": ...}`` —— 没有现成渲染，只能给文字。**但必须直接给结果**，
      不许教用户去打字。
    """
    if choice == "today":
        return {"view": "/today"}
    if choice == "help":
        return {"text": _menu_help_text()}
    if choice == "roles":
        # 角色**列表**没有现成视图（/roles 的渲染在 ChannelService 里），
        # 所以给按钮而不是文字：点角色名 → /role <名字> → 走只读视图。
        # 12.1.3 明确不做「角色少就发文本」的自适应——那会让「点角色」这个
        # 能力随数据量时有时无，而那次突变不会被任何测试撞到。
        return {"roles": True}
    if choice == "delegate":
        return {"text": _menu_delegate_text()}
    return {}


def _menu_delegate_text() -> str:
    """「委派给 opencode」。

    给的是**一条填好真实角色名、只留两处尖括号待填**的可复制命令，不是
    命令语法说明。仍然要说清「白名单」和「单行」这两件踩过坑的事 ——
    那是会直接导致失败的事实，不是可以省的客套。
    """
    names = _menu_role_names()
    role = names[0] if names else "<角色>"
    return (
        "把下面这条发给我（`<>` 里的换成你要的）：\n\n"
        f"`/delegate <项目绝对路径> | {role} | <要做的事，一句话>`\n\n"
        "两处要换：项目路径得在 `config.json` 的 `delegate.projects` 白名单里；"
        "需求写成**单行**（换行会被 opencode 判成复杂任务而失败）。"
    )


def _run_view_choice(value: dict[str, Any], *, who: str) -> dict[str, Any]:
    """点了只读选项 → 跑那个视图，结果发回聊天窗口，然后把按钮收掉。

    结果走**两条路**，缺一不可：

    1. ``send_text`` 发一个**聊天气泡** —— 用户在窗口里等的就是这个。
       只靠卡片回写的话，他得自己发现「刚才那张卡变了」。
    2. 卡片回写把按钮**收掉** —— 留着按钮等于骗人，他会以为还能再点，
       而第二次点只会得到同一份数据（实测过的坑：留着可点的按钮＝骗人）。

    复用**同一个** :class:`ChannelService`：只读视图的语义必须与「你直接
    在飞书里打『今天该做什么』」**逐字一致**。另写一套渲染就是第二份实现，
    而设计文档 12.1.1 明写「一份能力一份实现」—— 分叉的表现极隐蔽
    （Web 正常、飞书错，而两边各自都测过）。
    """
    view = str(value.get("view") or "").strip()
    chat_id = str(value.get("chat") or "").strip()
    if not view or not chat_id:
        return _no_card_change("选项不完整，未处理")
    if _card_channel is None or _card_sender is None:
        # 没有通道 = 没人能执行。**不猜**、不发话：只把卡收掉。
        log.warning("【选项卡】没连上通道，不处理（view=%r chat=%r）", view, chat_id)
        return _card_action_response(
            _decided_card("看不了", "**没连上，暂时看不到数据。**", granted=False),
            "error", "没连上",
        )

    try:
        with _card_channel.app.lock:
            reply = _card_channel.handle(chat_id, who, view)
    except Exception:
        log.exception("【选项卡】执行视图失败（view=%r）", view)
        return _card_action_response(
            _decided_card("看不了", f"**没能跑成**「{view}」。稍后再试。",
                          granted=False),
            "error", "没跑成",
        )

    if reply.denied:
        # 拒了（不在白名单/去重）。**不当成成功**回写 ——
        # 那会让卡面显示「已切换」而用户什么都没收到。
        log.info("【选项卡】通道拒了（view=%r denied=True）", view)
        return _no_card_change("没权限，未处理")

    try:
        _card_sender.send_text(chat_id, reply.text)
    except Exception:
        # 气泡发不出去**不能**掀翻整个点击：卡面回写照旧，用户至少看得见。
        log.warning("【选项卡】结果发不进聊天（view=%r）", view, exc_info=True)

    return _card_action_response(
        _decided_card("已切换", f"**{view}** —— 结果已发在上面。", granted=True),
        "success", "已切换",
    )


def _card_action_sdk(data: Any = None) -> Any:
    """把 :func:`_card_action` 的 dict 答复包成 SDK 声明的类型。

    ``register_p2_card_action_trigger`` 的签名要求 handler 返回
    ``P2CardActionTriggerResponse``，而 :func:`_card_action` 返回
    ``dict[str, Any]`` —— **运行时没事**（SDK 的 Encoder 照样能把 dict
    序列化出去，这也是之前能收到 1226 字节应答的原因），但类型上是违约的。

    为什么不直接让 ``_card_action`` 返回模型：那 30+ 个 ``return
    _card_action_response(...)`` 全都要包一层，测试也跟着全要改成读
    ``.toast``/``.card`` 属性，可读性反而变差。**在这一层包一次**就够了。

    为什么不 ``cast`` 蒙过去：``cast`` 是对类型系统说谎 —— 声称是模型，
    实际是 dict；哪天 SDK 真的去读模型属性就会炸。这里是真的构造一个模型。

    包一层安不安全？实测过（``tests/test_card_sdk_envelope.py``）：
    SDK 的 ``Encoder`` 对每个 model 做 ``filter_null(vars(o))``，逐层剥掉
    ``None``，所以模型多出来的 ``toast.i18n=None`` **不会**发出去。
    同一个测试断言 ``JSON.marshal(model) == JSON.marshal(dict)``，
    JSON 形状逐字节相同 —— 不是"应该没问题"，是"证明了没问题"。
    """
    from lark_oapi.event.callback.model.p2_card_action_trigger import (
        P2CardActionTriggerResponse,
    )

    return P2CardActionTriggerResponse(_card_action(data))



#: SDK 在真正连上 / 断开时打的日志片段（``lark_oapi/ws/client.py``）。
#:
#: 为什么盯日志而不自己推断：``WSClient`` **没有**「连上了」的回调。它只有
#: ``on_reconnecting`` / ``on_reconnected`` 两个**重连**钩子，而初次连接
#: 不走它们。唯一确证的信号是它自己打出来的日志。
#:
#: logger 名字是 ``"Lark"``（实测，不是我猜的模块路径）。
_SDK_LOGGER_NAME = "Lark"
_SDK_CONNECTED = "connected to "
_SDK_DISCONNECTED = "disconnected to "


class _ConnectionWatcher(logging.Handler):
    """把 SDK 的连接日志翻译成状态上报。

    **只报确证**：没看到「connected to」就报 `connected=False`。宁可让界面
    显示「正在连接」，也不要显示一个假的「已连接」—— 后者比没有状态更坏，
    因为它给出虚假的安心（见 :mod:`freeagent.feishu.status` 的模块说明）。
    """

    def __init__(self, reporter: StatusReporter, ready_state: str) -> None:
        super().__init__(level=logging.INFO)
        self._reporter = reporter
        self._ready_state = ready_state

    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        # ⚠️ 顺序是**故意的**：`disconnected to` 里含 `connected to` 子串。
        # 先判断开，否则一次断线会被当成重新连上 —— 正好是这个 bug 的翻版。
        if _SDK_DISCONNECTED in msg:
            self._report(connected=False, state="starting")
        elif _SDK_CONNECTED in msg:
            self._report(connected=True, state=self._ready_state)

    def _report(self, *, connected: bool, state: str) -> None:
        try:
            self._reporter.update(state=state, connected=connected)
        except Exception:            # noqa: BLE001
            # 这是第三方事件循环里的日志回调。这里抛出去只会让 logging 往
            # stderr 刷一串 "--- Logging error ---"，把真正的日志淹掉。
            pass


def _run_connection(
    client: Any,
    reporter: StatusReporter | None,
    bot_open_id: str | None,
) -> None:
    """跑长连接，**在真正连上之后**才把状态报成 ready。

    踩过的坑（会造成假健康）：原实现是在 ``client.start()`` **之前**就
    ``connected=True`` + 打「长连接已建立」，而文档还声称有个「短延时确认
    start() 没抛异常」的机制——代码里根本没有。于是凭据错误时 start() 立刻
    抛出，状态文件却仍写着 ready/connected，心跳照旧，界面就一直显示
    「已连接」，对着一个压根没连上的进程报健康。

    ``client.start()`` 的实际形状是：先 ``await self._connect()``（此刻
    连上了才会打 ``connected to``），**然后**才进阻塞的 ``_select()``。
    所以确证信号只能从日志里拿，见 :class:`_ConnectionWatcher`。
    """
    if reporter is None:
        client.start()
        return

    # 还没开始连。先落一个「连着吗还不知道」的状态，而不是先宣布就绪。
    reporter.update(state="starting", connected=False)

    watcher = _ConnectionWatcher(
        reporter, "ready" if bot_open_id else "degraded"
    )
    sdk_log = logging.getLogger(_SDK_LOGGER_NAME)
    # 状态**依赖**这一条日志，所以必须保证 level 放得过 INFO。
    #
    # 踩过的坑：原先指望 SDK 构造 Client 时会把级别设成调用方给的那个值
    # （bridge 传的是 INFO，于是碰巧能用）。可一旦有人把日志级别调成
    # WARN/ERROR，「connected to」就不打了，状态会**永远**停在「正在连接」——
    # 一个自己引入的静默失效，而且只在改日志级别时才发作。
    #
    # 只在会挡住时**调低到** INFO；调用方主动要更啰嗦的（DEBUG）时不按回去。
    if sdk_log.level == logging.NOTSET or sdk_log.level > logging.INFO:
        sdk_log.setLevel(logging.INFO)
    sdk_log.addHandler(watcher)
    try:
        client.start()                      # 连上时 watcher 会翻成 ready
    except Exception as exc:                # noqa: BLE001 - 报出去再照旧抛出
        # 启动就失败时必须说清，别留一个 ready 的假状态在盘上。
        try:
            reporter.update(
                state="down", connected=False, last_error=f"连接失败：{exc}"
            )
        except Exception:                   # noqa: BLE001
            pass
        raise
    finally:
        # start() 正常返回意味着事件循环结束了；一直阻塞时不会走到这。
        sdk_log.removeHandler(watcher)


def _log_path(db: str | None) -> Path:
    """日志文件放哪：与身份缓存、事件去重表**同源**。

    复用 :func:`state_home`（而不是自己拼 ``~/.freeagent``）：它已经处理了
    ``FREEAGENT_HOME`` 与 ``--db`` 跟随，且明确禁止另写一份路径解析 ——
    复制粘贴的解析迟早漂移，而漂移的表现是「缓存写在 A、读取去 B 找」，
    于是缓存永远命中不了，**且没有任何报错**。

    契约见设计文档 11.9.5。
    """
    return state_home(Path(db).parent if db else None) / "feishu.log"


def _log_handlers(args: argparse.Namespace) -> list[logging.Handler]:
    """stderr + **UTF-8** 文件。

    踩过的坑（实测）：原先这里只写 ``logging.basicConfig(level=..., format=...)``，
    它只建 ``StreamHandler(stderr)``，**没有 FileHandler**。于是
    ``feishu.log`` 的内容是**启动器把 stderr 重定向**进去的 ——
    而重定向的编码取决于**启动那个 shell 的代码页**：

    ====================  ==========
    启动方式                写出的中文
    ====================  ==========
    UTF-8 终端              UTF-8
    中文 Windows 的 cmd(936)  **GBK**
    ====================  ==========

    后果不是「日志不好看」，是**排障会被骗**：实测 9826 行合法 UTF-8 里
    混着 4 行 GBK，而那 4 行恰好是两次真实点击（``【卡片动作】允许`` /
    ``【卡片动作】拒绝``）。用 UTF-8 grep「卡片动作」**一条都搜不到** ——
    险些反过来断定「实机证据是假的」。

    所以编码**必须显式钉死** ``utf-8``，且**不许依赖任何隐式默认**。

    ``encoding`` 写死而不是跟随 locale：跟随 locale 等于把这个 bug 换个
    地方复现。日志是给人（和工具）读的，跨机器可读比「合本地口味」重要。

    **stderr 保留**：控制台仍要看得到日志。文件是为了**事后**能查。

    建文件失败**不许炸桥接** —— 记一条 stderr 告警然后只留 stderr。
    日志是诊断手段，为了写日志而拒绝启动是本末倒置。
    """
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    path = _log_path(getattr(args, "db", None))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(path, encoding="utf-8"))
    except OSError as exc:
        # 此时 logging 还没配好，print/stderr 是唯一能说话的地方。
        print(f"⚠ 日志文件开不了（{path}：{exc}），日志只走 stderr", file=sys.stderr)
    return handlers


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="freeagent-feishu",
        description="把本地助手接到飞书/Lark（长连接，无需公网 IP）",
    )
    parser.add_argument("--db", default=None, help="数据库路径（默认 ~/.freeagent）")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=_log_handlers(args),
        # ``force=True`` **不是**为了覆盖别人的配置，而是为了不漏句柄。
        #
        # ``basicConfig`` 在 root logger 已有 handler 时是**静默 no-op** ——
        # 于是 :func:`_log_handlers` 建出来的 ``FileHandler`` 既没被装上、
        # 也没人关，``ResourceWarning: unclosed file`` 就漏出来了。
        # 表现很误导：报的是「文件没关」，根因却是「配置压根没生效」。
        # 本函数是进程入口（CLI/被 supervisor 拉起），本就**该**由它决定日志。
        force=True,
    )

    try:
        config = load_config()
        config.check_ready()
        lock_port = lock_port_from_env()
    except ConfigError as exc:
        print(f"配置有问题：{exc}", file=sys.stderr)
        return 2

    # 单实例守卫放在最前面：连库都不必打开就能发现「已经有一个在跑」。
    lock = PortLock(lock_port)
    try:
        lock.acquire()
    except AlreadyRunning as exc:
        print(f"起不来：{exc}", file=sys.stderr)
        return 4

    # 资源清理集中在一个 ``finally``。此前有两条出错路径会漏：
    # 第二个 ``ImportError``（SDK 装了一半、``lark_oapi.ws`` 缺失时）没放锁，
    # 而非 ``ImportError`` 的异常连 ``app.close()`` 都不走。漏放锁的后果很
    # 难查 —— 「桥接起不来，但看不出为什么」，而这正是这道守卫唯一的作用。
    #
    # 意外的异常**不吞**：让它带着 traceback 冒出来，只是资源照样收干净。
    app = None
    bridge = None
    reporter = None
    try:
        from ..app import build_app
        from ..services.channel import ChannelService
        from .dedup import SeenEventStore, default_path
        from .sender import FeishuSender

        app = build_app(args.db)
        sender = FeishuSender(config)

        # 卡片处理器要用**同一个**库连接 —— 不新建第二个。
        # 新建会绕开 app.lock，于是「桥接写答复 / 执行器读答复」变成两个
        # 连接各写各的，锁形同虚设（实测会撞 SQLite 的 busy）。
        #
        # 必须放在 ``build_app`` **之后**：原来写在前面，于是「假装缺 SDK」
        # 那条测试直接 ``AttributeError: 'NoneType' has no attribute 'conn'``
        # —— 而它本该验的是「缺依赖时要给安装提示」。
        global _card_conn
        _card_conn = app.conn
        # 选项卡（只读视图切换）要用**同一个** ChannelService 与 sender。
        # 与 _card_conn 同理：卡片回调是模块级 handler，拿不到实例。
        global _card_channel, _card_sender
        _card_channel = ChannelService(app, allowed_senders=config.allowed_users)
        _card_sender = sender

        # 状态文件跟着 ``--db`` 走：改了库的位置，去重记录和身份缓存也跟着走，
        # 不会「换了一套数据却还记着旧的事件 id」。
        home = state_home(Path(args.db).parent if args.db else None)

        seen = SeenEventStore(default_path(home))
        if seen.last_error:
            log.warning("事件去重表：%s", seen.last_error)

        # bot 身份：群里「只认 @ 自己」的唯一依据。探不到就失败关闭，
        # 所以下面必须**显著告警**——否则用户只见群里静默，无从判断原因。
        #
        # 踩过的坑（类型检查抓出来的，1076 个测试全都漏了）：
        # ``resolve_identity`` 返回的是 ``BotIdentity`` **对象**，而
        # ``FeishuBridge`` 要的是 ``str``。之前直接把对象传进去，
        # ``self.bot_open_id`` 存下的是对象，于是 events.py 里
        # ``mention_open_id == bot_open_id`` 恒为 False ——
        # **真实环境里 bot 在群里永远不会被 @ 叫醒，而且一声不吭**。
        # 测试抓不到是因为每条测试都直接注入 ``"ou_bot"`` 这样的裸字符串，
        # 从没走过 ``main()`` 这段真实装配。
        identity, note = resolve_identity(sender, home=home)
        bot_open_id = identity.open_id if identity is not None else None
        if bot_open_id is None:
            log.warning(
                "⚠ %s\n"
                "  私聊不受影响，但**群里 @ 它也不会回**，直到探测成功。\n"
                "  我会在后台按退避重试（最多约 33 分钟），探到就自动恢复，"
                "不用你重启。若每次都失败，跑 "
                "`python -m freeagent.feishu.doctor` 看这一项 —— "
                "那通常是凭据或权限配错，不是网络问题。",
                note,
            )
        else:
            log.info("bot 身份 %s（%s）", bot_open_id, note)

        channel = ChannelService(
            app, allowed_senders=config.allowed_users, dedup=seen
        )

        # 运行状态上报（设计方案 12.7）。先报「还没连上」—— 用户在控制面
        # 看到 starting 比看到一片空白好，那片空白会被当成「界面坏了」。
        #
        # 刻意**建在桥接之前**：桥接要把「最近见过谁」写进状态文件，需要
        # 拿着 reporter 的引用（见 record_sender 的说明）。先后反了就得回头
        # 补一个 setter，那比挪两行更啰嗦。
        reporter = StatusReporter(status_path(home))
        reporter.update(
            state="starting",
            connected=False,
            bot_open_id=bot_open_id or "",
            bot_name=identity.app_name if identity is not None else "",
            allowed_users_count=len(config.allowed_users),
            lock_port=lock_port,
            dedup_entries=len(seen),
            last_error="",
            # 记下「这个进程是照着哪份配置起来的」。界面拿它跟盘上现在的
            # 配置比，就知道要不要提示重启（12.7）—— 少了它，界面只能显示
            # 「已保存」，而那可能根本没生效。
            config_revision=config_revision(home),
        )
        reporter.start()

        bridge = FeishuBridge(
            channel, sender, config,
            bot_open_id=bot_open_id, reporter=reporter,
        )
        if bot_open_id is None:
            # 探到了就不起这个线程：没得可重试，白白挂个线程只增加出错面。
            bridge.start_identity_recovery(sender, home=home)

        bridge.start_worker()
        bridge.start_reminders()

        import lark_oapi as lark
        from lark_oapi.ws import client as ws_client

        client = ws_client.Client(
            config.app_id,
            config.app_secret.use(),      # SDK 要裸字符串
            log_level=lark.LogLevel.INFO,
            event_handler=_build_handler(bridge),
            domain=config.base_url,
        )
        # 注意：**「Client 对象造好了」不等于「连上了」**。真正握手在
        # ``client.start()`` 里面，而它是阻塞的。所以这里先报 degraded，
        # 连上之后再报 ready —— 反过来做的话，控制面会在根本没连上时
        # 傻傻地显示「已连接」。
        reporter.update(
            state="degraded" if bot_open_id is None else "starting",
            connected=False,
        )
        log.info(
            "正在建立飞书长连接（域名 %s，授权 %d 人，提醒轮询 %s，"
            "跨重启去重已记住 %d 条，@ 门控 %s）",
            config.base_url, len(config.allowed_users),
            f"{config.reminder_poll}s" if config.reminder_poll else "关",
            len(seen),
            "按 open_id 判定" if bot_open_id else "失败关闭（身份未知）",
        )
        _run_connection(client, reporter, bot_open_id)
        return 0
    except ImportError as exc:
        print(
            f"缺少飞书依赖：{exc}\n"
            '装它：pip install ".[feishu]"',
            file=sys.stderr,
        )
        return 3
    except KeyboardInterrupt:
        log.info("收到 Ctrl+C，正在退出")
    finally:
        # 每一步都自己兜住：清理抛异常会把原始异常盖掉，还会连带跳过
        # 后面的关库和放锁 —— 那比不清理更糟。
        if reporter is not None:
            try:
                reporter.stop()
            except Exception:
                log.warning("停状态上报出错（已忽略）", exc_info=True)
        if bridge is not None:
            try:
                bridge.stop()
            except Exception:
                log.warning("停桥接出错（已忽略）", exc_info=True)
        if app is not None:
            try:
                app.close()
            except Exception:
                log.warning("关库出错（已忽略）", exc_info=True)
        lock.release()
    return 0


#: 踩过的坑：一开始漏了这个守卫，于是 ``python -m freeagent.feishu.bridge``
#: 只是导入模块、什么都不做、**退出码 0 且没有任何输出** ——
#: 看起来「启动成功了」，其实一条命令都没执行。控制台脚本
#: （``freeagent-feishu``）走的是另一条路，所以它是对的，``-m`` 这条却是哑的。
#: 联调时白等一轮才发现。
if __name__ == "__main__":
    raise SystemExit(main())
