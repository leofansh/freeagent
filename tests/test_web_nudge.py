"""Web「催办」视图端点测试（设计文档 11.20 / 20.1.4 / V1.57）。

催办草稿**只读 + 拼文本**：不建事务、不改关系状态、系统没有发送能力。
候选口径与信号逐字对齐（OVERDUE_WAIT / PROMISE_OVERDUE）；草稿只铺事实、
正文留白（[TODO]）。本测试盯住这三条不破。
"""

from __future__ import annotations

import sys
import threading
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from freeagent.app import App, build_app  # noqa: E402
from freeagent.domain import TaskKind, WaitingKind, WaitingOn  # noqa: E402
from freeagent.services.clock import FrozenClock  # noqa: E402
from freeagent.services.sorting import EnergyWindows  # noqa: E402
from freeagent.web.server import create_server  # noqa: E402
from freeagent.web.session import SESSION_HEADER, SESSION_TOKEN  # noqa: E402
from test_web import get  # noqa: E402

NOW = datetime(2026, 10, 21, 15, 0)  # 周三


@pytest.fixture
def served(tmp_path):
    """一只钉在周三的 App + 起好的服务器。"""
    frozen = FrozenClock(NOW)
    application = build_app(
        tmp_path / "agent.db", clock=frozen, energy_windows=EnergyWindows()
    )
    role = application.roles.create("工作")
    server = create_server(application, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"127.0.0.1:{server.server_address[1]}", application, role, frozen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        application.close()


def _waiting(app, role, *, follow_up_at=None, who="张总回合同", title="等张总回合同"):
    return app.tasks.create(
        title, [role.id], kind=TaskKind.WAIT,
        waiting_on=WaitingOn(
            kind=WaitingKind.PERSON, who_or_what=who,
            since=datetime(2026, 10, 10, 9, 0), follow_up_at=follow_up_at,
        ),
    )


def _promised(app, role, *, promised_for=None, contact="张总", title="给张总交付报价单"):
    task = app.tasks.create(title, [role.id])
    app.promise.promise(
        "周三前给张总报价单", contact_name=contact,
        promised_for=promised_for, task_id=task.id,
    )
    return task


class TestNudgeEndpoint:
    def test_candidates_empty_when_nothing_due(self, served):
        addr, app, role, _clock = served
        _waiting(app, role, follow_up_at=datetime(2026, 10, 25, 9, 0))  # 没到
        status, data, _h = get(addr, "/api/nudge/candidates")
        assert status == 200
        assert data["count"] == 0
        assert data["items"] == []

    def test_candidates_list_both_directions(self, served):
        addr, app, role, _clock = served
        w = _waiting(app, role, follow_up_at=datetime(2026, 10, 18, 9, 0))
        t = _promised(app, role, promised_for=datetime(2026, 10, 20, 9, 0))
        status, data, _h = get(addr, "/api/nudge/candidates")
        assert status == 200
        assert data["count"] == 2
        by_task = {i["task_id"]: i for i in data["items"]}
        assert by_task[w.id]["direction"] == "wait"
        assert by_task[t.id]["direction"] == "promise"
        assert by_task[t.id]["contact_name"] == "张总"
        assert "elapsed" in by_task[w.id]

    def test_draft_rejects_when_not_due(self, served):
        """不在候选里就拒绝 —— 指错 id 时说出来，而不是默默起草（11.20.2）。"""
        addr, app, role, _clock = served
        task = _waiting(app, role, follow_up_at=datetime(2026, 10, 25, 9, 0))
        status, data, _h = get(addr, "/api/nudge/draft?task_id=" + task.id)
        assert status == 400
        assert "还没到催的时候" in data["error"]

    def test_draft_produces_plain_text_with_todo(self, served):
        """只铺事实、正文留白、系统不发。"""
        addr, app, role, _clock = served
        task = _waiting(app, role, follow_up_at=datetime(2026, 10, 18, 9, 0))
        status, data, _h = get(addr, "/api/nudge/draft?task_id=" + task.id)
        assert status == 200
        assert data["ok"] is True
        assert "方向：" in data["draft"]
        assert "[TODO" in data["draft"]
        assert "系统不会发这条" in data["draft"]

    def test_draft_promise_direction_flips_wording(self, served):
        """promise 方向是「告知延期」，不是「问进度」—— 语气相反。"""
        addr, app, role, _clock = served
        task = _promised(app, role, promised_for=datetime(2026, 10, 20, 9, 0))
        status, data, _h = get(addr, "/api/nudge/draft?task_id=" + task.id)
        assert status == 200
        assert "等我方交付" in data["draft"]
        assert "我答应过" in data["draft"]

    def test_draft_requires_task_id(self, served):
        addr, app, _role, _clock = served
        status, data, _h = get(addr, "/api/nudge/draft")
        assert status == 400
        assert "要指定一条事务" in data["error"]

    def test_draft_unknown_task_is_400(self, served):
        addr, app, _role, _clock = served
        status, data, _h = get(addr, "/api/nudge/draft?task_id=nope")
        assert status == 400
