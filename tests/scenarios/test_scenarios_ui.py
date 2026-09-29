"""S8–S11 —— **基于操作界面**的场景测试。

S1–S7 走终端（``test_scenarios.py`` / ``test_scenarios_v1gapfill.py``）。
这里全部走 HTTP，模拟用户在浏览器里实际会做的事：建单 → 看今天 → 开详情 →
改状态 → 刷新。断言的是**界面承诺**，不是内部实现。
"""

from __future__ import annotations

import json
import threading
from datetime import date, datetime
from http.client import HTTPConnection

import pytest

from freeagent.app import App, build_app
from freeagent.domain import TaskKind, TaskState, WaitingKind, WaitingOn
from freeagent.services.clock import FrozenClock
from freeagent.services.sorting import DISCLAIMER_MARKER
from freeagent.web.server import create_server
from freeagent.web.session import SESSION_HEADER, SESSION_TOKEN

NOW = datetime(2026, 9, 26, 9, 0)
TODAY = "2026-09-26"


class Ui:
    """极简浏览器：只发请求，不解析 HTML（页面由契约测试守着）。

    **带上会话令牌**：真实浏览器里令牌由页面注入、``js_core`` 自动放进
    请求头（设计方案 12.7）。这里照做 —— 不是给测试开后门，是让模拟器
    走真实浏览器那条路。
    """

    def __init__(self, addr: str) -> None:
        self.addr = addr

    def get(self, path: str):
        return self._request("GET", path)

    def post(self, path: str, body: dict | None = None):
        return self._request("POST", path, body or {})

    def _request(self, method: str, path: str, body: dict | None = None):
        host, port = self.addr.split(":")
        conn = HTTPConnection(host, int(port), timeout=5)
        try:
            payload = json.dumps(body).encode() if body is not None else None
            headers = {SESSION_HEADER: SESSION_TOKEN}
            if payload is not None:
                headers |= {
                    "Content-Type": "application/json",
                    "Content-Length": str(len(payload)),
                }
            conn.request(method, path, body=payload, headers=headers)
            res = conn.getresponse()
            raw = res.read()
            return res.status, json.loads(raw.decode("utf-8"))
        finally:
            conn.close()

    def page(self) -> str:
        host, port = self.addr.split(":")
        conn = HTTPConnection(host, int(port), timeout=5)
        try:
            conn.request("GET", "/")
            return conn.getresponse().read().decode("utf-8")
        finally:
            conn.close()


@pytest.fixture()
def ui(app: App):
    server = create_server(app, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield Ui(f"127.0.0.1:{server.server_address[1]}")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture()
def work(app: App):
    role = app.roles.create(
        "工作项目A", note="销售 周报", default_definition_of_done="先给我能用的就行"
    )
    app.roles.create("家庭", note="孩子 学校 材料")
    return role


# =============================================================================
# S8 —— 建单 → 立刻在「今天」里看得见
# =============================================================================
class TestS8CreateThenVisibleToday:
    """回归：曾经界面上建的事务在今天视图里 count=0（表单没有日期输入）。"""

    def test_created_task_shows_up_in_today(self, ui: Ui, work):
        status, created = ui.post(
            "/api/task",
            {"title": "下周二交周报", "role_ids": [work.id], "scheduled_for": TODAY},
        )
        assert status == 201
        new_id = created["task"]["id"]
        assert created["task"]["scheduled_for"] == TODAY

        status, today = ui.get("/api/today")
        assert status == 200
        assert new_id in [i["id"] for i in today["items"]], "建完必须在今天视图里能看见"

    def test_unscheduled_task_is_not_in_today(self, ui: Ui, work):
        """留空日期 = 先不排，那就**不该**出现在今天 —— 区别要清楚。"""
        _s, created = ui.post(
            "/api/task", {"title": "以后再说", "role_ids": [work.id]}
        )
        assert created["task"]["scheduled_for"] is None
        _s, today = ui.get("/api/today")
        assert created["task"]["id"] not in [i["id"] for i in today["items"]]
        _s, everything = ui.get("/api/all?scope=open")
        assert created["task"]["id"] in [i["id"] for i in everything["items"]]

    def test_page_has_date_input_defaulting_today(self, ui: Ui, work):
        page = ui.page()
        assert 'id="d"' in page, "表单必须有日期输入框"
        assert 'type="date"' in page

    def test_form_sends_the_date(self, ui: Ui, work):
        page = ui.page()
        assert "scheduled_for" in page, "提交时必须把日期发出去"
        assert 'value="' + TODAY + '"' in page or "toISOString" in page


# =============================================================================
# S9 —— 详情页 + 改状态（界面必须是「能操作」的）
# =============================================================================
class TestS9OperateFromDetail:
    def test_detail_exposes_restore_contract(self, ui: Ui, app: App, work):
        t = app.tasks.create("写周报", [work.id], intent="先理一版")
        app.tasks.start(t.id)
        app.artifacts.create_draft(t.id, "初稿", "# 框架")
        app.tasks.note(t.id, "有框架了")

        status, data = ui.get(f"/api/task/{t.id}")
        assert status == 200
        assert data["task"]["intent"] == "先理一版"
        assert data["effective_definition_of_done"] == "先给我能用的就行"
        assert data["artifact"]["version"] == 1
        assert "有框架了" in data["progress_note"]
        assert data["records"]
        assert data["next_actions"]

    def test_allowed_next_states_come_from_service(self, ui: Ui, app: App, work):
        """前端不自己算合法迁移 —— 由服务层告知。"""
        t = app.tasks.create("写周报", [work.id])
        _s, data = ui.get(f"/api/task/{t.id}")
        assert {s["value"] for s in data["allowed_next_states"]} == {
            "active", "blocked", "done", "dropped"
        }
        # 状态变了，合法迁入跟着变
        ui.post(f"/api/task/{t.id}/done", {})
        _s, done = ui.get(f"/api/task/{t.id}")
        assert {s["value"] for s in done["allowed_next_states"]} == {"active", "inbox"}

    def test_full_lifecycle_from_ui(self, ui: Ui, app: App, work):
        t = app.tasks.create("写周报", [work.id])
        tid = t.id
        for action, expected in [
            ("start", "active"),
            ("done", "done"),
        ]:
            status, data = ui.post(f"/api/task/{tid}/{action}", {})
            assert status == 200, data
            assert data["task"]["state"] == expected
            assert app.task_repo.get(tid).state.value == expected

    def test_pause_from_ui(self, ui: Ui, app: App, work):
        t = app.tasks.create("写周报", [work.id])
        app.tasks.start(t.id)
        status, data = ui.post(f"/api/task/{t.id}/pause", {})
        assert status == 200 and data["task"]["state"] == "inbox"

    def test_drop_from_ui(self, ui: Ui, app: App, work):
        t = app.tasks.create("算了的事", [work.id])
        status, data = ui.post(f"/api/task/{t.id}/drop", {})
        assert status == 200 and data["task"]["state"] == "dropped"
        assert app.task_repo.get(t.id).dropped_at is not None

    def test_pin_and_unpin_from_ui(self, ui: Ui, app: App, work):
        t = app.tasks.create("排期的事", [work.id])
        status, pinned = ui.post(f"/api/task/{t.id}/pin", {})
        assert status == 200 and pinned["task"]["scheduled_for"] == TODAY
        status, un = ui.post(f"/api/task/{t.id}/unpin", {})
        assert status == 200 and un["task"]["scheduled_for"] is None
        # 移出排期**不改变状态**（设计文档 6.4）
        assert un["task"]["state"] == "inbox"

    def test_blocked_requires_saying_what_you_wait_for(self, ui: Ui, app: App, work):
        """「放一放」必须说清在等什么 —— 否则 BLOCKED 是黑洞状态。"""
        t = app.tasks.create("写周报", [work.id])
        status, data = ui.post(f"/api/task/{t.id}/blocked", {})
        assert status == 400 and "在等什么" in data["error"]
        assert app.task_repo.get(t.id).state.value == "inbox"

        status, ok = ui.post(
            f"/api/task/{t.id}/blocked", {"waiting_on": "客户法务"}
        )
        assert status == 200, ok
        got = app.task_repo.get(t.id)
        assert got.state.value == "blocked"
        assert got.waiting_on.who_or_what == "客户法务"

    def test_illegal_transition_is_refused(self, ui: Ui, app: App, work):
        """DONE 不能直接回 BLOCKED —— 服务层拦，界面收到 400。"""
        t = app.tasks.create("写周报", [work.id])
        ui.post(f"/api/task/{t.id}/done", {})
        status, data = ui.post(
            f"/api/task/{t.id}/blocked", {"waiting_on": "某人"}
        )
        assert status == 400
        assert "不允许" in data["error"]
        assert app.task_repo.get(t.id).state.value == "done"

    def test_allowed_next_states_carry_action_names(self, ui: Ui, app: App, work):
        """状态名 ≠ 动作名（active ↔ start）。前端必须用接口给的动作。"""
        t = app.tasks.create("写周报", [work.id])
        _s, data = ui.get(f"/api/task/{t.id}")
        mapping = {s["value"]: s["action"] for s in data["allowed_next_states"]}
        # 初始态 INBOX 不能迁往自己，所以没有 inbox 这一项
        assert mapping == {
            "active": "start", "blocked": "blocked",
            "done": "done", "dropped": "drop",
        }
        # 到了 ACTIVE，「等会再做」才出现
        ui.post(f"/api/task/{t.id}/start", {})
        _s, active = ui.get(f"/api/task/{t.id}")
        assert {s["value"]: s["action"] for s in active["allowed_next_states"]} == {
            "inbox": "pause", "blocked": "blocked",
            "done": "done", "dropped": "drop",
        }

    def test_ui_offers_only_actions_the_api_supports(self, ui: Ui, app: App, work):
        """回归：UI 曾把状态名当动作 POST（`/active`）→ 必 404。"""
        t = app.tasks.create("写周报", [work.id])
        _s, data = ui.get(f"/api/task/{t.id}")
        for s in data["allowed_next_states"]:
            body = {"waiting_on": "客户"} if s["value"] == "blocked" else {}
            status, _r = ui.post(f"/api/task/{t.id}/{s['action']}", body)
            assert status != 404, f"界面提供了「{s['label']}」但接口不支持"
            # 回到初始态，逐个独立验证
            if s["value"] != "inbox":
                ui.post(f"/api/task/{t.id}/inbox", {})

    def test_transition_appends_record(self, ui: Ui, app: App, work):
        t = app.tasks.create("写周报", [work.id])
        ui.post(f"/api/task/{t.id}/start", {})
        types = [r.type.value for r in app.record_repo.list_for_task(t.id)]
        assert "status_change" in types

    def test_unknown_action_is_404(self, ui: Ui, work):
        t = ui.post("/api/task", {"title": "x", "role_ids": [work.id]})[1]["task"]
        status, _data = ui.post(f"/api/task/{t['id']}/teleport", {})
        assert status == 404

    def test_transition_on_missing_task_is_400(self, ui: Ui):
        status, data = ui.post("/api/task/deadbeef/start", {})
        assert status == 400 and "找不到事务" in data["error"]


# =============================================================================
# S10 —— 等候类：界面上能看到「该催谁」，也能销账
# =============================================================================
class TestS10WaitingInUi:
    @pytest.fixture()
    def waiting(self, app: App, work):
        t = app.tasks.create(
            "等客户回复合同", [work.id], kind=TaskKind.WAIT,
            waiting_on=WaitingOn(
                WaitingKind.PERSON, "客户法务", datetime(2026, 9, 20),
                datetime(2026, 9, 25),
            ),
        )
        app.tasks.schedule(t.id, date(2026, 9, 26))
        return t

    def test_overdue_wait_is_visible_with_reason(self, ui: Ui, waiting):
        _s, today = ui.get("/api/today")
        item = next(i for i in today["items"] if i["id"] == waiting.id)
        codes = {s["code"] for s in item["signals"]}
        assert "overdue_wait" in codes
        reason = next(s["reason"] for s in item["signals"] if s["code"] == "overdue_wait")
        assert "客户法务" in reason, "界面必须点名该催谁"
        # 权重最高 → 排第一
        assert today["items"][0]["id"] == waiting.id

    def test_waiting_detail_exposes_target(self, ui: Ui, waiting):
        _s, data = ui.get(f"/api/task/{waiting.id}")
        assert data["waiting_on"]["who_or_what"] == "客户法务"
        assert data["waiting_on"]["follow_up_at"] is not None

    def test_waiting_can_be_closed_from_ui(self, ui: Ui, app: App, waiting):
        """等候类到期即销账。"""
        status, data = ui.post(f"/api/task/{waiting.id}/done", {})
        assert status == 200 and data["task"]["state"] == "done"
        got = app.task_repo.get(waiting.id)
        assert got.state is TaskState.DONE
        assert got.kind is TaskKind.WAIT, "终态不改 kind"
        assert got.waiting_on is None

    def test_waiting_can_become_action_from_ui(self, ui: Ui, app: App, waiting):
        status, data = ui.post(f"/api/task/{waiting.id}/start", {})
        assert status == 200
        got = app.task_repo.get(waiting.id)
        assert got.kind is TaskKind.ACTION, "离开 BLOCKED 自动转动作类"
        assert got.waiting_on is None
        # 原等候信息进了历史
        assert any("客户法务" in r.content for r in app.record_repo.list_for_task(waiting.id))

    def test_rollover_flag_shown_in_ui(self, ui: Ui, app: App, work):
        t = app.tasks.create("上周就该交的", [work.id])
        app.tasks.schedule(t.id, date(2026, 9, 20))
        _s, today = ui.get("/api/today")
        item = next(i for i in today["items"] if i["id"] == t.id)
        assert item["rolled_over"] is True
        assert "顺延" in today["rollover"]["summary"]

    def test_rollover_is_idempotent(self, ui: Ui, app: App, work):
        t = app.tasks.create("上周就该交的", [work.id])
        app.tasks.schedule(t.id, date(2026, 9, 20))
        first = ui.get("/api/today")[1]
        second = ui.get("/api/today")[1]
        assert first["rollover"]["count"] == 1
        assert second["rollover"]["count"] == 0
        types = [r.type.value for r in app.record_repo.list_for_task(t.id)]
        assert types.count("rollover") == 1

    def test_reminders_work_in_ui(self, ui: Ui, app: App, clock, work):
        """回归：Web 层曾完全不检查提醒，提醒只有终端有。"""
        t = app.tasks.create(
            "修窗户", [work.id], kind=TaskKind.REMINDER,
            reminder_time=datetime(2026, 9, 26, 15, 0),
        )
        app.tasks.schedule(t.id, date(2026, 9, 26))

        status, early = ui.get("/api/reminders")
        assert status == 200 and early["count"] == 0 and early["text"] is None

        clock.set(datetime(2026, 9, 26, 15, 5))
        _s, fired = ui.get("/api/reminders")
        assert fired["count"] == 1
        assert "修窗户" in fired["text"]
        assert fired["fired"][0]["task_id"] == t.id

        # 幂等：同一次提醒不重复推送（设计文档 9.3）
        _s, again = ui.get("/api/reminders")
        assert again["count"] == 0

    def test_handler_failure_does_not_consume_the_reminder(
        self, ui: Ui, app: App, clock, work, monkeypatch
    ):
        """**回归**：handler 失败时提醒必须还在。

        以前这个端点直接调消费型的 ``check()``，于是 payload 构造一抛，
        令牌已经落库、提醒永久丢失，而用户从没看到。9.2 现在要求
        「令牌在送达确认之后才落库」，所以确认必须在 payload 成功之后。
        """
        from freeagent.web import serialize_api

        t = app.tasks.create(
            "修窗户", [work.id], kind=TaskKind.REMINDER,
            reminder_time=datetime(2026, 9, 26, 15, 0),
        )
        clock.set(datetime(2026, 9, 26, 15, 5))

        def boom(_digest):
            raise RuntimeError("序列化炸了")

        monkeypatch.setattr(serialize_api, "reminder_payload", boom)
        status, _ = ui.get("/api/reminders")
        assert status == 500, "handler 抛异常时该回 500"

        # 关键断言：失败没有消费掉提醒
        monkeypatch.undo()
        _s, after = ui.get("/api/reminders")
        assert after["count"] == 1, "handler 失败不该把提醒消费掉"
        assert after["fired"][0]["task_id"] == t.id

    def test_missed_reminder_labelled_in_ui(self, ui: Ui, app: App, clock, work):
        app.tasks.create(
            "交物业费", [work.id], kind=TaskKind.REMINDER,
            reminder_time=datetime(2026, 9, 25, 10, 0),
        )
        clock.set(datetime(2026, 9, 26, 20, 0))
        _s, digest = ui.get("/api/reminders")
        assert len(digest["missed"]) == 1
        assert "已错过" in digest["text"], "错过的要如实标注，不当新提醒"

    def test_reminder_digest_is_merged_single_message(self, ui: Ui, app: App, clock, work):
        """不扰民：三条到期合成一条，不逐条弹。"""
        for i in range(3):
            app.tasks.create(
                f"提醒{i}", [work.id], kind=TaskKind.REMINDER,
                reminder_time=datetime(2026, 9, 26, 15, i),
            )
        clock.set(datetime(2026, 9, 26, 15, 30))
        _s, digest = ui.get("/api/reminders")
        assert digest["count"] == 3
        assert digest["text"] and "\n" not in digest["text"]
        assert "3 条" in digest["text"]

    def test_page_shows_reminder_banner(self, ui: Ui):
        page = ui.page()
        assert "/api/reminders" in page, "界面应主动查提醒"
        assert "🔔" in page


# =============================================================================
# S11 —— 界面不许丢掉设计承诺
# =============================================================================
class TestS11UiKeepsDesignPromises:
    def test_every_list_carries_disclaimer(self, ui: Ui, work):
        for path in ("/api/today", "/api/all?scope=open", "/api/all?scope=closed"):
            _s, data = ui.get(path)
            assert data["disclaimer"] == DISCLAIMER_MARKER, path

    def test_page_states_it_does_not_decide(self, ui: Ui):
        page = ui.page()
        assert DISCLAIMER_MARKER in page
        assert "不替你决定" in page

    def test_signals_always_have_reasons(self, ui: Ui, app: App, work):
        t = app.tasks.create("催一下", [work.id])
        app.tasks.set_due_time(t.id, NOW.replace(hour=18))
        app.tasks.schedule(t.id, date(2026, 9, 26))
        for path in ("/api/today", "/api/all?scope=open"):
            _s, data = ui.get(path)
            for item in data["items"]:
                for sig in item["signals"]:
                    assert sig["reason"].strip(), f"{sig['code']} 理由为空"
                    assert isinstance(sig["weight"], int)

    def test_signals_expose_which_strongest(self, ui: Ui, app: App, work):
        t = app.tasks.create("催一下", [work.id])
        app.tasks.set_due_time(t.id, NOW.replace(hour=18))
        app.tasks.schedule(t.id, date(2026, 9, 26))
        _s, data = ui.get("/api/today")
        item = data["items"][0]
        assert item["signals"] == sorted(
            item["signals"], key=lambda s: -s["weight"]
        ), "信号应按权重降序，界面第一条就是最强的提示"

    def test_role_selection_has_visual_state(self, ui: Ui):
        """回归：角色按钮曾经没有选中态，用户不知道选了谁。"""
        page = ui.page()
        assert "aria-pressed" in page

    def test_refresh_control_exists(self, ui: Ui):
        assert 'id="refresh"' in ui.page()

    def test_no_write_beyond_whitelisted_actions(self):
        """界面的写操作是白名单，不是任意方法调用。"""
        import inspect

        from freeagent.web import endpoints
        from freeagent.web.actions import STATE_ACTIONS

        assert set(STATE_ACTIONS) == {"start", "done", "pause", "drop", "blocked"}
        source = inspect.getsource(endpoints.mutate)
        # 界面**不能**做这些：草稿采纳、角色合并、删除、改名、静置
        for forbidden in ("accept", "merge", "delete", "rename", "silence", "set_kind"):
            assert forbidden not in source, f"界面不该做 {forbidden}"

    def test_web_delegates_transitions_to_service(self):
        """迁移逻辑必须在服务层；web 只转发。"""
        import inspect

        from freeagent.web import endpoints

        source = inspect.getsource(endpoints.mutate)
        for call in (
            "app.tasks.start",
            "app.tasks.complete",
            "app.tasks.pause",
            "app.tasks.drop",
            "app.tasks.schedule",
            "app.tasks.unschedule",
        ):
            assert call in source, f"web 层应委托 {call}"
        assert "ALLOWED_TRANSITIONS" not in source, "web 层不该自己查迁移表"
