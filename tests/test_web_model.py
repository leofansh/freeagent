"""Web「画像」视图端点测试（设计文档 11.17 / V1.59）。

``GET /api/model`` 是个人模型的只读投影：节奏 / 关系账 / 承诺与目标 三面，
只摆事实。本测试盯住：端点存在、返回结构齐全、关系账与承诺含预期字段、
且带「只摆事实」免责标记。
"""

from __future__ import annotations

import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from freeagent.app import build_app  # noqa: E402
from freeagent.domain import TaskKind, WaitingKind, WaitingOn  # noqa: E402
from freeagent.services.clock import FrozenClock  # noqa: E402
from freeagent.services.sorting import EnergyWindows  # noqa: E402
from freeagent.web.server import create_server  # noqa: E402
from test_web import get  # noqa: E402

NOW = datetime(2026, 10, 21, 15, 0)  # 周三


@pytest.fixture
def served(tmp_path):
    """一只钉在周三的 App + 起好的服务器，并预置「张总」的未结清账。"""
    frozen = FrozenClock(NOW)
    application = build_app(
        tmp_path / "agent.db", clock=frozen, energy_windows=EnergyWindows()
    )
    role = application.roles.create("工作")
    zhang = application.promise.contact("张总")
    task = application.tasks.create("给张总报价单", [role.id])
    application.promise.promise(
        "周三前给张总报价单", contact_name="张总",
        promised_for=NOW - timedelta(days=2), task_id=task.id,
    )
    application.tasks.create(
        "等张总回合同", [role.id], kind=TaskKind.WAIT,
        waiting_on=WaitingOn(
            kind=WaitingKind.PERSON, who_or_what="张总", contact_id=zhang.id,
            since=NOW - timedelta(days=3),
        ),
    )
    application.memory.add("今年把英语练到能开会", kind="目标")
    server = create_server(application, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"127.0.0.1:{server.server_address[1]}", application
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        application.close()


class TestModelEndpoint:
    def test_returns_full_payload(self, served):
        addr, _app = served
        status, body, _headers = get(addr, "/api/model")
        assert status == 200
        assert body["kind"] == "personal_model"
        # 节奏面 + 两面新分面都在。
        assert "completion_by_hour" in body
        assert "relationships" in body
        assert "commitments" in body
        assert "disclaimer" in body and body["disclaimer"]

    def test_relationship_carries_open_counts(self, served):
        addr, _app = served
        status, body, _headers = get(addr, "/api/model")
        assert status == 200
        zhang = next(r for r in body["relationships"] if r["name"] == "张总")
        assert zhang["open_promises"] == 1
        assert zhang["open_waitings"] == 1

    def test_commitments_include_overdue_promise_and_goal(self, served):
        addr, _app = served
        status, body, _headers = get(addr, "/api/model")
        assert status == 200
        texts = {c["text"] for c in body["commitments"]}
        assert "周三前给张总报价单" in texts
        assert "今年把英语练到能开会" in texts
        overdue = next(c for c in body["commitments"] if c["text"] == "周三前给张总报价单")
        assert overdue["kind"] == "承诺"
        assert overdue["overdue"] is True
