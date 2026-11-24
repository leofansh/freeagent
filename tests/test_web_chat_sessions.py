"""多会话前端接线（V1.176）的静态守卫。

浏览器长相不由这里守 —— 这里守的是「状态一处真源」的接线形状：
- 聊天历史按会话分 key（切会话只换 id，不搬消息）；
- 会话栏是两级（项目 → 会话），只调既有端点，不复制写入逻辑；
- boot 必须在 loadChat 画历史之前跑，否则 chatLoad 读错 key；
- 切会话要清右栏轮询（幽灵轮询会把新会话画成旧会话）。
JS 语法另有 ``test_web_js_syntax`` 的 node --check 守着，这里不重复。
"""

from __future__ import annotations

from pathlib import Path

from freeagent.web import (
    js_chat,
    js_chat_sessions,
    js_chat_shell,
    js_chat_store,
    page,
)

_SRC = Path(__file__).resolve().parents[1] / "src" / "freeagent" / "web"


def _read(name: str) -> str:
    return (_SRC / name).read_text(encoding="utf-8")


class TestChatStore:
    def test_chat_history_keyed_by_session(self):
        js = js_chat_store.JS_CHAT_STORE
        assert 'const CHAT_KEY = "freeagent.chat.v1"' in js
        assert "let chatSessionId" in js
        assert "function chatKey()" in js
        assert "chatKey()" in js
        # 三个存取口都走分 key；老 key 只是迁移源，不再直接读写历史。
        assert "localStorage.getItem(chatKey())" in js
        assert "localStorage.setItem(chatKey()" in js
        assert "localStorage.removeItem(chatKey())" in js
        assert "function chatUseSession(sid)" in js
        assert 'CHAT_KEY + ".current"' in js

    def test_old_key_is_not_a_live_store(self):
        js = js_chat_store.JS_CHAT_STORE
        assert "localStorage.getItem(CHAT_KEY)" not in js
        assert "localStorage.setItem(CHAT_KEY," not in js


class TestSessionsModule:
    def test_boot_restores_and_migrates(self):
        js = js_chat_sessions.JS_CHAT_SESSIONS
        assert "async function chatSessionsBoot()" in js
        assert 'CHAT_KEY + ".current"' in js           # 同步恢复（赶在 render 前）
        assert 'api("/api/agent/state")' in js         # 对账服务端 prefs
        assert 'post("/api/agent/session_import"' in js  # 历史迁移建 hist: 会话
        assert "localStorage.removeItem(CHAT_KEY)" in js  # 只搬一次

    def test_paint_is_two_levels_and_reuses_endpoints(self):
        js = js_chat_sessions.JS_CHAT_SESSIONS
        assert "async function chatSessionsPaint()" in js
        assert 'api("/api/agent/projects")' in js
        assert 'api("/api/agent/sessions?directory=" + encodeURIComponent' in js
        for action in ("session_new", "session_switch", "session_rename",
                       "session_delete", "session_import"):
            assert f'"/api/agent/{action}"' in js
        # 动态文本不进 innerHTML（el 第三参是 innerHTML）。
        assert "label.textContent =" in js
        assert "date.textContent =" in js

    def test_switch_clears_stale_poll_and_reloads(self):
        js = js_chat_sessions.JS_CHAT_SESSIONS
        assert "async function chatSessionSwitch(sid)" in js
        assert "chatUseSession(sid)" in js
        assert "history_ = []" in js
        assert "clearInterval(chatSideTimer)" in js
        assert 'load("chat")' in js


class TestWiring:
    def test_page_assembles_sessions_module(self):
        assert "from .js_chat_sessions import JS_CHAT_SESSIONS" in _read("page.py")
        html = page.INDEX_HTML
        assert "async function chatSessionsBoot()" in html
        assert "async function chatSessionsPaint()" in html

    def test_chat_rail_delegates_with_fallback(self):
        js = js_chat_shell.JS_CHAT_SHELL
        assert "async function chatRailPaint()" in js
        assert 'if (typeof chatSessionsPaint === "function")' in js
        assert "await chatSessionsPaint(); return;" in js
        # 模块没拼进来时仍有单层兜底，不让左栏空白。
        assert 'api("/api/agent/projects")' in js

    def test_load_chat_boots_before_render(self):
        js = js_chat.JS_CHAT
        boot = 'if (typeof chatSessionsBoot === "function") await chatSessionsBoot();'
        assert boot in js
        # boot 必须在 renderChatShell 之前，否则 chatLoad 读基座 key 读成空。
        assert js.index(boot) < js.index("renderChatShell(null);", js.index(boot))
