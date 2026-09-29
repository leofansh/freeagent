"""远程通道（飞书等）共用的消息路由层。

这个模块不碰飞书、不发网络请求，全部离线可测。

最该被锁住的是三条**反直觉**的行为：

1. 白名单留空 = 全部拒绝（不是全部放行）
2. 普通消息**不能**顺带消费到期提醒 —— 那是通道自己按节奏推的
3. 命令语义与终端**同一份实现**，所以行为不许漂移
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from freeagent.app import build_app
from freeagent.services.channel import (
    MAX_REPLY_CHARS,
    ChannelService,
    Deduplicator,
    InMemoryDeduplicator,
)
from freeagent.services.clock import FrozenClock

ALLOWED = "ou_alice"
STRANGER = "ou_stranger"


@pytest.fixture()
def channel(app):
    return ChannelService(app, allowed_senders=frozenset({ALLOWED}))


def _role(app):
    """取（或建）「跑腿」角色。

    踩过的坑：一开始这里每次都 ``roles.create("跑腿")``，于是第二次调用
    撞 UNIQUE 约束 —— 失败的是测试自己，不是被测代码。
    """
    for r in app.roles.list_roles():
        if r.name == "跑腿":
            return r
    return app.roles.create("跑腿")


def _task(app, title="买牛奶", **kw):
    return app.tasks.create(title, [_role(app).id], **kw)


class TestAllowlistFailsClosed:
    """白名单是「谁能指挥本机执行」的边界，默认必须是关的。"""

    def test_empty_allowlist_denies_everyone(self, app):
        svc = ChannelService(app)          # 不传白名单
        reply = svc.handle("c1", "ou_anybody", "/today")
        assert reply.denied, "默认必须拒绝，不能默认放行"
        assert "没有权限" in reply.text

    def test_stranger_is_denied(self, channel):
        assert channel.handle("c1", STRANGER, "/today").denied

    def test_denial_does_not_leak_membership(self, channel):
        """不能说「你不在白名单」—— 那等于给陌生人一个可探测的开关。"""
        reply = channel.handle("c1", STRANGER, "/today")
        assert "白名单" not in reply.text and "不在" not in reply.text

    def test_allowed_sender_passes(self, channel):
        assert not channel.handle("c1", ALLOWED, "/today").denied

    def test_denied_message_does_not_touch_db(self, app, channel):
        before = len(app.tasks.list_all())
        channel.handle("c1", STRANGER, "记一下买牛奶")
        assert len(app.tasks.list_all()) == before


class TestParityWithTerminal:
    """通道不是「另一套命令」—— 它包住终端那一套，所以不该漂移。"""

class TestDelegationThroughChannel:
    """委派是这套系统的关键能力，通道必须能用 —— 但闸门一道都不能松。

    踩过的坑：这几个测试一开始用 ``app`` 夹具，结果 ``/delegate`` 全部失败。
    查下去不是 bug —— 白名单为空时**本来就该拒绝**。于是顺势补了
    「白名单外必须被拒」这条，它比「能用」更重要。
    """

    def _app(self, tmp_path, clock, projects):
        (tmp_path / "config.json").write_text(
            json.dumps({"delegate": {"projects": [str(p) for p in projects]}}),
            encoding="utf-8",
        )
        return build_app(tmp_path / "agent.db", clock=clock)

    def test_whitelisted_project_creates_delegation(self, tmp_path, clock):
        project = tmp_path / "proj"
        project.mkdir()
        app = self._app(tmp_path, clock, [project])
        try:
            channel = ChannelService(app, allowed_senders=frozenset({ALLOWED}))
            app.roles.create("工作")
            reply = channel.handle(
                "c1", ALLOWED, f"/delegate {project} | 工作 | 加个登录"
            )
            assert not reply.denied
            created = [t for t in app.tasks.list_all() if t.project_path]
            assert created, f"应当建出委派事务，回复是：{reply.text!r}"
            assert created[0].state.value == "inbox", (
                "建出来必须是未确认状态，通道不许直接派发"
            )
        finally:
            app.close()

    def test_project_outside_allowlist_is_refused(self, tmp_path, clock):
        """**安全属性**：白名单外的目录，哪怕消息来自白名单用户也不能委派。"""
        allowed_dir = tmp_path / "allowed"
        other_dir = tmp_path / "somewhere-else"
        allowed_dir.mkdir()
        other_dir.mkdir()
        app = self._app(tmp_path, clock, [allowed_dir])
        try:
            channel = ChannelService(app, allowed_senders=frozenset({ALLOWED}))
            app.roles.create("工作")
            reply = channel.handle(
                "c1", ALLOWED, f"/delegate {other_dir} | 工作 | 做事"
            )
            assert [t for t in app.tasks.list_all() if t.project_path] == [], (
                "白名单外的项目绝不能建出委派事务"
            )
            assert "拒绝" in reply.text, f"应明确告知拒绝，实际：{reply.text!r}"
        finally:
            app.close()

    def test_delegation_still_needs_human_start(self, tmp_path, clock):
        """通道不许绕过人类闸门：建出来但不可派发。"""
        project = tmp_path / "proj"
        project.mkdir()
        app = self._app(tmp_path, clock, [project])
        try:
            channel = ChannelService(app, allowed_senders=frozenset({ALLOWED}))
            app.roles.create("工作")
            channel.handle("c1", ALLOWED, f"/delegate {project} | 工作 | 做事")
            from freeagent.services.delegate import eligible_tasks

            assert eligible_tasks(app.task_repo, app.record_repo) == [], (
                "还没 /start 的委派绝不能出现在可派发列表里"
            )
        finally:
            app.close()

    def test_records_originating_chat_for_push_back(self, tmp_path, clock):
        """**闭环的关键**：从飞书发起的委派必须记下是哪个会话。

        执行器靠这个字段把结果推回去。没有它，「在飞书里派一件事、
        结果自己飞回来」就不成立 —— 那只是个半开环。
        """
        project = tmp_path / "proj"
        project.mkdir()
        app = self._app(tmp_path, clock, [project])
        try:
            channel = ChannelService(app, allowed_senders=frozenset({ALLOWED}))
            app.roles.create("工作")
            channel.handle(
                "oc_from_lark", ALLOWED, f"/delegate {project} | 工作 | 加个登录"
            )
            task = [t for t in app.tasks.list_all() if t.project_path][0]
            assert task.delegate_chat_id == "oc_from_lark", (
                "委派没记住来源会话，结果就回不去了"
            )
        finally:
            app.close()

    def test_terminal_delegation_has_no_chat_id(self, tmp_path, clock):
        """从终端发起的委派不该有 chat_id —— 终端本来就能看到结果。"""
        project = tmp_path / "proj2"
        project.mkdir()
        app = self._app(tmp_path, clock, [project])
        try:
            app.roles.create("工作")
            app.tasks.create(
                "加个登录", [app.roles.list_roles()[0].id],
                project_path=str(project),
            )
            task = [t for t in app.tasks.list_all() if t.project_path][0]
            assert task.delegate_chat_id is None
        finally:
            app.close()

    def test_unknown_command_says_so(self, channel):
        reply = channel.handle("c1", ALLOWED, "/nope")
        assert "未知命令" in reply.text

    def test_quit_is_not_meaningful_in_chat(self, channel):
        """/quit 在终端是退出循环，在通道里会让人以为 bot 死了。"""
        reply = channel.handle("c1", ALLOWED, "/quit")
        assert reply.denied
        assert "终端" in reply.text
        assert "再见" not in reply.text


class TestRemindersAreNotSwallowed:
    """**核心回归**。

    ``reminders.check()`` 是消费型的：写 ``REMINDER_FIRED`` 记录且不重发。
    若通道沿用终端的 ``handle()``，一条「今天该做什么」就能把到期提醒
    吞进不相干的回复里，用户**再也收不到**。所以必须走 ``check_reminders=False``。
    """

    def _due_task(self, app):
        task = _task(app, "该交周报了")
        app.tasks.set_reminder_time(
            task.id, app.clock.now() - timedelta(minutes=5)
        )
        return task

    def test_plain_message_does_not_fire_reminders(self, app, channel):
        self._due_task(app)
        reply = channel.handle("c1", ALLOWED, "/today")
        assert "[提醒]" not in reply.text, (
            "普通消息不该夹带提醒 —— 那会把它消费掉"
        )
        # 关键断言：提醒还在，没被消费
        assert channel.due_reminders(), "提醒应该还在等着被推送"

    def test_due_reminders_delivers_them(self, app, channel):
        self._due_task(app)
        pushed = channel.due_reminders()
        assert "该交周报了" in pushed

    def test_reminders_only_fire_once(self, app, channel):
        self._due_task(app)
        assert channel.due_reminders(), "第一次应推出去"
        assert channel.due_reminders() == "", "不该重复推"

    def test_reminder_flow_survives_many_messages(self, app, channel):
        """连发十条无关消息，提醒仍必须送达。"""
        self._due_task(app)
        for i in range(10):
            channel.handle("c1", ALLOWED, f"/note {self._any_id(app)} 第 {i} 笔")
        assert "该交周报了" in channel.due_reminders()

    @staticmethod
    def _any_id(app) -> str:
        return app.tasks.list_all()[0].id[:8]


class TestEventDedup:
    """飞书会重投事件；不去重就会「记一下买牛奶」变成两条事务。"""

    # 刻意用**显式** ``/new 角色 | 描述`` 而不是自然语言。
    # 踩过的坑：一开始连发两句「记一下买牛奶」「记一下买鸡蛋」，结果只建出
    # 一条 —— 因为第一句触发了「这是放到哪个脉络里？」追问，第二句被当成
    # 角色名吃掉了。那不是去重的问题，是 pending 状态在正常工作。
    def test_same_event_id_runs_once(self, app, channel):
        _role(app)
        before = len(app.tasks.list_all())
        first = channel.handle(
            "c1", ALLOWED, "/new 跑腿 | 买牛奶", event_id="ev1"
        )
        assert not first.denied
        mid = len(app.tasks.list_all())
        second = channel.handle(
            "c1", ALLOWED, "/new 跑腿 | 买牛奶", event_id="ev1"
        )
        assert second.denied, "重投事件必须被丢弃"
        assert len(app.tasks.list_all()) == mid, "不该建出第二条事务"
        assert mid > before, "第一次是真的建了"

    def test_distinct_event_ids_both_run(self, app, channel):
        _role(app)
        channel.handle("c1", ALLOWED, "/new 跑腿 | 买牛奶", event_id="ev1")
        channel.handle("c1", ALLOWED, "/new 跑腿 | 买鸡蛋", event_id="ev2")
        titles = [t.title for t in app.tasks.list_all()]
        assert len(titles) == 2, f"两个不同事件都该建出事务，实际 {titles}"

    def test_no_event_id_means_no_dedup(self, app, channel):
        _role(app)
        channel.handle("c1", ALLOWED, "/new 跑腿 | 买牛奶")
        channel.handle("c1", ALLOWED, "/new 跑腿 | 买牛奶")
        assert len(app.tasks.list_all()) == 2


class RecordingDedup:
    """记录 ``is_duplicate`` 被调了哪些 id。用来证明**顺序**。"""

    def __init__(self):
        self.seen: list[str] = []

    def is_duplicate(self, event_id: str) -> bool:
        self.seen.append(event_id)
        return False                    # 永远不重投，专测「谁先谁后」


class TestAllowlistPrecedesDedup:
    """**白名单检查必须排在去重之前。**

    反过来的话，未授权者的 ``event_id`` 会先进去重表 —— 陌生人只要狂发
    消息就能把表撑大。生产里那张表是**落盘**的（跨重启去重），于是这等于
    让别人消耗你的磁盘，而且重启后还在。

    反例现场：群里一个陌生人发 500 条消息，每条一个不同 ``event_id``，
    去重表被灌满、正常用户的事件被条数上限挤掉 → 真正的重投不再被识别
    → 「记一下买牛奶」建出两条事务。
    """

    def test_stranger_event_never_reaches_dedup(self, app):
        _role(app)
        dedup = RecordingDedup()
        ch = ChannelService(
            app, allowed_senders=frozenset({ALLOWED}), dedup=dedup
        )
        ch.handle("c1", STRANGER, "/today", event_id="ev-stranger")
        assert dedup.seen == [], (
            f"陌生人的 event_id 污染了去重表：{dedup.seen}"
        )

    def test_authorized_event_does_reach_dedup(self, app):
        """反向断言：确认 ``dedup`` 真的被接上了，不是压根没调用。"""
        _role(app)
        dedup = RecordingDedup()
        ch = ChannelService(
            app, allowed_senders=frozenset({ALLOWED}), dedup=dedup
        )
        ch.handle("c1", ALLOWED, "/today", event_id="ev-ok")
        assert dedup.seen == ["ev-ok"]

    def test_stranger_never_grows_the_table(self, app):
        """用真内存表量一下：陌生人刷 200 条，表必须**一条都不涨**。"""
        _role(app)
        ch = ChannelService(app, allowed_senders=frozenset({ALLOWED}))
        for i in range(200):
            ch.handle("c1", STRANGER, "/today", event_id=f"ev-spam-{i}")
        assert len(ch.dedup) == 0, "陌生人的事件不该进表"

    def test_denied_reply_still_sent(self, app):
        """顺序对了，但不能因此把拒绝回复也吞掉。"""
        dedup = RecordingDedup()
        ch = ChannelService(
            app, allowed_senders=frozenset({ALLOWED}), dedup=dedup
        )
        got = ch.handle("c1", STRANGER, "/today", event_id="ev-x")
        assert got.denied
        assert "没有权限" in got.text


class TestDeduplicatorInjection:
    """去重实现可替换 —— 桥接注入落盘版，测试用内存版。"""

    def test_default_is_in_memory(self, app):
        """不给就退化成内存表，也就是改造前的行为：不能起不来。"""
        ch = ChannelService(app, allowed_senders=frozenset({ALLOWED}))
        assert isinstance(ch.dedup, InMemoryDeduplicator)

    def test_injected_dedup_is_used(self, app):
        _role(app)
        dedup = InMemoryDeduplicator()
        ch = ChannelService(
            app, allowed_senders=frozenset({ALLOWED}), dedup=dedup
        )
        ch.handle("c1", ALLOWED, "/today", event_id="ev-1")
        assert len(dedup) == 1, "该走注入的那个，不是自己新建一个"

    def test_persistent_store_satisfies_protocol(self, tmp_path):
        """落盘版**必须**满足渠道层要的协议形状。

        这是分层纪律的兑现点：``feishu/dedup.py`` 和 ``services/channel.py``
        互不 import（否则会顺着 SDK 爬进核心包），靠的只是一个结构化形状。
        ``@runtime_checkable`` 的 ``isinstance`` 只查方法**存在**，签名靠类型
        检查器 —— 两者配合才完整。
        """
        from freeagent.feishu.dedup import SeenEventStore

        assert isinstance(InMemoryDeduplicator(), Deduplicator)
        assert isinstance(SeenEventStore(tmp_path / "d.json"), Deduplicator)

    def test_in_memory_dedup_dedups(self):
        d = InMemoryDeduplicator()
        assert d.is_duplicate("ev-1") is False
        assert d.is_duplicate("ev-1") is True

    def test_in_memory_dedup_respects_ttl(self):
        clock = [0.0]
        d = InMemoryDeduplicator(
            ttl_seconds=100, now=lambda: clock[0]
        )
        d.is_duplicate("ev-1")
        clock[0] = 500
        assert d.is_duplicate("ev-1") is False


class TestPerChatState:
    """终端的「这是放到哪个脉络里？」追问是有状态的；聊天是持久会话。"""

    def test_two_chats_do_not_share_pending_state(self, app, channel):
        """否则 A 群的追问会被 B 群的回答接走 —— 那是串台。"""
        app.roles.create("工作")
        channel.handle("chatA", ALLOWED, "/new | 写周报")
        channel.handle("chatB", ALLOWED, "/today")
        # 两个 chat 各自独立，B 不该把 A 的追问吃掉
        assert not channel._repls["chatA"] is channel._repls["chatB"]

    def test_same_chat_keeps_pending_followup(self, app, channel):
        """同一 chat 内追问要能续上，否则「先理一版」这类输入永远建不成。"""
        app.roles.create("家庭")
        channel.handle("c1", ALLOWED, "记一下买牛奶")
        again = channel.handle("c1", ALLOWED, "家庭")
        assert not again.denied
        # 光断言「没被拒」**太弱**，本文件里那个 buffer bug 就是从这儿漏过去的：
        # 空字符串同样满足 not denied。追问必须真的**有话可说**。
        assert again.text.strip(), "续问那条必须真有回复，不能是空的"

    def test_every_message_in_a_chat_gets_a_reply(self, app, channel):
        """回归：**同一会话的第 2 条及之后也必须拿到回复**。

        踩过的坑（真在飞书里撞出来的）：``_run`` 只在**首次**创建 ``Repl`` 时把
        ``buffer`` 传进构造函数，而 ``Repl`` 把它存成 ``self.out``。于是复用的
        Repl 一直往**第一次那个** buffer 写 —— 那对象早就没人引用 —— 本条
        消息新建的 buffer 永远是空的。

        后果不是「偶尔丢一句」，而是「每个会话只有第一条有回复，之后全沉默」。
        而它能躲过上面那条测试，正是因为那里只断言了 ``not denied``。
        """
        for i in range(4):
            reply = channel.handle("c1", ALLOWED, f"/today 第{i}次")
            assert not reply.denied, f"第 {i + 1} 条被拒了"
            assert reply.text.strip(), (
                f"第 {i + 1} 条回复是空的 —— 同一会话只有第一条能回话，"
                "说明 Repl 的输出目标没跟着换"
            )

    def test_two_messages_in_one_chat_both_reach_the_database(self, app, channel):
        """比上面那条更实一点：第二条必须**真的建出事务**，而不只是有字。"""
        app.roles.create("工作")
        channel.handle("c1", ALLOWED, "/new | 写第一份周报")
        channel.handle("c1", ALLOWED, "/new | 写第二份周报")
        titles = [r[0] for r in app.conn.execute("SELECT title FROM tasks")]
        assert len(titles) == 2, f"第二条没建成功，库里只有：{titles}"

    def test_chat_lru_is_bounded(self, app):
        """恶意或误用不能无限增长 —— 每个 chat 都留着一个 Repl。"""
        svc = ChannelService(app, allowed_senders=frozenset({ALLOWED}), max_chats=3)
        for i in range(10):
            svc.handle(f"chat{i}", ALLOWED, "/today")
        assert len(svc._repls) <= 3


class TestOutputClipping:
    def test_short_reply_untouched(self, channel):
        reply = channel.handle("c1", ALLOWED, "/today")
        assert len(reply.text) <= MAX_REPLY_CHARS

    def test_long_reply_says_it_was_truncated(self, app, channel):
        """静默截断比说清楚危险得多 —— 用户会以为看到的是全部。"""
        for i in range(60):
            _task(app, f"事务{i}" + "很长的标题" * 8)
        reply = channel.handle("c1", ALLOWED, "/all all")
        assert len(reply.text) <= MAX_REPLY_CHARS + 40
        assert "截断" in reply.text

    def test_empty_message_gets_prompt_back(self, channel):
        reply = channel.handle("c1", ALLOWED, "   ")
        assert reply.denied and reply.text


class TestNoSecretLeak:
    def test_llm_command_never_prints_key(self, app, channel, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-should-never-appear")
        reply = channel.handle("c1", ALLOWED, "/llm")
        assert "sk-should-never-appear" not in reply.text, (
            "远程通道的回复里绝不能出现 API Key"
        )
