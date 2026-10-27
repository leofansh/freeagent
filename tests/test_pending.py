"""待处理收集器（设计文档 11.18 / V1.50）的服务层、渲染与桥接落队。

与 test_personal_model.py 同一条纪律：断言按「状态正确 / 关联正确 / 计数正确」
写，而不是按「输出里有没有某个词」写。收集器最易错的地方是三件：
澄清后有没有真关联上事务、重复澄清有没有被关死、桥接收下有没有真入队 ——
这三件错了都悄无声息（列表看着挺正常）。
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from freeagent.app import build_app
from freeagent.cli.render import render_pending, render_today
from freeagent.domain import DEFAULT_ROLE_NAME, ORIGIN_CAPTURED
from freeagent.domain.enums import CapturedStatus, TaskState
from freeagent.feishu.bridge import FeishuBridge
from freeagent.feishu.events import AttachmentRef, IncomingMessage
from freeagent.services.clock import FrozenClock
from freeagent.services.sorting import EnergyWindows

#: 2026-10-21 是周三。钉住一个有具体小时的时间，标题里的时间戳才可断言。
WEDNESDAY = datetime(2026, 10, 21, 15, 0)


@pytest.fixture
def env(tmp_path):
    """一只钉在周三的 App、它自己的时钟、一条能挂事务的角色。"""
    frozen = FrozenClock(WEDNESDAY)
    application = build_app(
        tmp_path / "agent.db", clock=frozen, energy_windows=EnergyWindows()
    )
    role = application.roles.create("工作")
    try:
        yield application, frozen, role
    finally:
        application.close()


def _add(app, clock, *, kind="file", name="报表.pdf", size=2048, path="/x/a.pdf",
         key="k1", source="feishu", advance_by=60):
    """收下一条捕获。默认每次推进一分钟，保证创建序可断言（FIFO）。"""
    if advance_by:
        clock.advance(seconds=advance_by)
    return app.pending.add(
        source=source, kind=kind, media_key=key, name=name, size=size, path=path,
    )


# --------------------------------------------------------------------- 服务层
class TestPendingService:
    def test_add_lists_fifo_and_counts(self, env):
        app, clock, _role = env
        a = _add(app, clock, name="first.pdf")
        b = _add(app, clock, name="second.pdf")
        items = app.pending.pending()
        assert [i.id for i in items] == [a.id, b.id], "应按创建序（先进先出）"
        assert [i.name for i in items] == ["first.pdf", "second.pdf"]
        assert app.pending.count() == 2
        assert all(i.status is CapturedStatus.PENDING for i in items)
        assert all(i.resolved_at is None and i.task_id is None for i in items)

    def test_fifo_holds_even_when_captured_at_the_same_instant(self, env):
        """同刻收下也得按插入序 —— 回归（V1.51 修）。

        上面那条 FIFO 靠 ``_add`` 默认推进一分钟才成立，时间戳天然有差。
        真实世界不保证：一次批量发多张图、脚本导入、测试冻时钟，都能同刻
        落库。而排序原本写的是 ``created_at ASC, id ASC``，``id`` 是随机
        hex —— 同刻时顺序随机，于是「列表第 1 条」与「``/pending 1``
        澄清出来的第 1 条」可能不是同一条，用户会以为系统弄错了。

        这个坑 V1.37 在 ``task_records`` 上踩过一次（那次加了 seq 列），
        这里改用 ``rowid``（插入序），与 tasks 仓储同一个先例。
        """
        app, _clock, _role = env
        # advance_by=0 → 不推进时钟，三条必定同刻。
        names = ["one.pdf", "two.pdf", "three.pdf"]
        added = [_add(app, _clock, name=n, advance_by=0) for n in names]
        items = app.pending.pending()
        assert [i.id for i in items] == [a.id for a in added], (
            "同刻创建的顺序必须等于插入顺序"
        )

    def test_clarify_creates_task_links_item_and_empties_queue(self, env):
        app, clock, _role = env
        item = _add(app, clock, name="报价单.pdf", size=4096, path="/x/quote.pdf")
        task = app.pending.clarify(item.id)
        # 事务侧：INBOX 起步、动作类、标题取自真文件名
        assert task.state is TaskState.INBOX
        assert task.title == "报价单.pdf"
        # 来源注记只记事实（不猜内容）：来源、类型、本地路径都在
        assert "feishu" in (task.intent or "")
        assert "/x/quote.pdf" in (task.intent or "")
        # 来源是**结构化的**一列，不是靠 intent 的措辞去认（V1.93）：
        # 改文案不会让「这是外部内容」这个判断失效。
        assert task.origin == ORIGIN_CAPTURED
        # 捕获侧：关联 + 终态 + 出队
        stored = app.pending._captured.get(item.id)
        assert stored.status is CapturedStatus.CLARIFIED
        assert stored.task_id == task.id
        assert stored.resolved_at is not None
        assert app.pending.count() == 0
        assert app.pending.pending() == ()

    def test_clarify_auto_title_for_image_does_not_invent_names(self, env):
        app, clock, _role = env
        # 飞书侧图片统一叫 image —— 标题必须如实说「一张图片」+ 时间戳，
        # 而不是把 "image" 这个内部名当成文件名用。
        item = _add(app, clock, kind="image", name="image", path="/x/img")
        task = app.pending.clarify(item.id)
        assert task.title == f"图片（{item.created_at:%m-%d %H:%M}）"

    def test_clarify_explicit_title_wins(self, env):
        app, clock, _role = env
        item = _add(app, clock, name="Screenshot 2026-10-21.png")
        task = app.pending.clarify(item.id, title="  把报价单扫描件归档 ")
        assert task.title == "把报价单扫描件归档"

    def test_clarify_defaults_to_default_role_and_says_so(self, env):
        app, clock, _role = env
        # 库里已有「工作」角色，但澄清**不猜**归属 —— 进默认脉络。
        item = _add(app, clock)
        task = app.pending.clarify(item.id)
        role_names = {r.id: r.name for r in app.roles.list_roles()}
        assert [role_names[r] for r in task.role_ids] == [DEFAULT_ROLE_NAME]

    def test_clarify_twice_is_rejected_with_task_ref(self, env):
        app, clock, _role = env
        item = _add(app, clock)
        task = app.pending.clarify(item.id)
        with pytest.raises(Exception) as excinfo:
            app.pending.clarify(item.id)
        assert task.id[:8] in str(excinfo.value), "重复澄清要指向已生成的事务"

    def test_dismiss_removes_from_pending_and_is_idempotent_refused(self, env):
        app, clock, _role = env
        item = _add(app, clock)
        app.pending.dismiss(item.id)
        assert app.pending.count() == 0
        stored = app.pending._captured.get(item.id)
        assert stored.status is CapturedStatus.DISMISSED
        assert stored.task_id is None
        with pytest.raises(Exception):
            app.pending.dismiss(item.id)

    def test_unknown_item_id_raises(self, env):
        app, _clock, _role = env
        with pytest.raises(Exception):
            app.pending.clarify("0" * 32)

    def test_failed_download_reference_still_clarifiable(self, env):
        """下载失败的捕获（path 指向 .meta.json）照样能澄清 ——
        「收下」不依赖那一次下载成功，只依赖飞书还认这个 key。"""
        app, clock, _role = env
        item = _add(app, clock, kind="image", name="image", size=0,
                    path="/x/evt__image.meta.json", key="k9")
        task = app.pending.clarify(item.id)
        assert task.title.startswith("图片（")
        assert "k9" in (task.intent or "") or "meta.json" in (task.intent or "")


# --------------------------------------------------------------------- 文本捕获
class TestPendingTextCapture:
    """录入口三件之文本捕获（V1.88）：kind="text"，落盘 .txt，media_key 占位空串。

    与图片/文件同一条纪律：只入队不建事务（澄清是 /pending N），不猜内容
    （切分只按行）。文本没有飞书媒体凭据，所以 media_key 给空串 —— 这正是
    「原件在盘上、DB 里存引用」的延续。
    """

    def test_capture_text_single_mail_entry_lands_and_clarifies(self, env):
        app, clock, _role = env
        item = app.pending.capture_text(
            source="mail", text="张总确认合同", name="转发：合同确认"
        )
        assert item.kind == "text" and item.source == "mail"
        assert item.name == "转发：合同确认"
        assert item.media_key == ""
        assert Path(item.path).read_text(encoding="utf-8") == "张总确认合同"
        assert app.pending.count() == 1
        # 澄清后标题用显式 name（不猜内容，原样带走）
        task = app.pending.clarify(item.id)
        assert task.title == "转发：合同确认"

    def test_capture_text_auto_title_uses_first_line(self, env):
        app, _clock, _role = env
        item = app.pending.capture_text(
            source="mail", text="周三和张总确认\n（转发邮件，正文见下）"
        )
        assert item.name == "周三和张总确认"

    def test_capture_text_blank_raises(self, env):
        app, _clock, _role = env
        with pytest.raises(Exception):
            app.pending.capture_text(source="mail", text="   \n  ")

    def test_capture_batch_splits_lines_and_skips_blanks(self, env):
        app, _clock, _role = env
        items = app.pending.capture_batch(
            source="cli", text="周三和张总确认\n\n   \n周四交季度报告\n"
        )
        assert len(items) == 2, "空行/纯空白行要跳过"
        assert [i.name for i in items] == ["周三和张总确认", "周四交季度报告"]
        assert all(i.kind == "text" and i.source == "cli" for i in items)
        assert app.pending.count() == 2
        # 每条各自落盘成 .txt，内容即该行原文
        for item in items:
            assert Path(item.path).read_text(encoding="utf-8") == item.name

    def test_capture_batch_all_blank_raises(self, env):
        app, _clock, _role = env
        with pytest.raises(Exception):
            app.pending.capture_batch(source="cli", text="\n\n  \n")


# --------------------------------------------------------------------- 渲染
class TestRenderPending:
    def _items(self, env):
        app, clock, _role = env
        first = _add(app, clock, name="first.pdf", path="/x/first.pdf")
        second = _add(app, clock, kind="image", name="image", path="")
        return app, first, second

    def test_empty_state_says_how_things_get_in(self, env):
        app, _clock, _role = env
        text = render_pending(app.pending.pending())
        assert "0 条" in text
        assert "飞书" in text, "空态要说明东西从哪进来"

    def test_list_shows_fifo_names_paths_and_hints(self, env):
        app, first, second = self._items(env)
        text = render_pending(app.pending.pending())
        assert "2 条" in text
        assert "first.pdf" in text
        assert "/x/first.pdf" in text
        # 图片没有真名：不显示「image」这个内部名，显示类型 + 时间戳
        assert "「image」" not in text
        # 没有本地路径的那条照实说「未下载」并给出飞书引用（k1 是它的 key）
        assert "未下载" in text and "k1" in text
        assert "/pending <序号>" in text

    def test_today_hook_only_appears_when_pending(self, env):
        app, clock, role = env
        task = app.tasks.create(title="写周报", role_ids=(role.id,))
        view = app.today.view()
        names = {role.id: role.name}
        # 0 条：不占位（今天的主区是排序结果，不被常态空行稀释）
        assert "待处理" not in render_today(view, names)
        assert "待处理" not in render_today(view, names, pending_count=0)
        # >0：末尾多一行有界提醒，并给路
        _add(app, clock)
        _add(app, clock)
        text = render_today(view, names, pending_count=app.pending.count())
        assert "待处理 2 条" in text
        assert "/pending" in text


# --------------------------------------------------------------------- 桥接落队
class _FakeDedup:
    def __init__(self):
        self.seen = set()

    def is_duplicate(self, key):
        if key in self.seen:
            return True
        self.seen.add(key)
        return False


class _FakeChannel:
    """带 ``app`` 的假 channel —— 桥接的 ``_db_lock`` 要拿它持进程级锁。"""

    def __init__(self, app):
        self.app = app
        self.dedup = _FakeDedup()
        self.allowed = {"ou_a"}

    def is_allowed(self, ids):
        return bool(ids & self.allowed)

    def allowlist_mismatch(self, ids):
        return f"不在白名单: {ids}"


class _FakeSender:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    def send_text(self, chat_id, text):
        self.sent.append((chat_id, text))

    def download_media(self, key, *, media_type, downloader=None):
        if self.fail:
            raise RuntimeError("no network")
        return b"IMGDATA-bytes"


def _msg(**kw):
    base = dict(
        event_id="e1", chat_id="oc_x", sender_open_id="ou_a", text="",
        is_group=False, mentioned=True, unsupported_type="image",
        message_id="m1", attachment=None,
    )
    base.update(kw)
    return IncomingMessage(**base)


class TestBridgeEnqueues:
    def _bridge(self, app, tmp_path, *, fail=False):
        home = tmp_path / "state"
        home.mkdir()
        bridge = FeishuBridge(
            _FakeChannel(app), _FakeSender(fail=fail), config=None,
            home=home, pending=app.pending,
        )
        return bridge, home

    def test_capture_success_enqueues_with_local_path(self, env, tmp_path):
        app, _clock, _role = env
        bridge, home = self._bridge(app, tmp_path)
        bridge._capture_unsupported(
            _msg(attachment=AttachmentRef("image", "k1", "image", 0)),
        )
        assert app.pending.count() == 1, "收下即入队（11.18.4）"
        item = app.pending.pending()[0]
        assert item.kind == "image" and item.media_key == "k1"
        assert Path(item.path).read_bytes() == b"IMGDATA-bytes"
        assert any("待处理队列" in t for _, t in bridge.sender.sent)

    def test_capture_failure_enqueues_reference(self, env, tmp_path):
        app, _clock, _role = env
        bridge, _home = self._bridge(app, tmp_path, fail=True)
        bridge._capture_unsupported(
            _msg(unsupported_type="file",
                 attachment=AttachmentRef("file", "k2", "报表.pdf", 1234)),
        )
        assert app.pending.count() == 1, "下载失败也入队（引用不丢）"
        item = app.pending.pending()[0]
        assert item.path.endswith(".meta.json")
        assert item.size == 1234, "size 用消息里声称的大小"
        assert any("待处理队列" in t for _, t in bridge.sender.sent)

    def test_capture_without_pending_service_still_works(self, env, tmp_path):
        """pending=None（离线路径）：只落盘不入队，行为与 V1.48 一致。"""
        app, _clock, _role = env
        home = tmp_path / "state2"
        home.mkdir()
        bridge = FeishuBridge(
            _FakeChannel(app), _FakeSender(), config=None, home=home,
        )
        bridge._capture_unsupported(
            _msg(attachment=AttachmentRef("image", "k3", "image", 0)),
        )
        assert app.pending.count() == 0
        assert any("已收下" in t for _, t in bridge.sender.sent)
        # 没接队列就不许提「待处理队列」—— 提了用户就会去 /pending 找个
        # 不存在的条目。这个撒谎分支在提交前全量测试里才暴露（见桥接
        # ``_enqueue_capture`` 的三态契约）。
        assert not any("待处理队列" in t for _, t in bridge.sender.sent)

    def test_whitelist_outsider_never_enqueues(self, env, tmp_path):
        app, _clock, _role = env
        bridge, _home = self._bridge(app, tmp_path)
        bridge._capture_unsupported(
            _msg(sender_open_id="ou_b",
                 attachment=AttachmentRef("image", "k4", "image", 0)),
        )
        assert app.pending.count() == 0
        assert not bridge.sender.sent

    def test_dedup_does_not_double_enqueue(self, env, tmp_path):
        app, _clock, _role = env
        bridge, _home = self._bridge(app, tmp_path)
        att = AttachmentRef("image", "k5", "image", 0)
        bridge._capture_unsupported(_msg(event_id="dup", message_id="", attachment=att))
        bridge._capture_unsupported(_msg(event_id="dup", message_id="", attachment=att))
        assert app.pending.count() == 1

    # ---- 飞书长文本录入口（/capture、/mail，V1.88）------------------------ #
    def _text_msg(self, text, **kw):
        """文本消息：unsupported_type=None（不是图片/文件）。"""
        return _msg(unsupported_type=None, text=text, **kw)

    def test_feishu_capture_command_enqueues_batch(self, env, tmp_path):
        app, _clock, _role = env
        bridge, _home = self._bridge(app, tmp_path)
        reply = bridge._route_capture(
            self._text_msg("/capture\n周三和张总确认\n周四交季度报告")
        )
        assert reply and "2 条" in reply
        assert app.pending.count() == 2
        items = app.pending.pending()
        assert [i.name for i in items] == ["周三和张总确认", "周四交季度报告"]
        assert all(i.source == "feishu" and i.kind == "text" for i in items)

    def test_feishu_mail_command_enqueues_single(self, env, tmp_path):
        app, _clock, _role = env
        bridge, _home = self._bridge(app, tmp_path)
        reply = bridge._route_capture(
            self._text_msg("/mail\n张总确认合同\n（正文细节）")
        )
        assert reply and "1 条" in reply
        assert app.pending.count() == 1
        item = app.pending.pending()[0]
        assert item.source == "mail" and item.name == "张总确认合同"

    def test_feishu_capture_command_alone_shows_usage_not_enqueue(self, env, tmp_path):
        app, _clock, _role = env
        bridge, _home = self._bridge(app, tmp_path)
        reply = bridge._route_capture(self._text_msg("/capture"))
        assert reply and "批量倾倒" in reply
        assert app.pending.count() == 0, "命令单独一行：回用法，不猜用户想倒什么"

    def test_feishu_capture_non_command_not_intercepted(self, env, tmp_path):
        app, _clock, _role = env
        bridge, _home = self._bridge(app, tmp_path)
        assert bridge._route_capture(self._text_msg("普通文本")) is None, (
            "非 /capture /mail 的命令或普通文本不拦"
        )


# ----------------------------------------------------------------- 通道收口
class TestChannelCanClarify:
    """飞书里 ``/pending`` 到底能不能用（设计文档 11.18.8）。

    这条纪律是**实测出来的**：文档曾把「飞书里直接 ``/pending``」列为不做，
    而 ``channel.py:359`` 早就放行了所有 ``/`` 前缀命令 —— 文档说不做、
    代码做得到，是比两种明确状态都差的错位。11.18.8 收口成「能与终端同权」，
    本类把它钉住，免得哪天又被当成「没接线」而改回去。
    """

    def _channel(self, app):
        from freeagent.services.channel import ChannelService

        return ChannelService(app, allowed_senders=frozenset({"ou_a"}))

    def test_channel_lists_pending(self, env):
        app, clock, _role = env
        _add(app, clock, name="合同扫描件.pdf")
        reply = self._channel(app).handle("oc1", "ou_a", "/pending")
        assert "待处理" in reply.text, f"通道里 /pending 要能列出：{reply.text!r}"
        assert "合同扫描件.pdf" in reply.text, f"要看到收下了什么：{reply.text!r}"

    def test_channel_clarifies_into_a_task(self, env):
        app, clock, _role = env
        _add(app, clock, name="合同扫描件.pdf")
        channel = self._channel(app)
        reply = channel.handle("oc1", "ou_a", "/pending 1 处理合同扫描件")
        assert app.pending.count() == 0, "澄清后待处理清空"
        tasks = app.tasks.list_all() if hasattr(app.tasks, "list_all") else []
        assert "处理合同扫描件" in reply.text, f"回话要说清建了什么：{reply.text!r}"
        # 落点必须说清：手机上看不见「我归到默认脉络了」（11.18.8）
        assert "默认脉络" in reply.text, f"要说清落在哪：{reply.text!r}"
        assert "/move" in reply.text, f"要附归位方法：{reply.text!r}"

    def test_capture_reply_points_to_this_channel_not_terminal(self, env, tmp_path):
        """飞书回话不许说「在终端 /pending」—— 用户人就在飞书里。

        说错了用户会绕到终端去找，绕一圈才发现飞书里就有；而 11.18.7
        曾把飞书澄清记成「不做」，于是回话与文档双双说谎。
        """
        app, _clock, _role = env
        home = tmp_path / "state3"
        home.mkdir()
        bridge = FeishuBridge(
            _FakeChannel(app), _FakeSender(), config=None,
            home=home, pending=app.pending,
        )
        bridge._capture_unsupported(
            _msg(attachment=AttachmentRef("image", "k9", "image", 0)),
        )
        reply = next(t for _, t in bridge.sender.sent if "待处理队列" in t)
        assert "终端" not in reply, f"飞书回话不许把人指去终端：{reply!r}"
        assert "/pending" in reply, f"要指明在哪看：{reply!r}"
