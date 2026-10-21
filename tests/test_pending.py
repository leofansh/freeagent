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
from freeagent.domain import DEFAULT_ROLE_NAME
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
