"""V1.133 四功能的静态接线守卫。

为什么静态：这些模块都是拼进单页 HTML 的 JS 字符串，没有可单测的 Python
逻辑；行为靠浏览器走查。守卫钉的是**接线** —— 路由在不在、按钮接的是不是
它、动态内容有没有先转义，这类错最不容易被发现。
"""

from __future__ import annotations

from datetime import date, datetime

from freeagent.services.week import WeekView
from freeagent.web import routes, serialize_week
from freeagent.web.js_artifact import JS_ARTIFACT
from freeagent.web.js_boot import JS_BOOT
from freeagent.web.js_chat import JS_CHAT
from freeagent.web.js_nav import JS_NAV
from freeagent.web.js_onboard import JS_ONBOARD
from freeagent.web.js_week import JS_WEEK
from freeagent.web.js_workbench import JS_WORKBENCH
from freeagent.web.js_workbench_side import JS_WORKBENCH_SIDE
from freeagent.web.markup import PAGE_MARKUP
from freeagent.web.page import INDEX_HTML


def _body(source: str, fn_name: str) -> str:
    """按大括号配平取函数源码，避免整文件子串误判。"""
    start = source.index(f"function {fn_name}(")
    depth, i = 0, start
    while i < len(source):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start:i + 1]
        i += 1
    raise AssertionError(f"{fn_name} 的大括号不配平")


class TestWeekWeb:
    """周回顾 Web 化：路由 / 载荷 / 导航入口 / 启动分发。"""

    def test_route_and_payload(self):
        assert "/api/week" in routes.READ_ROUTES
        view = WeekView(date(2026, 1, 5), date(2026, 1, 11), (), (), (), (), (), ())
        p = serialize_week.week_payload(view, {})
        assert p["kind"] == "week" and p["start"] == "2026-01-05"
        assert p["turn_count"] == 0 and p["decisions"] == []

    def test_payload_carries_traces(self):
        from freeagent.domain import DecisionLog, Promise

        d = DecisionLog(id="d1", chosen_index=0, options_json="[]",
                        reason_text="因为便宜", conflict_text="A 还是 B",
                        decided_at=datetime.fromisoformat("2026-01-06T09:00:00+08:00"),
                        confront_on="2026-01-13")
        p = Promise(id="p1", task_id="", contact_id="", statement="下周三交",
                    promised_for=datetime.fromisoformat("2026-01-14T00:00:00+08:00"),
                    status="open",
                    created_at="2026-01-07T09:00:00+08:00", updated_at="",
                    resolved_at="", note="")
        view = WeekView(date(2026, 1, 5), date(2026, 1, 11), (), (), (), (), (), (),
                        decisions=(d,), promises=(p,), turn_count=3)
        payload = serialize_week.week_payload(view, {})
        assert payload["decisions"][0]["conflict"] == "A 还是 B"
        assert payload["promises"][0]["statement"] == "下周三交"
        assert payload["turn_count"] == 3

    def test_nav_entry_and_module_in_page(self):
        assert 'data-view="week"' in PAGE_MARKUP
        assert '"week"' in JS_NAV
        assert JS_WEEK in INDEX_HTML

    def test_boot_dispatches_week(self):
        assert 'view === "week"' in JS_BOOT
        assert "renderWeek(root)" in JS_BOOT


class TestArtifactPortable:
    """产物直接可交：复制 / 导出 .md，对任何有正文的稿都给。"""

    def test_copy_bar_is_unconditionally_called(self):
        body = _body(JS_ARTIFACT, "artifactBar")
        assert "artifactCopyBar(data, card)" in body
        # 必须在草稿判定**之前**——否则已采纳版拿不到复制/导出。
        assert body.index("artifactCopyBar(data, card)") < body.index('a.status !== "draft"')

    def test_clipboard_and_blob_download(self):
        body = _body(JS_ARTIFACT, "artifactCopyBar")
        assert "navigator.clipboard.writeText(a.content)" in body
        assert "new Blob([a.content]" in body
        assert ".md" in body

    def test_dynamic_filename_is_sanitized(self):
        body = _body(JS_ARTIFACT, "artifactCopyBar")
        # 反斜杠/引号走 char code（V1.114 的坑），其余逐字符白名单过滤。
        assert 'title.split("")' in body
        assert "String.fromCharCode(92)" in body  # 反斜杠
        assert "String.fromCharCode(34)" in body  # 双引号
        for ch in ('"/"', '":"', '"*"', '"?"', '"<"', '">"', '"|"'):
            assert ch in body

    def test_workbench_side_copy_export(self):
        body = _body(JS_WORKBENCH_SIDE, "wbSidePaint")
        assert "复制答复" in body and "导出 .md" in body
        assert "wbCopyText(turn.text)" in body and "wbExportMd(turn.text)" in body
        # 答复原文走 textContent，不进 innerHTML。
        assert "pre.textContent = turn.text" in body


class TestChatWelcome:
    """对话首屏欢迎区（V1.145）：居中一句介绍 + 可点例子，只填框不发送。

    它取代 V1.133 的可关闭引导卡 —— 空对话本来就该友好，永久关闭是反模式
    （关掉之后首屏只剩一个空框）。例子来自服务端能力清单，不硬编码。
    """

    def test_hero_and_examples_come_from_menu(self):
        body = _body(JS_ONBOARD, "renderChatWelcome")
        assert "你好，我是值守" in body
        assert "试试这样问" in body
        assert "menu.can_do" in body, "例子要来自服务端能力清单，别硬编码"
        assert "cw-hero" in body and "cw-grid" in body

    def test_click_fills_input_without_sending(self):
        body = _body(JS_ONBOARD, "renderChatWelcome")
        assert "ta.value = s" in body
        assert 'new Event("input")' in body
        assert "sendChat" not in body

    def test_notice_is_escaped(self):
        body = _body(JS_ONBOARD, "renderChatWelcome")
        assert "esc(menu.notice" in body, "服务端文案也要先转义"

    def test_hooked_into_chat_shell_and_page(self):
        shell = _body(JS_CHAT, "renderChatShell")
        assert "renderChatWelcome(menu)" in shell
        assert 'log.classList.add("is-empty")' in shell
        assert JS_ONBOARD in INDEX_HTML

    def test_first_message_removes_welcome(self):
        """真机点过才发现的坑：只摘 is-empty 的话，欢迎区仍堆在对话上面。"""
        body = _body(JS_CHAT, "addMsg")
        assert 'log.querySelector(".chat-welcome")' in body
        assert "w.remove()" in body


class TestUsageDisplay:
    """用量可见：读不到就说读不到，绝不显示 0。"""

    def test_workbench_paint_shows_readable_and_unreadable(self):
        body = _body(JS_WORKBENCH, "wbPaint")
        assert "st.usage" in body
        assert 'u.state === "ok"' in body and "Token 输入 " in body
        assert "Token 读不到：" in body

    def test_agent_state_exposes_usage(self):
        from freeagent.web.endpoints_agent import agent_state
        from freeagent.app import build_app

        app = build_app()
        payload = agent_state(app)
        assert "usage" in payload
        assert payload["usage"]["state"] in ("ok", "unreadable")
