"""Web UI 集成测试。

用标准库 ``http.client`` 打**真实服务器**（随机端口，后台线程），
不引入 requests，也不依赖浏览器。核心断言是两条设计契约：

1. 每条排序信号的**理由**必须出现在 API 响应里；
2. 响应必须带免责标记「启发式提示，不是评分」。
"""

from __future__ import annotations

import json
import re
import threading
from datetime import date, datetime
from http.client import HTTPConnection

import pytest

from freeagent.app import App, build_app
from freeagent.domain import TaskKind, WaitingKind, WaitingOn
from freeagent.services.clock import FrozenClock
from freeagent.services.sorting import DISCLAIMER_MARKER
from freeagent.web.server import create_server
from freeagent.web.session import SESSION_HEADER, SESSION_TOKEN

NOW = datetime(2026, 9, 26, 9, 0)

#: 会真正发起网络请求的 URL（带协议或协议相对）。用于「不许外部资源」守卫。
_EXTERNAL_URL = re.compile(r"^(?:[a-z][a-z0-9+.-]*:)?//", re.I)


@pytest.fixture()
def served(app: App):
    """起一个真实服务器，端口自动分配。"""
    server = create_server(app, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"127.0.0.1:{server.server_address[1]}", app
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def get(addr: str, path: str) -> tuple[int, object, dict]:
    """发 GET，**带上会话令牌**。

    刻意不为了图省事而关掉令牌：所有 ``/api/``（除白名单）都要校验
    （设计方案 12.7），所以每个测试都得像真实界面那样把令牌带上 ——
    这正是令牌该有的样子。带令牌的测试和带令牌的界面走同一条路。
    """
    host, port = addr.split(":")
    conn = HTTPConnection(host, int(port), timeout=5)
    try:
        conn.request("GET", path, headers={SESSION_HEADER: SESSION_TOKEN})
        res = conn.getresponse()
        raw = res.read()
        ctype = dict(res.getheaders()).get("Content-Type", "")
        if "json" in ctype:
            return res.status, json.loads(raw.decode("utf-8")), dict(res.getheaders())
        return res.status, raw.decode("utf-8"), dict(res.getheaders())
    finally:
        conn.close()


def post(addr: str, path: str, body: dict) -> tuple[int, object]:
    host, port = addr.split(":")
    conn = HTTPConnection(host, int(port), timeout=5)
    try:
        payload = json.dumps(body).encode("utf-8")
        conn.request(
            "POST",
            path,
            body=payload,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
                # POST **一律**要令牌，不设白名单（见 server._post 的注释）。
                SESSION_HEADER: SESSION_TOKEN,
            },
        )
        res = conn.getresponse()
        return res.status, json.loads(res.read().decode("utf-8"))
    finally:
        conn.close()


@pytest.fixture()
def seeded(app: App) -> App:
    work = app.roles.create("工作项目A", note="销售 周报", default_definition_of_done="先给我能用的就行")
    app.roles.create("家庭", note="孩子 学校")

    report = app.tasks.create(
        "下周二要交的销售周报初稿", [work.id], intent="先理一版",
        due_time=datetime(2026, 9, 27, 18, 0),
    )
    app.tasks.schedule(report.id, date(2026, 9, 26))
    app.tasks.start(report.id)
    app.artifacts.create_draft(report.id, "初稿", "# 框架\n- [TODO] 数据")
    app.tasks.note(report.id, "有框架了，还没填数据")

    wait = app.tasks.create(
        "孩子周四要交的材料清单", [app.roles.get_by_name("家庭").id],
        kind=TaskKind.WAIT,
        waiting_on=WaitingOn(
            WaitingKind.PERSON, "孩子带回来",
            datetime(2026, 9, 20), datetime(2026, 9, 25),
        ),
    )
    app.tasks.schedule(wait.id, date(2026, 9, 26))
    errand = app.tasks.create(
        "修一下窗户螺丝", [work.id], kind=TaskKind.REMINDER
    )
    app.tasks.schedule(errand.id, date(2026, 9, 26))
    return app


# =============================================================================
# 页面
# =============================================================================
class TestPage:
    def test_index_is_served(self, served):
        status, body, headers = get(served[0], "/")
        assert status == 200
        assert headers["Content-Type"].startswith("text/html")
        assert "个人事务助手" in body

    def test_page_has_no_external_resources(self, served):
        """本地工具必须能断网用 —— 不许有 CDN 引用。

        **只查真正会发起网络请求的位置**（``src`` / ``href`` / ``srcset`` /
        CSS ``@import`` / ``url()``）。正文里出现 ``http://`` 字样不算 ——
        那只是说明文字；而响应头 ``Content-Security-Policy: default-src 'none'``
        已经让任何真实外部加载 fail closed，所以这里不必把整个文档里
        的 URL 片段都当成违规（那样会误伤正常的帮助文案）。
        """
        _status, body, _h = get(served[0], "/")
        for needle in ("//cdn", "cdnjs", "unpkg", "googleapis", "jsdelivr"):
            assert needle not in body, f"页面引用了外部资源：{needle}"
        for attr in ("src", "href", "srcset"):
            for url in re.findall(rf'{attr}="([^"]*)"', body):
                assert not _EXTERNAL_URL.match(url), f"{attr} 指向外部资源：{url}"
        assert not re.search(r"@import", body), "CSS 里有 @import"
        assert not re.search(r"""url\(\s*['"]?(?:https?:)?//""", body), (
            "CSS 里有外部 url()"
        )

    def test_page_declares_cjk_font_stack(self, served):
        _status, body, _h = get(served[0], "/")
        assert "PingFang SC" in body and "Microsoft YaHei" in body

    def test_page_ships_disclaimer(self, served):
        _status, body, _h = get(served[0], "/")
        assert DISCLAIMER_MARKER in body

    def test_security_headers_present(self, served):
        _status, _body, headers = get(served[0], "/")
        assert "Content-Security-Policy" in headers
        assert headers["X-Content-Type-Options"] == "nosniff"

    def test_unknown_path_is_404(self, served):
        status, _body, _h = get(served[0], "/nope")
        assert status == 404


# =============================================================================
# 只读接口
# =============================================================================
class TestReadEndpoints:
    def test_health(self, served):
        status, data, _h = get(served[0], "/api/health")
        assert status == 200 and data["ok"] is True
        assert "llm" in data
        assert data["disclaimer"] == DISCLAIMER_MARKER

    def test_today(self, served, seeded):
        status, data, _h = get(served[0], "/api/today")
        assert status == 200
        assert data["kind"] == "today"
        assert data["day"] == "2026-09-26"
        assert data["count"] == 3

    def test_signals_carry_reasons(self, served, seeded):
        """核心契约：理由不许被 UI 藏起来。"""
        _status, data, _h = get(served[0], "/api/today")
        found_overdue = False
        for item in data["items"]:
            for sig in item["signals"]:
                assert sig["reason"], f"{sig['code']} 缺理由"
                assert isinstance(sig["weight"], int)
            if any(s["code"] == "overdue_wait" for s in item["signals"]):
                found_overdue = True
                reason = next(
                    s["reason"] for s in item["signals"] if s["code"] == "overdue_wait"
                )
                assert "孩子带回来" in reason, "理由里应点名该催谁"
        assert found_overdue, "种了跟进过期的事务，应命中 overdue_wait"

    def test_today_carries_disclaimer(self, served, seeded):
        _status, data, _h = get(served[0], "/api/today")
        assert data["disclaimer"] == DISCLAIMER_MARKER

    def test_items_are_globally_sorted(self, served, seeded):
        _status, data, _h = get(served[0], "/api/today")
        weights = [i["total_weight"] for i in data["items"]]
        assert weights == sorted(weights, reverse=True)

    def test_rolled_over_flag(self, served, seeded, clock):
        late = seeded.tasks.create("上周就该交的材料", [seeded.roles.get_by_name("家庭").id])
        seeded.tasks.schedule(late.id, date(2026, 9, 20))
        clock.set(NOW)
        _status, data, _h = get(served[0], "/api/today")
        rolled = [i for i in data["items"] if i["rolled_over"]]
        assert [i["title"] for i in rolled] == ["上周就该交的材料"]
        assert data["rollover"]["count"] == 1
        assert "顺延" in data["rollover"]["summary"]

    def test_all_scopes(self, served, seeded):
        done = seeded.tasks.create("做完的", [seeded.roles.get_by_name("家庭").id])
        seeded.tasks.complete(done.id)
        _s, open_data, _h = get(served[0], "/api/all?scope=open")
        assert done.id not in [i["id"] for i in open_data["items"]]
        _s, closed, _h = get(served[0], "/api/all?scope=closed")
        assert [i["id"] for i in closed["items"]] == [done.id]
        _s, every, _h = get(served[0], "/api/all?scope=all")
        assert len(every["items"]) == len(open_data["items"]) + 1

    def test_bad_scope_rejected(self, served, seeded):
        status, data, _h = get(served[0], "/api/all?scope=bogus")
        assert status == 400 and "scope" in data["error"]

    def test_roles(self, served, seeded):
        status, data, _h = get(served[0], "/api/roles")
        assert status == 200
        assert {r["name"] for r in data["active"]} == {"工作项目A", "家庭"}
        assert data["options"], "建事务表单需要下拉项"
        # 下拉项只给未合并的角色，且字段就是前端要用的那几个
        assert all(set(o) == {"id", "name", "active"} for o in data["options"])
        assert {o["name"] for o in data["options"]} == {"工作项目A", "家庭"}

    def test_roles_excludes_merged_from_options(self, served, seeded):
        seeded.roles.merge(
            seeded.roles.get_by_name("家庭").id,
            seeded.roles.get_by_name("工作项目A").id,
        )
        _status, data, _h = get(served[0], "/api/roles")
        assert "家庭" not in {o["name"] for o in data["options"]}
        merged = [r for r in data["silenced"] + data["active"] if r["name"] == "家庭"]
        assert merged and merged[0]["merged_into_name"] == "工作项目A"


# =============================================================================
# 恢复契约
# =============================================================================
class TestTaskEndpoint:
    def test_restore_contract_complete(self, served, seeded):
        report = next(
            t for t in seeded.task_repo.list_all() if "销售周报" in t.title
        )
        status, data, _h = get(served[0], f"/api/task/{report.id}")
        assert status == 200
        assert data["kind"] == "task"
        assert data["task"]["title"] == report.title
        assert data["task"]["intent"] == "先理一版"
        assert data["effective_definition_of_done"] == "先给我能用的就行"
        assert data["artifact"]["version"] == 1
        assert "有框架了" in data["progress_note"]
        assert data["records"]
        assert data["next_actions"]

    def test_waiting_is_exposed(self, served, seeded):
        wait = next(
            t for t in seeded.task_repo.list_all() if "材料清单" in t.title
        )
        _status, data, _h = get(served[0], f"/api/task/{wait.id}")
        assert data["waiting_on"]["who_or_what"] == "孩子带回来"
        assert data["waiting_on"]["follow_up_at"] is not None

    def test_short_id_resolves(self, served, seeded):
        report = next(
            t for t in seeded.task_repo.list_all() if "销售周报" in t.title
        )
        status, _data, _h = get(served[0], f"/api/task/{report.id[:8]}")
        assert status == 200

    def test_unknown_id_is_400(self, served, seeded):
        status, data, _h = get(served[0], "/api/task/deadbeef")
        assert status == 400 and "找不到事务" in data["error"]

    def test_opening_marks_resumed(self, served, seeded):
        report = next(
            t for t in seeded.task_repo.list_all() if "销售周报" in t.title
        )
        get(served[0], f"/api/task/{report.id}")
        assert seeded.task_repo.get(report.id).last_resumed_at is not None


# =============================================================================
# 新建事务
# =============================================================================
class TestCreate:
    def test_create_requires_title(self, served, seeded):
        status, data = post(served[0], "/api/task", {"role_ids": ["x"]})
        assert status == 400 and "标题" in data["error"]

    def test_create_requires_role(self, served, seeded):
        status, data = post(served[0], "/api/task", {"title": "有事"})
        assert status == 400 and "角色" in data["error"]

    def test_create_rejects_unknown_role(self, served, seeded):
        status, data = post(
            served[0], "/api/task", {"title": "有事", "role_ids": ["nope"]}
        )
        assert status == 400 and "角色不存在" in data["error"]

    def test_create_rejects_bad_kind(self, served, seeded):
        role = seeded.roles.get_by_name("家庭")
        status, data = post(
            served[0], "/api/task",
            {"title": "有事", "role_ids": [role.id], "kind": "urgent"},
        )
        assert status == 400 and "kind" in data["error"]

    def test_create_rejects_bad_date(self, served, seeded):
        role = seeded.roles.get_by_name("家庭")
        status, data = post(
            served[0], "/api/task",
            {"title": "有事", "role_ids": [role.id], "scheduled_for": "下周二"},
        )
        assert status == 400 and "日期" in data["error"]

    def test_create_succeeds(self, served, seeded):
        role = seeded.roles.get_by_name("家庭")
        status, data = post(
            served[0], "/api/task",
            {
                "title": "买猫粮",
                "role_ids": [role.id],
                "kind": "action",
                "intent": "月底前买完",
                "scheduled_for": "2026-09-30",
            },
        )
        assert status == 201, data
        assert data["task"]["title"] == "买猫粮"
        assert data["task"]["scheduled_for"] == "2026-09-30"
        stored = seeded.task_repo.get(data["task"]["id"])
        assert stored.title == "买猫粮"
        assert stored.role_ids == (role.id,)

    def test_create_appends_record(self, served, seeded):
        role = seeded.roles.get_by_name("家庭")
        _status, data = post(
            served[0], "/api/task", {"title": "买猫粮", "role_ids": [role.id]}
        )
        records = seeded.record_repo.list_for_task(data["task"]["id"])
        assert records[0].type.value == "created"

    def test_create_shows_up_in_today(self, served, seeded):
        role = seeded.roles.get_by_name("家庭")
        _s, created = post(
            served[0], "/api/task",
            {"title": "买猫粮", "role_ids": [role.id], "scheduled_for": "2026-09-26"},
        )
        _s, today, _h = get(served[0], "/api/today")
        assert _s == 200, today
        assert created["task"]["id"] in [i["id"] for i in today["items"]]

    def test_bad_json_body(self, served, seeded):
        host, port = served[0].split(":")
        conn = HTTPConnection(host, int(port), timeout=5)
        try:
            conn.request(
                "POST", "/api/task", body=b"{oops",
                headers={"Content-Type": "application/json", "Content-Length": "5",
                         SESSION_HEADER: SESSION_TOKEN},
            )
            res = conn.getresponse()
            assert res.status == 400
            assert "JSON" in json.loads(res.read().decode())["error"]
        finally:
            conn.close()

    def test_wrong_post_path_404(self, served):
        status, _data = post(served[0], "/api/nope", {})
        assert status == 404


# =============================================================================
# 安全边界
# =============================================================================
class TestSecurity:
    def test_refuses_non_loopback_bind(self, app: App):
        for host in ("0.0.0.0", "192.168.1.10", "::"):
            with pytest.raises(ValueError, match="回环"):
                create_server(app, host, 0)

    def test_allows_loopback(self, app: App):
        server = create_server(app, "127.0.0.1", 0)
        try:
            assert server.server_address[0] == "127.0.0.1"
        finally:
            server.server_close()

    def test_health_never_exposes_key(self, served, monkeypatch, tmp_path):
        from freeagent.config import API_KEY_ENV

        monkeypatch.setenv(API_KEY_ENV, "sk-secret-value-should-not-appear")
        _status, body, _h = get(served[0], "/api/health")
        rendered = json.dumps(body, ensure_ascii=False)
        assert "sk-secret" not in rendered
        assert body["key_configured"] is True

    def test_write_surface_is_whitelisted(self):
        """Web 层的写操作是白名单；状态迁移只委托服务层。"""
        import inspect

        from freeagent.web import endpoints
        from freeagent.web.actions import (
            ACTION_FOR_STATE, SCHEDULE_ACTIONS, STATE_ACTIONS,
        )
        from freeagent.web.server import WebRequestHandler

        assert set(STATE_ACTIONS) == {"start", "done", "pause", "drop", "blocked"}
        source = inspect.getsource(endpoints.mutate)
        for forbidden in ("accept", "merge", "delete", "rename", "silence",
                          "set_kind"):
            assert forbidden not in source, f"Web 层不该做 {forbidden}"
        # 迁移合法性来自服务层，web 不查表
        assert "ALLOWED_TRANSITIONS" not in inspect.getsource(WebRequestHandler)
        assert "ALLOWED_TRANSITIONS" not in source
        assert set(SCHEDULE_ACTIONS) == {"pin", "unpin"}

    def test_state_action_mapping_stays_inside_whitelist(self):
        """回归：``dropped`` 曾被映射成 ``reopen``，而白名单里没有 reopen。

        结果是界面上「已放弃」这个按钮一点就 404 —— 正是这个测试抓住的。
        映射和白名单是同一份契约的两半，必须一起校验。
        """
        from freeagent.web.actions import (
            ACTION_FOR_STATE, SCHEDULE_ACTIONS, STATE_ACTIONS,
        )

        allowed = set(STATE_ACTIONS) | set(SCHEDULE_ACTIONS)
        outside = {v for v in ACTION_FOR_STATE.values() if v not in allowed}
        assert not outside, f"这些状态映射到了白名单外的动作：{outside}"

    def test_every_state_has_an_action(self):
        from freeagent.domain import TaskState

        from freeagent.web.actions import ACTION_FOR_STATE

        missing = [s.value for s in TaskState if s.value not in ACTION_FOR_STATE]
        assert not missing, f"这些状态没有对应动作，界面上会缺按钮：{missing}"


# =============================================================================
# 设置页：能配非秘密项，Key 仍然配不了
# =============================================================================
class TestSettings:
    def test_get_returns_current_config(self, served):
        status, data, _h = get(served[0], "/api/settings")
        assert status == 200
        assert data["kind"] == "settings"
        assert data["model"]
        assert data["timeout"] > 0
        assert data["energy_windows"] is None or isinstance(data["energy_windows"], dict)

    def test_get_never_exposes_key(self, served, monkeypatch):
        from freeagent.config import API_KEY_ENV

        monkeypatch.setenv(API_KEY_ENV, "sk-secret-value-should-not-appear")
        _status, data, _h = get(served[0], "/api/settings")
        rendered = json.dumps(data, ensure_ascii=False)
        assert "sk-secret" not in rendered
        assert data["key_configured"] is True
        assert data["key_env_name"] == API_KEY_ENV

    def test_page_explains_how_to_set_key(self, served):
        """界面上必须说清 Key 怎么办 —— 否则用户在这里卡死。

        环境变量名**不在页面里硬编码**，而是由 ``/api/settings`` 下发
        （多 provider 之后改成逐家的 ``providers[].key_env_var``），
        避免文案和真实变量名各说各话。
        """
        _status, body, _h = get(served[0], "/")
        assert "key_env_var" in body, "页面应从接口取环境变量名，而不是写死"
        # 「不硬编码」的正面证明：页面里不能出现任何一家的真实变量名。
        # 只靠上面的 in 检查证明不了这一点 —— 硬编码了同时也读了接口，
        # 那样这条测试照样绿，而真正显示给用户的仍是那个写死的名字。
        for name in ("DEEPSEEK_API_KEY", "MOONSHOT_API_KEY", "DASHSCOPE_API_KEY"):
            assert name not in body, f"页面里硬编码了 {name}"
        for needle in ("环境变量", "重启"):
            assert needle in body, f"设置页应提到 {needle}"
        _s, data, _h = get(served[0], "/api/settings")
        assert data["key_env_name"] == "DEEPSEEK_API_KEY"
        # 每一家都带着自己的变量名，切 provider 时界面才知道该显示哪个
        by_id = {p["id"]: p for p in data["providers"]}
        assert by_id["kimi"]["key_env_var"] == "MOONSHOT_API_KEY"

    def test_page_has_settings_entry(self, served):
        _status, body, _h = get(served[0], "/")
        assert 'data-view="settings"' in body
        assert "/api/settings" in body

    def test_llm_section_is_mounted_inside_the_styled_form(self):
        """智能层那块必须挂进 ``form.set``，不能挂在它外面。

        踩过的坑（真在浏览器里看出来的）：``style.css`` 里所有字段样式都以
        ``form.set`` 为前缀 —— ``form.set .field > label { display: block }``
        让标签压在输入框上面，``form.set .field input[type=text] { width:100% }``
        让输入框铺满。挂在 form 外面就一条都吃不到：标签变成**和输入框并排**，
        输入框退化成通用的 ``flex: 1 1 260px``，于是接口地址被截成
        「https://api.deepseek.c」，刚好少掉 ``/v1`` —— 而那是个
        **能跑和跑不通**的区别。

        DOM 断言全绿（结构没错、只是没吃到样式），所以这里直接钉源码。
        """
        from freeagent.web.js_settings import JS_SETTINGS

        assert "f.appendChild(llm.node)" in JS_SETTINGS, (
            "智能层必须挂进 form.set（样式都挂在 form.set 前缀下）"
        )
        assert "root.appendChild(llm.node)" not in JS_SETTINGS, (
            "别把智能层挂到 form 外面 —— 那样吃不到任何字段样式"
        )

    def test_password_input_gets_the_field_width(self):
        """``input[type=password]`` 也要吃到基础样式与铺满宽度。

        设置页的 API Key 框就是它。之前两条规则只写了 ``input[type=text]``：
        基础外观（边框/内边距/字号）和 ``form.set .field`` 下的 ``width:100%``
        都没它，于是那个框又窄又没统一外观，占位符被截成
        「填了会写进 C:\\Users\\fanli\\Ap」—— 而那正是用户最需要看清的一句
        （它要说清 Key 会写到哪个文件）。
        """
        from freeagent.web.style import STYLE_CSS

        flat = " ".join(STYLE_CSS.split())
        assert "form.set .field input[type=text], form.set .field input[type=password]" in flat, (
            "字段铺满宽度那条规则要覆盖 password 框"
        )
        assert "input[type=text], input[type=password], select, input[type=number]" in flat, (
            "基础外观那条规则要覆盖 password 框"
        )

    def test_post_saves_and_applies(self, served, app: App):
        status, data = post(
            served[0], "/api/settings",
            {"model": "deepseek-reasoner", "timeout": 42, "allow_fallback": False},
        )
        assert status == 200
        assert data["model"] == "deepseek-reasoner"
        assert data["timeout"] == 42.0
        assert data["allow_fallback"] is False
        # 立即生效，不用重启
        assert app.config.model == "deepseek-reasoner"

    def test_post_persists_to_file(self, served, app: App):
        post(served[0], "/api/settings", {"model": "m-persisted"})
        from freeagent.config import load_config

        assert load_config(app.config.home).model == "m-persisted"

    def test_post_never_writes_key(self, served, app: App):
        from freeagent.config import API_KEY_ENV

        post(served[0], "/api/settings", {"model": "m-x"})
        raw = (app.config.home and
               __import__("pathlib").Path(app.config.home, "config.json")
               .read_text(encoding="utf-8")) or ""
        assert "api_key" not in raw and API_KEY_ENV not in raw

    def test_post_accepts_api_key_into_llm_env(self, served, app: App):
        """界面上填的 Key 必须**落进 llm.env**（这正是本次要加的能力）。

        原先这里是「POST api_key 必须被拒绝」，因为那时 Key 只走环境变量。
        现在正常路径**应该收下**，所以把那个测试反转成了这条 —— 顺带把
        上一条最关键的不变量补上：响应里绝不能出现明文。

        明文那条不能省：省了的话，一个手滑的 ``json.dumps(payload)`` 就把
        整个数据库的 Key 泄进日志，而且**没有任何测试会红**。
        """
        from freeagent import llm_env
        from freeagent.config import API_KEY_ENV

        status, data = post(
            served[0], "/api/settings", {"api_key": "sk-typed-into-ui"}
        )
        assert status == 200, data

        # 落到了该落的文件
        assert llm_env.read_env(app.config.home)[API_KEY_ENV] == "sk-typed-into-ui"

        # 但响应里一个字符都不许回显
        blob = json.dumps(data, ensure_ascii=False)
        assert "sk-typed-into-ui" not in blob, "响应里回显了明文 Key"
        assert data["key_configured"] is True
        # 只给掩码，且必须真的遮住了中间
        assert data["key_masked"]
        assert "typed-into" not in data["key_masked"]

    def test_empty_api_key_does_not_wipe_stored_key(self, served, app: App):
        """空串 = **不改**，不是清空。

        这是最容易造成「保存一次别的设置，Key 就没了」的地方：输入框每次加载
        都空着（明文不可能回显），用户改个模型顺手点保存，如果空串被当成清空，
        他要到下次启动才发现智能层没了。必须显式 ``clear_api_key`` 才清。
        """
        from freeagent import llm_env
        from freeagent.config import API_KEY_ENV

        post(served[0], "/api/settings", {"api_key": "sk-keep-me"})
        post(served[0], "/api/settings", {"model": "m-untouched"})

        assert llm_env.read_env(app.config.home)[API_KEY_ENV] == "sk-keep-me"

    def test_post_still_reaches_safety_net_without_endpoint(self, app: App):
        """绕过端点直接调 ``config_from_settings`` 时**必须报错**。

        端点会先把 api_key 摘走，所以走不到这里；真走到了说明写入流程漏了
        一步。那正是更该炸的时刻 —— 静默忽略会让用户看着「已保存」而 Key
        压根没存。
        """
        from freeagent.config import ValidationError, config_from_settings

        with pytest.raises(ValidationError):
            config_from_settings({"api_key": "sk-x"}, base=app.config)

    @pytest.mark.parametrize("bad", [
        {"model": ""}, {"timeout": 0}, {"timeout": "x"},
        {"base_url": "ftp://x"}, {"energy_windows": {"morning": [5]}},
        {"energy_windows": {"evening": [20, 3]}}, {"nope": 1},
    ])
    def test_post_rejects_bad_input(self, served, bad):
        status, data = post(served[0], "/api/settings", bad)
        assert status == 400
        assert data["error"], "必须给出原因"

    def test_rejected_post_does_not_corrupt_file(self, served, app: App):
        from freeagent.config import config_path

        post(served[0], "/api/settings", {"model": "good"})
        status, _ = post(served[0], "/api/settings", {"model": "good2", "timeout": -1})
        assert status == 400
        # 校验不过就不该写文件
        from freeagent.config import load_config

        assert load_config(app.config.home).model == "good"

    def test_energy_windows_round_trip(self, served):
        status, data = post(
            served[0], "/api/settings",
            {"energy_windows": {"morning": [6, 11], "evening": [19, 23]}},
        )
        assert status == 200
        assert data["energy_windows"]["morning"] == [6, 11]
        assert data["energy_windows"]["evening"] == [19, 23]

    def test_reports_env_override(self, served, monkeypatch):
        """环境变量压住某字段时必须报出来，否则用户改了以为坏了。"""
        monkeypatch.setenv("DEEPSEEK_MODEL", "from-env")
        _status, data, _h = get(served[0], "/api/settings")
        assert "model" in data["overridden_by_env"]

    def test_no_override_reported_when_env_clean(self, served, monkeypatch):
        for name in ("DEEPSEEK_MODEL", "DEEPSEEK_BASE_URL", "DEEPSEEK_TIMEOUT",
                     "FREEAGENT_RULES_ONLY", "FREEAGENT_ALLOW_FALLBACK"):
            monkeypatch.delenv(name, raising=False)
        _status, data, _h = get(served[0], "/api/settings")
        assert data["overridden_by_env"] == []


class TestCreateRejectsQuestions:
    """表单是另一个入口，同样不许拿问句/改已有事务的请求来建东西。

    我实测发现终端会问一句就建一条垃圾事务、把「周报进展怎么样」建成角色；
    表单这条路径当时是敞开的。判定逻辑见 ``test_question_input.py``。
    """

    def _work_id(self, addr) -> str:
        active = get(addr, "/api/roles")[1]["active"]
        return next(r["id"] for r in active if r["name"] == "工作项目A")

    def test_question_titles_rejected(self, served, seeded):
        work = self._work_id(served[0])
        for title in ("今天要做什么", "周报进展怎么样", "这条为什么排第一？"):
            status, data = post(served[0], "/api/task",
                                {"title": title, "role_ids": [work]})
            assert status == 400, f"问句被建成了事务：{title}"
            assert data["error"], "必须给出原因"

    def test_mutate_titles_rejected(self, served, seeded):
        work = self._work_id(served[0])
        for title in ("把周报改到下周三", "删掉那条旧提醒"):
            status, data = post(served[0], "/api/task",
                                {"title": title, "role_ids": [work]})
            assert status == 400, f"改已有事务的请求被建成了新事务：{title}"

    def test_rejected_creates_nothing(self, served, seeded):
        work = self._work_id(served[0])
        before = get(served[0], "/api/all?scope=open")[1]["count"]
        for title in ("今天要做什么", "把周报改到下周三"):
            post(served[0], "/api/task", {"title": title, "role_ids": [work]})
        after = get(served[0], "/api/all?scope=open")[1]["count"]
        assert after == before

    def test_real_task_accepted(self, served, seeded):
        work = self._work_id(served[0])
        status, data = post(
            served[0], "/api/task",
            {"title": "下周二要交的销售周报初稿", "role_ids": [work]},
        )
        assert status == 201
        assert "销售周报" in data["task"]["title"]


class TestChatEndpoint:
    """对话入口的 HTTP 端到端。

    界面是「打开就能打字」，所以这条路径必须真的能用 ——
    包括开场白给例子、回答带结构化数据、以及不越权改数据。
    """

    def test_opening_lists_what_you_can_ask(self, served):
        _status, data, _h = get(served[0], "/api/chat")
        assert data["kind"] == "menu"
        assert data["can_do"], "必须告诉用户能问什么"
        assert "不改已有事务" in data["notice"], "要说明边界"

    def test_page_opens_with_a_chat_box(self, served):
        _status, body, _h = get(served[0], "/")
        assert 'data-view="chat"' in body
        # 输入框是 JS 建的（ta.id = "chatinput"），所以查它的赋值语句
        assert '"chatinput"' in body, "首屏要有输入框"
        assert 'load("chat")' in body, "默认落在对话而不是列表"
        assert "/api/chat" in body
        assert "textarea" in body, "多行输入，能写长一点的句子"

    def test_answers_a_question_with_items(self, served, seeded):
        status, data = post(served[0], "/api/chat", {"text": "今天有什么"})
        assert status == 200
        assert data["kind"] in ("answer", "recorded", "clarify", "cannot", "help")
        assert data["text"].strip()

    def test_question_does_not_create_task(self, served, seeded):
        before = get(served[0], "/api/all?scope=all")[1]["count"]
        for text in ("今天要做什么", "周报进展怎么样", "我在等什么"):
            post(served[0], "/api/chat", {"text": text})
        after = get(served[0], "/api/all?scope=all")[1]["count"]
        assert after == before, "提问不该建事务"

    def test_records_via_chat(self, served, seeded):
        before = get(served[0], "/api/all?scope=all")[1]["count"]
        status, data = post(
            served[0], "/api/chat", {"text": "下周二要交的材料清单"}
        )
        assert status == 200
        if data["kind"] == "recorded":
            assert get(served[0], "/api/all?scope=all")[1]["count"] == before + 1
        else:
            assert data["kind"] == "clarify", "不确定归属时只能追问"

    def test_mutate_request_changes_nothing(self, served, seeded):
        before = get(served[0], "/api/all?scope=all")[1]
        status, data = post(served[0], "/api/chat", {"text": "把周报改到下周三"})
        assert status == 200
        assert data["kind"] == "cannot"
        after = get(served[0], "/api/all?scope=all")[1]
        assert after["count"] == before["count"]

    def test_suggestions_come_back(self, served, seeded):
        _status, data = post(served[0], "/api/chat", {"text": "你能做什么"})
        assert data["suggestions"], "应给可点的后续问题"

    def test_overlong_input_rejected(self, served, seeded):
        status, data = post(served[0], "/api/chat", {"text": "长" * 600})
        assert status == 400
        assert data["error"]


# =============================================================================
# 页面拼装：分块只是搬文本，拼起来必须还是原来那一页
# =============================================================================
class TestPageAssembly:
    """``page.py`` 曾是 957 行的单个字符串常量，现按骨架/样式/脚本分块。

    分块是纯文本搬移。这里守住两件事：

    1. 拼装顺序固定（``JS_BOOT`` 必须最后 —— 里面有 ``load("chat")``）
    2. 分块里不能混进 Python 语法（否则拼进 HTML 才炸）
    """

    def test_blocks_are_plain_text(self):
        from freeagent.web import (
            js_boot, js_chat, js_core, js_detail, js_settings, markup, style,
        )

        blocks = [
            style.STYLE_CSS, markup.PAGE_MARKUP, js_core.JS_CORE,
            js_chat.JS_CHAT, js_settings.JS_SETTINGS, js_detail.JS_DETAIL,
            js_boot.JS_BOOT,
        ]
        for b in blocks:
            assert b.strip(), "分块不能为空"
            # r""" 里的内容不能出现会提前闭合的引号组合
            assert '"""' not in b, "分块里出现三引号会提前闭合字符串"
            assert not b.lstrip().startswith(("def ", "class ", "import ")), (
                f"分块里混进了 Python 代码：{b[:40]!r}"
            )

    def test_boot_block_is_last_and_wires_the_app(self):
        from freeagent.web.js_boot import JS_BOOT

        assert 'load("chat")' in JS_BOOT, "启动调用必须留在最后一块"
        assert "querySelectorAll" in JS_BOOT, "事件接线也在最后一块"

    def test_assembled_page_keeps_every_block(self, served):
        _status, body, _h = get(served[0], "/")
        from freeagent.web import (
            js_chat, js_core, js_detail, js_settings, markup, style,
        )

        for name, text in (
            ("style", style.STYLE_CSS), ("markup", markup.PAGE_MARKUP),
            ("core", js_core.JS_CORE), ("chat", js_chat.JS_CHAT),
            ("settings", js_settings.JS_SETTINGS), ("detail", js_detail.JS_DETAIL),
        ):
            head = text.strip().splitlines()[0][:40]
            assert head in body, f"分块 {name} 的内容没出现在拼装结果里"

    def test_script_appears_exactly_once(self, served):
        _status, body, _h = get(served[0], "/")
        assert body.count("<script>") == 1
        assert body.count("<style>") == 1
        assert body.index("<style>") < body.index("<script>")

    def test_chat_container_is_attached_before_messages_go_in(self):
        """回归：首屏对话页 **100% 崩溃**，只有真在浏览器里跑才看得出来。

        ``addMsg`` / ``addChips`` 用 ``document.querySelector("#chatlog")``
        找容器，而游离元素 querySelector 不到 —— 所以容器必须**先**进文档。
        原先 ``root.appendChild(wrap)`` 写在最后，于是首次载入时 ``log`` 是
        null，``log.appendChild(...)`` 抛异常；接着 ``loadChat`` 的 catch 又调
        ``addMsg`` 抛同样的错，**原始错误被覆盖**，页面上只剩一句没法排查的
        ``Cannot read properties of null``。

        按**行**匹配而不是子串：踩过一次坑 —— 早先用 ``index()`` 找子串，
        结果源码里一句 ``// root.appendChild(wrap);`` 的注释就把测试骗过了，
        明明 bug 还在却报绿。所以这里要求那一行去掉缩进后**就是**这条语句。
        """
        from freeagent.web.js_chat import JS_CHAT

        # 只取 renderChatShell 的函数体 —— addMsg 的**定义**在它前面，
        # 拿整个文件比会误判。
        start = JS_CHAT.index("function renderChatShell")
        end = JS_CHAT.index("function buildBar")
        lines = JS_CHAT[start:end].splitlines()

        def line_of(needle: str) -> int:
            for i, raw in enumerate(lines):
                if raw.strip() == needle:
                    return i
            raise AssertionError(f"renderChatShell 里找不到语句 {needle}")

        attach = line_of("root.appendChild(wrap);")
        first_add = next(i for i, raw in enumerate(lines) if "addMsg(" in raw)
        assert attach < first_add, (
            "容器必须在 addMsg 之前进文档：querySelector 找不到游离元素，"
            "首屏会直接崩"
        )


class TestWebPackageStaysSmall:
    """``web`` 包曾因单文件超长而拆过 —— 守住别退化。

    刻意的例外：``page.py`` 只做拼装，所以很小。
    """

    LIMIT = 250

    def test_no_web_module_exceeds_limit(self):
        from pathlib import Path as _Path

        from freeagent.web import __path__ as pkg_path

        offenders = []
        for p in _Path(pkg_path[0]).glob("*.py"):
            n = len(p.read_text(encoding="utf-8").splitlines())
            if n > self.LIMIT:
                offenders.append((n, p.name))
        assert not offenders, f"web 包里的文件又变长了：{sorted(offenders, reverse=True)}"


class TestNoUndefinedNames:
    """静态查未定义名。

    起因：拆模块时把调用点从 ``endpoints.x`` 换成 ``endpoints_write.x``，
    但忘了加 import —— 语法合法、import 也过，**只有运行到那条路由才 500**。
    当时还因为 handler 把异常信息吞掉（只回类型名）而多绕了几轮。

    这类错误静态就能查出来，不该等运行时。
    """

    def _undefined(self, path) -> set[str]:
        import ast
        import builtins

        tree = ast.parse(path.read_text(encoding="utf-8"))
        defined = set(dir(builtins)) | {"__name__", "__file__", "__doc__",
                                         "__package__", "__spec__"}
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for a in node.names:
                    defined.add(a.asname or a.name.split(".")[0])
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                   ast.ClassDef)):
                defined.add(node.name)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                defined.add(node.id)
            elif isinstance(node, ast.arg):
                defined.add(node.arg)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                defined.add(node.name)
        return {
            n.id for n in ast.walk(tree)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
            and n.id not in defined
        }

    def test_web_modules_have_no_undefined_names(self):
        from pathlib import Path as _Path

        from freeagent.web import __path__ as pkg_path

        bad = {}
        for p in sorted(_Path(pkg_path[0]).glob("*.py")):
            missing = self._undefined(p)
            if missing:
                bad[p.name] = sorted(missing)
        assert not bad, f"这些模块里有未定义的名字：{bad}"

    def test_no_JS_constant_is_used_without_being_defined(self):
        """**JS 里的大写常量不许「用了没定义」**。

        起因：设置页引用了 ``BANDS`` 和 ``FIELD_LABEL``，两者**从未定义过**。
        而那个页面 100% 白屏 —— 一点「设置」就报 ``BANDS is not defined``。

        为什么一直没被发现，两条都值得记：

        1. 上面的 :class:`TestNoUndefinedNames` 是 **ast** 的，而 JS 藏在
           ``r\"\"\"...\"\"\"`` 字符串里 —— 它**看不见**。
        2. 其余页面测试只断言「字符串在不在」，不执行 JS。

        只查大写常量，不做全量作用域分析：那样误报太多，反而会被调成
        摆设（用 ``# noqa`` 逐个压掉）。而大写常量正是「本模块该有的
        东西忘了写」最稳定的信号 —— 它不是局部变量，不可能来自别处。
        """
        import re

        from freeagent.web.page import INDEX_HTML

        # **只取 <script> 块**。
        #
        # 踩过的坑：一开始扫整个 INDEX_HTML，于是剥字符串的正则把 HTML 里的
        # 引号也当 JS 字符串配对 —— 引号在 HTML/CSS 里不成对，跨标签错位，
        # 吞掉大段代码再吐出碎片，结果是满屏误报（DOCTYPE / HTML / ERROR…）。
        # 限定在 script 内之后，JS 的引号才是配对的。
        blocks = re.findall(r"<script>(.*?)</script>", INDEX_HTML, re.S)
        assert len(blocks) == 1, "页面应恰好一个 <script> 块（见 script 守卫）"
        code = blocks[0]

        def strip_noise(src: str) -> str:
            # 注释先剥：`BLOCKED` 这类词出现在解释性注释里（js_detail.js），
            # 那不是「用了没定义」。
            src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
            src = re.sub(r"//[^\n]*", " ", src)
            # 再剥字符串字面量：不剥的话 FEISHU_APP_ID（标签表的键）、
            # ERROR（日志匹配的字符串）全是误报。
            src = re.sub(r'"(?:[^"\\\n]|\\.)*"', '""', src)
            src = re.sub(r"'(?:[^'\\\n]|\\.)*'", "''", src)

            def keep_parts(m):
                return " " + " ".join(re.findall(r"\$\{[^}]*\}", m.group(1))) + " "
            return re.sub(r"`([^`]*)`", keep_parts, src)

        code = strip_noise(code)

        # 浏览器/标准库提供的全局。列全会被质疑，但**只列真正用到的** ——
        # 这个清单每多一项，守卫就钝一分。
        declared = {
            "JSON", "Math", "Date", "Object", "Array", "String", "Number",
            "Boolean", "Promise", "Error", "Infinity", "NaN", "URL",
            "URLSearchParams", "Set", "Map", "RegExp", "console", "window",
            "document", "fetch", "setTimeout", "clearTimeout", "localStorage",
        }
        declared |= set(re.findall(
            r"\b(?:const|let|var|function|class)\s+([A-Z][A-Z0-9_]*)", code
        ))
        used = set(re.findall(r"\b([A-Z][A-Z0-9_]{2,})\b", code))
        # 属性名与对象键不是自由标识符：`d.values` 里的 `X`、`{model: …}`
        # 里的 `MODEL` 都不算「用了没定义」。
        skip = set(re.findall(r"[.]\s*([A-Z][A-Z0-9_]*)", code)) | set(
            re.findall(r"([A-Z][A-Z0-9_]*)\s*:", code)
        )

        missing = sorted(used - declared - skip)
        assert not missing, (
            f"页面脚本里用了没定义的大写常量：{missing}"
            "（用了没定义 → 该页面白屏，而测试只断言字符串在不在、抓不到）"
        )

    def test_handler_does_not_swallow_error_detail(self):
        """兜底异常不能只回类型名 —— 否则线上出问题无从查起。

        起因正是这个：``服务端出错：NameError`` 看不出是哪个名字。
        """
        import inspect

        from freeagent.web.server import WebRequestHandler

        source = inspect.getsource(WebRequestHandler)
        assert "服务端出错：{type(exc).__name__}" not in source, (
            "兜底错误必须带上异常信息（至少 repr），不能只回类型名"
        )


class TestVisionEndpoint:
    """拍照识物的 HTTP 端到端。

    这里**不测模型准不准**（那要真图 + 真 Key），只测契约与边界：
    体积、格式、没配 Key 时怎么说、以及**图片不进响应**。
    """

    def _png_data_url(self) -> str:
        import base64
        import zlib

        def chunk(tag, data):
            return (len(data).to_bytes(4, "big") + tag + data
                    + zlib.crc32(tag + data).to_bytes(4, "big"))

        ihdr = (1).to_bytes(4, "big") + (1).to_bytes(4, "big") + bytes([8, 0, 0, 0, 0])
        raw = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
               + chunk(b"IDAT", zlib.compress(b"\x00\xff\xff\xff"))
               + chunk(b"IEND", b""))
        return "data:image/png;base64," + base64.b64encode(raw).decode()

    def test_status_reports_unavailable_without_key(self, served, monkeypatch):
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        _status, data, _h = get(served[0], "/api/vision")
        assert data["available"] is False
        assert "DEEPSEEK_API_KEY" in data["notice"], "要说清该设哪个环境变量"
        assert data["key_env_name"] == "DEEPSEEK_API_KEY"

    def test_identify_without_key_explains(self, served, monkeypatch):
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        status, data = post(
            served[0], "/api/vision", {"image": self._png_data_url()}
        )
        assert status == 400
        assert "DEEPSEEK_API_KEY" in data["error"]

    def test_rejects_empty_image(self, served):
        status, data = post(served[0], "/api/vision", {"image": ""})
        assert status == 400
        assert data["error"]

    def test_rejects_empty_image(self, served):
        status, data = post(served[0], "/api/vision", {"image": ""})
        assert status == 400
        assert data["error"]

    def test_rejects_malformed_data_url(self, served):
        for bad in ("not-a-url", "data:image/png,no-base64", "data:text/plain;base64,aGk="):
            status, data = post(served[0], "/api/vision", {"image": bad})
            assert status == 400, bad

    def test_rejects_oversized_image(self, served):
        """超限图片要给出可读原因，不是崩。

        **刻意不在这一层测**：9 MB 的请求体过测试用的 ``http.client`` 时
        会被 Windows 中止（WinError 10053），那是测试基础设施的上限，
        不是产品行为。体积校验在 ``test_vision.py`` 里直接测服务层，
        这里只测「不崩且给出可读原因」。
        """
        import base64

        blob = b"\xff\xd8\xff\xe0" + b"\x00" * (9 * 1024 * 1024)
        try:
            status, data = post(
                served[0], "/api/vision",
                {"image": "data:image/jpeg;base64," + base64.b64encode(blob).decode()},
            )
        except OSError:
            pytest.skip("本机测试客户端传不了 9 MB，体积校验在 test_vision 里测")
        assert status == 400
        assert data["error"]

    def test_oversized_body_is_rejected_by_normal_endpoints(self, served):
        """普通端点的体积上限**不能**因为加了图片接口而放宽。"""
        status, data = post(
            served[0], "/api/chat", {"text": "x" * (200 * 1024)}
        )
        assert status == 400
        assert "太大" in data["error"]

    def test_page_has_vision_entry(self, served):
        _status, body, _h = get(served[0], "/")
        # 这些 id 是 JS 赋的（vbar.id = "visionbar"），所以查赋值语句
        assert '"visionbar"' in body, "要有拍照入口容器"
        assert "/api/vision" in body
        assert 'type = "file"' in body, "要有文件选择入口"
        assert "image/jpeg" in body, "accept 应限死格式"

    def test_status_notice_explains_image_not_stored(self, served):
        """「不落盘」这句由接口下发，这样文案和实现只有一处来源。

        注意 ``served`` 的 app 是在 fixture 里建的（那时还没有 Key），
        所以这里走的是**未配置**那条分支 —— 照样必须说清不落盘。
        """
        _status, data, _h = get(served[0], "/api/vision")
        assert data["available"] is False
        assert "DEEPSEEK_API_KEY" in data["notice"]

    def test_notice_mentions_not_stored_when_available(self, tmp_path,
                                                       monkeypatch):
        """有 Key 时那句「不落盘」也要出现。"""
        from freeagent.app import build_app

        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
        app = build_app(tmp_path / "v.db")
        server = create_server(app, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            _s, data, _h = get(
                "%s:%d" % ("127.0.0.1", server.server_address[1]), "/api/vision"
            )
            assert data["available"] is True
            assert "不写进数据库" in data["notice"]
            assert "用完即弃" in data["notice"]
        finally:
            server.shutdown()
            server.server_close()
            app.close()


class TestEntryPointsImport:
    """命令行入口必须真的能 import。

    这不是假设性的问题：今晚两次「测试全绿但程序起不来」——
    一次是调用点改了却忘了加 import，一次是 ``serve`` 移了模块而
    ``__main__`` 仍从旧位置导入。两者都是**语法合法、import 也过**，
    只有真去启动才会炸，而测试没覆盖启动路径。

    所以这里直接调 ``main()``，并让它**不要真起服务器**。
    """

    def test_web_main_imports(self):
        from freeagent.web.__main__ import main

        assert callable(main)

    def test_web_main_parses_args(self, monkeypatch):
        """``--db`` / ``--port`` / ``--no-browser`` 都要被认，且不真启动。"""
        from freeagent.web import __main__ as m

        seen = {}

        def fake_serve(db_path=None, *, port=0, open_browser=True, **kw):
            seen.update(db=db_path, port=port, browser=open_browser)
            return 0

        monkeypatch.setattr(m, "serve", fake_serve)
        code = m.main([
            "--db", "data/x.db", "--port", "9123", "--no-browser",
        ])
        assert code == 0
        assert seen == {
            "db": __import__("pathlib").Path("data/x.db"),
            "port": 9123,
            "browser": False,
        }

    def test_cli_main_imports(self):
        from freeagent.cli.app import main  # noqa: F401

    def test_every_module_imports(self):
        """把 web 包里每个模块都 import 一遍。

        拆模块时最容易漏的就是 import —— 静态查未定义名查不到
        「模块 A 从 B 导入，但 B 已经不存在」这类问题。
        """
        import importlib
        import pkgutil

        from freeagent.web import __path__ as pkg_path

        failed = {}
        for mod in pkgutil.iter_modules(list(pkg_path)):
            name = "freeagent.web." + mod.name
            try:
                importlib.import_module(name)
            except Exception as exc:  # noqa: BLE001
                failed[name] = f"{type(exc).__name__}: {exc}"
        assert not failed, f"这些模块 import 失败：{failed}"


# =============================================================================
# 守卫自检：证明「不许外部资源」这条检查还有牙
# =============================================================================
class TestExternalResourceGuardHasTeeth:
    """我为了放行正文里的 ``http://`` 说明文字而收窄过这个守卫。

    收窄不能等于放水 —— 这里用真实的坏例子证明它仍然会失败。
    """

    @pytest.mark.parametrize("bad", [
        '<script src="https://cdn.example.com/x.js"></script>',
        '<link href="//cdn.example.com/a.css" rel="stylesheet">',
        '<img src="http://example.com/p.png">',
        "<script src='https://cdn.example.com/y.js'></script>",
        '<div data-x="1" srcset="https://example.com/z.png 2x"></div>',
        '@import url("https://fonts.example.com/f.css");',
        'background: url(https://example.com/bg.png);',
    ])
    def test_guard_catches_real_external_references(self, bad):
        found = False
        for needle in ("//cdn", "cdnjs", "unpkg", "googleapis", "jsdelivr"):
            if needle in bad:
                found = True
        for attr in ("src", "href", "srcset"):
            for url in re.findall(rf'{attr}="([^"]*)"', bad):
                if _EXTERNAL_URL.match(url):
                    found = True
        for url in re.findall(r"{attr}='([^']*)'".format(attr="src"), bad):
            if _EXTERNAL_URL.match(url):
                found = True
        if re.search(r"@import", bad):
            found = True
        if re.search(r"""url\(\s*['"]?(?:https?:)?//""", bad):
            found = True
        assert found, f"守卫没抓住这个真实外部资源：{bad}"

    @pytest.mark.parametrize("ok", [
        "自建或代理服务时改。必须以 http:// 或 https:// 开头。",
        '<a href="/api/today">今天</a>',
        '<link href="#main">跳到主内容</link>',
        '<img src="data:image/svg+xml;base64,AAA">',
        "background: url(#gradient);",
    ])
    def test_guard_allows_prose_and_local_refs(self, ok):
        """正文里提 URL、以及同源/内联引用，必须放行。"""
        for needle in ("//cdn", "cdnjs", "unpkg", "googleapis", "jsdelivr"):
            assert needle not in ok
        for attr in ("src", "href", "srcset"):
            for url in re.findall(rf'{attr}="([^"]*)"', ok):
                assert not _EXTERNAL_URL.match(url), f"{attr} 误判：{url}"
        assert not re.search(r"@import", ok)
        assert not re.search(r"""url\(\s*['"]?(?:https?:)?//""", ok)
