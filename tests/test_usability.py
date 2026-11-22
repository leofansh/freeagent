"""V1.132 易用性五件套的静态守卫。

为什么是静态断言：这些模块全是拼进单页 HTML 的 JS 字符串，没有可单测的
Python 逻辑；真正的行为靠浏览器走查。静态守卫钉住的是**接线** —— 函数有没有
进页面、按钮接的是不是它、转义有没有漏，这类改错最不容易被发现。

为什么不用子任务 / 真实浏览器跑在这里：本仓测试纪律是「时间可注入、不依赖
外部进程」；浏览器走查单独做，不进 pytest。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from freeagent.web.js_chat import JS_CHAT
from freeagent.web.markup import PAGE_MARKUP
from freeagent.web.js_chat_search import JS_CHAT_SEARCH
from freeagent.web.js_chat_shell import JS_CHAT_SHELL
from freeagent.web.js_compact import JS_COMPACT
from freeagent.web.js_settings import JS_SETTINGS
from freeagent.web.js_vision import JS_VISION
from freeagent.web.js_settings_groups import JS_SETTINGS_GROUPS
from freeagent.web.js_workbench import JS_WORKBENCH
from freeagent.web.js_workbench_page import JS_WORKBENCH_PAGE
from freeagent.web.js_workbench_side import JS_WORKBENCH_SIDE
from freeagent.web.page import INDEX_HTML
from freeagent.web.style_nav import STYLE_NAV_CSS
from freeagent.web.style_usability import STYLE_USABILITY_CSS
from freeagent.web.style_workbench import STYLE_WORKBENCH_CSS


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


class TestCompactMode:
    """简洁模式：默认开、可切、状态持久化、只折叠工作台过程。"""

    def test_defaults_on_and_persists(self):
        assert 'const FA_COMPACT_KEY = "fa_compact"' in JS_COMPACT
        body = _body(JS_COMPACT, "compactOn")
        # 只有显式存成 "0" 才是关；读不到按默认开（fail-closed 到简洁）。
        assert 'getItem(FA_COMPACT_KEY) !== "0"' in body
        assert "return true" in body
        toggle = _body(JS_COMPACT, "compactToggle")
        assert 'setItem(FA_COMPACT_KEY, next ? "1" : "0")' in toggle

    def test_applies_body_class_and_wires_button(self):
        apply = _body(JS_COMPACT, "compactApply")
        assert 'classList.toggle("compact", on)' in apply
        init = _body(JS_COMPACT, "compactInit")
        assert 'b.id = "compact-btn"' in init
        assert "b.onclick = compactToggle" in init
        assert 'appendChild(b)' in init  # 按钮真的挂进 nav
        assert 'nav.appendChild(b)' in init

    def test_css_hides_only_process(self):
        assert "body.compact .wb-view .wb-steps" in STYLE_USABILITY_CSS
        assert "body.compact .wb-view .wb-process" in STYLE_USABILITY_CSS
        # 答复正文（.msg .bubble / pre）不在折叠名单里 —— 折叠的是过程不是结论。
        assert ".msg .bubble" not in STYLE_USABILITY_CSS

    def test_process_class_on_footprint_cards(self):
        assert JS_WORKBENCH.count('card wb-process') == 2, (
            "文件 / 命令两张足迹卡都要打 wb-process 标记，简洁模式一起折"
        )

    def test_scripts_assembled(self):
        assert JS_COMPACT in INDEX_HTML
        assert STYLE_USABILITY_CSS in INDEX_HTML


class TestChatSearch:
    """对话内搜索：Ctrl+F、逐条跳、高亮前还原、动态文本走 textContent。"""

    def test_functions_present(self):
        for fn in ("chatSearchOpen", "chatSearchClear", "chatSearchRun",
                   "chatSearchMark", "chatSearchStep", "chatSearchClose"):
            assert f"function {fn}(" in JS_CHAT_SEARCH, f"缺 {fn}"

    def test_ctrl_f_scoped_to_chat(self):
        assert 'addEventListener("keydown"' in JS_CHAT_SEARCH
        assert 'view !== "chat"' in JS_CHAT_SEARCH
        assert "e.preventDefault()" in JS_CHAT_SEARCH

    def test_highlight_restores_original(self):
        clear = _body(JS_CHAT_SEARCH, "chatSearchClear")
        assert "b.dataset.faOrig" in clear
        assert "delete b.dataset.faOrig" in clear
        run = _body(JS_CHAT_SEARCH, "chatSearchRun")
        assert "chatSearchClear()" in run, "搜索前必须还原上次高亮，否则 mark 嵌套"
        assert 'b.dataset.faOrig = raw' in run

    def test_dynamic_text_not_innerhtml(self):
        run = _body(JS_CHAT_SEARCH, "chatSearchRun")
        assert "m.textContent = raw.slice" in run
        assert "innerHTML" not in run, "命中词来自人的输入，不能进 innerHTML"

    def test_delegate_from_chat_is_a_confirmed_explicit_action(self):
        """V1.152：一句话从对话直接派出，目录/档位先给人看一眼。"""
        fn = _body(JS_CHAT_SEARCH, "chatDelegate")
        assert "window.confirm" in fn and "档位" in fn
        assert '"/api/agent/state"' in fn and '"/api/agent/start"' in fn
        assert "force: true" in fn, "人点了交给它就是显式推翻规则"
        tools = _body(JS_CHAT_SEARCH, "chatToolsMount")
        assert 'add("交给它执行", chatDelegate)' in tools

    def test_search_lives_in_the_chat_tools_menu(self):
        tools = _body(JS_CHAT_SEARCH, "chatToolsMount")
        assert "chatSearchOpen" in tools, "搜索必须接上搜索面板"
        assert "查找对话内容" in tools
        bar = _body(JS_CHAT, "buildBar")
        assert "搜索" not in bar, "搜索不再是输入栏上的独立按钮"

    def test_scripts_assembled_in_order(self):
        assert JS_CHAT_SEARCH in INDEX_HTML
        # 搜索依赖 chat 建立的 #chatlog / .msg .bubble 结构，必须排在 chat 之后。
        assert INDEX_HTML.index(JS_CHAT) < INDEX_HTML.index(JS_CHAT_SEARCH)


class TestChatShell:
    """对话页三栏外壳（V1.154）：左工作区 / 中对话 / 右产物，两侧可收。"""

    def test_shell_wraps_center_and_is_wired(self):
        shell = _body(JS_CHAT_SHELL, "chatShellMount")
        assert 'el("div", "chat-shell")' in shell
        assert "chat-rail" in shell and "chat-side" in shell
        assert "chatRailPaint()" in shell and "chatSidePaint()" in shell
        bar = _body(JS_CHAT, "renderChatShell")
        assert "chatShellMount()" in bar

    def test_panels_use_read_only_endpoints(self):
        rail = _body(JS_CHAT_SHELL, "chatRailPaint")
        side = _body(JS_CHAT_SHELL, "chatSidePaint")
        assert '"/api/agent/projects"' in rail and '"/api/agent/project_select"' in rail
        assert '"/api/agent/state"' in side

    def test_bar_has_project_model_mode_selectors(self):
        fn = _body(JS_CHAT_SHELL, "chatBarControls")
        for sel in ("chat-ctrl-project", "chat-ctrl-model", "chat-ctrl-mode"):
            assert f'"{sel}"' in fn
        assert '"/api/agent/prefs_save"' in fn and '"/api/oc/options"' in fn
        assert '"/api/agent/project_select"' in fn
        shell = _body(JS_CHAT_SHELL, "chatShellMount")
        assert "chatBarControls()" in shell

    def test_toggle_buttons_hide_panels(self):
        shell = _body(JS_CHAT_SHELL, "chatShellMount")
        assert "left.hidden = !left.hidden" in shell
        assert "right.hidden = !right.hidden" in shell


class TestButtonHierarchy:
    """按钮层级（V1.148）：主动作 / 工具 / 开关三种角色，样式必须成套。"""

    def test_chat_bar_has_send_arrow_and_paste(self):
        """V1.150：左 + 右 ↑（OpenCode 骨架）；回车仍是主路径，图片可直接粘。"""
        bar = _body(JS_CHAT, "buildBar")
        assert 'el("button", "btn primary send-arrow", "↑")' in bar
        assert "chatsend" in bar and "sendChat()" in bar
        assert "ta.onpaste" in bar and 'it.type.indexOf("image/")' in bar
        assert "doVision(blob)" in bar, "粘贴的图片走同一条识别链路"
        assert 'e.key === "Enter" && !e.shiftKey' in bar

    def test_attachment_is_plus_and_bar_has_no_tool_buttons(self):
        bar = _body(JS_CHAT, "buildBar")
        for tool in ("搜索", "清空", "多选删除"):
            assert f'el("button", "btn ghost", "{tool}")' not in bar, (
                f"{tool} 已收进右上角 … 菜单，不该占输入栏"
            )
        assert 'vbar.id = "visionbar"' in bar, "附件入口要挂在输入框左侧"

    def test_nav_uses_the_same_button_language(self):
        assert 'class="btn ghost">刷新' in PAGE_MARKUP
        assert 'el("button", "btn ghost", "简洁")' in JS_COMPACT

    def test_ghost_css_is_global_and_shows_toggle_state(self):
        assert ".btn.ghost {" in STYLE_NAV_CSS, "ghost 不能只认 nav，要跨区域一致"
        assert '.btn.ghost[aria-pressed="true"]' in STYLE_NAV_CSS

    def test_attachment_plus_replaces_the_lone_vision_pill(self):
        load = _body(JS_VISION, "loadVisionBar")
        assert 'el("button", "btn ghost attach", "+")' in load
        assert "attach-menu" in load and "attach-image" in load
        assert 'el("button", "chip", "📷 拍照识物")' not in load, "独立的拍照胶囊已收进 + 菜单"
        assert "visionOutsideBound" in load, "点外面/esc 关闭的监听只绑一次"
        # V1.150/151：快捷键与分组入口都必须是真能用的，不摆没有的能力。
        assert "Ctrl+U" in load and 'e.key.toLowerCase() === "u"' in load
        assert "attach-paste" in load and "Ctrl+V" in load
        for view in ("roles", "services", "feishu"):
            assert f'"{view}"' in load, f"+ 菜单要能直达 {view}"


class TestSettingsGroups:
    """设置分组：三组标题按依赖顺序挂进 form.set，不改字段挂载点。"""

    def test_helper_uses_section_label(self):
        body = _body(JS_SETTINGS_GROUPS, "settingsGroupLabel")
        assert 'el("div", "section-label", esc(text))' in body

    def test_three_groups_in_order(self):
        labels = ["智能层与模型", "运行与降级", "排序偏好"]
        positions = [JS_SETTINGS.index(f'settingsGroupLabel("{x}")') for x in labels]
        assert positions == sorted(positions), "分组标题顺序错乱"
        # 标题必须和字段一样 append 进 form.set（吃样式），不能挂到 root。
        for x in labels:
            assert f'f.appendChild(settingsGroupLabel("{x}"))' in JS_SETTINGS

    def test_script_assembled_before_settings(self):
        assert JS_SETTINGS_GROUPS in INDEX_HTML
        assert INDEX_HTML.index(JS_SETTINGS_GROUPS) < INDEX_HTML.index(JS_SETTINGS)


class TestWorkbenchSide:
    """产物侧栏：同屏对照、转义、无轮次也清残留、窄屏折回一列。"""

    def test_fill_function(self):
        body = _body(JS_WORKBENCH_SIDE, "wbSidePaint")
        # V1.142 起右栏拆两块：上块 #wb-side-turn 归产物（每轮清空重画），
        # 下块 #wb-files 归「这个工作区有什么」（常驻，不清场）。所以清空
        # 目标从整栏换成了上块 —— 不是迁就实现，是容器契约变了。
        assert '$("#wb-side-turn")' in body
        assert "turn.files" in body
        assert "turn.commands" in body
        assert "turn.text" in body
        assert "side.innerHTML = \"\"" in body, "每次重画先清掉上一轮残留"

    def test_all_model_text_escaped(self):
        body = _body(JS_WORKBENCH_SIDE, "wbSidePaint")
        assert 'wbLocateRow(f, "")' in body, "文件路径走带转义的行渲染器"
        assert "esc(c)" in body
        assert "pre.textContent = turn.text" in body
        assert ".innerHTML = turn" not in body
        row = _body(JS_WORKBENCH_SIDE, "wbLocateRow")
        assert "esc(path)" in row, "路径进 innerHTML 前必须 esc"
        assert "loc.dataset.path = path" in row, "路径进属性、不拼进 HTML 串"

    def test_paint_calls_side(self):
        paint = _body(JS_WORKBENCH, "wbPaint")
        assert "wbSidePaint(turn, st.ledger)" in paint, (
            "侧栏必须由 wbPaint 同步，且带上本会话变更账，不能另起轮询"
        )


class TestWorkbenchChanges:
    """V1.135：变更账三态分开 + 受控定位，不把「读不到」说成「没改」。"""

    def test_ledger_three_states(self):
        body = _body(JS_WORKBENCH_SIDE, "wbPaintLedger")
        assert 'ledger.state === "unreadable"' in body
        assert "别把「没记录」当成「没改」" in body
        assert 'ledger.state === "none"' in body
        assert "这不等于没做事" in body
        assert "f.path" in body and "f.count" in body
        assert "没有逐行差异" in body, "账里只有路径与次数，不许编一个 diff"

    def test_reveal_goes_through_server(self):
        body = _body(JS_WORKBENCH_SIDE, "wbReveal")
        assert 'api("/api/agent/reveal"' in body
        assert "JSON.stringify({ path: path })" in body
        assert "shell" not in body, "前端不碰本地执行"

    def test_container_assembled(self):
        page = JS_WORKBENCH_PAGE
        for cls in ("wb-col-main", "wb-col-side", "wb-cols"):
            assert f'"{cls}"' in page or f"'{cls}'" in page or cls in page
            assert f'"{cls}"' in STYLE_WORKBENCH_CSS or f".{cls}" in STYLE_WORKBENCH_CSS
        assert 'side.id = "wb-side"' in page
        assert 'while (view.firstChild) main.appendChild(view.firstChild)' in page, (
            "搬现成 children，不重建 DOM —— paint/history 的 id 引用照旧"
        )

    def test_narrow_screen_folds(self):
        assert "@media (max-width: 860px)" in STYLE_WORKBENCH_CSS
        assert "flex-direction: column" in STYLE_WORKBENCH_CSS

    def test_script_assembled(self):
        assert JS_WORKBENCH_SIDE in INDEX_HTML
        assert INDEX_HTML.index(JS_WORKBENCH) < INDEX_HTML.index(JS_WORKBENCH_SIDE)


_SRC = Path(__file__).resolve().parent.parent / "src" / "freeagent"


def _js_clean(name: str) -> str:
    """取 JS 模块的上屏串并剥掉注释 —— 注释里的品牌名按决策保留。"""
    raw = (_SRC / "web" / name).read_text(encoding="utf-8")
    body = raw.split('r"""', 1)[1].rsplit('"""', 1)[0]
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    body = re.sub(r"(?<!:)//[^\n]*", "", body)
    return body


def _py_strings(rel: str) -> str:
    """取 Python 模块的字符串字面量 —— 不含注释，只含可能上屏的文案。"""
    tree = ast.parse((_SRC / rel).read_text(encoding="utf-8"))
    return "\n".join(
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    )


class TestEngineNotHighlighted:
    """V1.136：底座不突出显示 —— 用户可见文案不点名引擎品牌，顶栏不摆内部细节。

    注释 / 故障排查文档 / CLI 命令表 / 执行器事实名里的品牌名按决策保留，
    所以这里只扫 JS 上屏串（剥注释）与 Python 字符串字面量（不含注释），
    逐模块钉死旧文案不再出现、新中性文案在位。
    """

    #: (JS 模块, 必须消失的旧串, 必须出现的新串)
    _JS_CASES = (
        ("js_workbench.py", ("eng.pid", "eng.session_id", "eng.directory"),
         ('$("#wb-engine")', "Token 读不到：")),
        ("js_delegate_projects.py", ("读不到 OpenCode", "OpenCode 里还没有",
                                      "OpenCode 注册表", "白名单（OpenCode"),
         ("读不到后台引擎", "后台引擎的项目注册表")),
        ("js_oc_selection.py", ("交给 opencode", "连不上 OpenCode",
                                 "opencode --version", "正在问 opencode"),
         ("连不上后台引擎", "正在问引擎")),
        ("js_oc_cards.py", ("OpenCode 没起来", "OpenCode Desktop",
                             "改 OpenCode 的配置", "OpenCode 那个接口",
                             "配凭据请在 OpenCode", "换行会让 opencode 判成复杂任务",
                             "python -m freeagent.delegate"),
         ("后台引擎没起来", "换行会被判成复杂任务",
          "在「后台服务」里确认它在跑")),
        ("js_delegate_projects.py", ("tools\\run_feishu.bat",
                                     "python -m freeagent.delegate"),
         ("「后台服务」里重启桥接与执行器",)),
    )

    #: (相对 src/freeagent 的路径, 必须消失的旧串, 必须出现的新串)
    _PY_CASES = (
        ("web/endpoints_oc_selection.py", ("或 OpenCode 已更新",), ("或后台引擎已更新",)),
        ("web/endpoints_oc_selection_write.py", ("或 OpenCode 不再提供",), ("或后台引擎不再提供",)),
        ("web/endpoints_delegate_action.py", ("opencode 继续干活",), ("它继续干活",)),
        ("web/endpoints_oc_dispatch.py", ("含换行时 opencode 会判成复杂任务",),
         ("含换行时会被判成复杂任务",)),
        ("services/oc_discovery.py", ("OpenCode 没起来", "查不到 OpenCode 的工作模式列表",
                                      "先在 OpenCode 里登录", "OpenCode 的 /provider"),
         ("后台引擎没起来",)),
        ("services/capability.py", ("让 opencode 改", "交给 opencode 做", "让 opencode 看看"),
         ("让它改", "交给引擎做")),
        ("services/svcs.py", ("交给 opencode；执行期逐次授权",), ("交给后台引擎",)),
        ("feishu/bridge.py", ("让 opencode 改代码", "opencode 继续干活", "交给 opencode 改代码"),
         ("让它改代码", "交给它改代码")),
        ("feishu/sender.py", ("被 opencode 判成复杂任务",), ("被判成复杂任务",)),
        ("delegate.py", ("或 OpenCode 已更新。请到「编程」页签",),
         ("或后台引擎已更新", "请到「委派总览」页")),
        ("services/engine_errors.py", ("在「编程」页签",), ("在「委派总览」页",)),
        ("web/endpoints_oc_dispatch.py", ("python -m freeagent.delegate",),
         ("在「后台服务」里确认它在跑",)),
    )

    def test_js_onscreen_text_has_no_engine_brand(self):
        for name, gone, kept in self._JS_CASES:
            body = _js_clean(name)
            for old in gone:
                assert old not in body, f"{name} 仍在上屏串里露出品牌/内部细节：{old}"
            for new in kept:
                assert new in body, f"{name} 丢了应保留的中性文案：{new}"

    def test_py_user_text_has_no_engine_brand(self):
        for rel, gone, kept in self._PY_CASES:
            blob = _py_strings(rel)
            for old in gone:
                assert old not in blob, f"{rel} 仍有用户可见旧文案：{old}"
            for new in kept:
                assert new in blob, f"{rel} 丢了中性文案：{new}"

    def test_facts_and_fault_hints_kept(self):
        """允许保留的例外不能误伤：执行器事实名 / 故障排查命令 / 审批 subject。"""
        # 执行器事实名：test_capability_route 钉的就是它。
        assert "问 opencode 在不在" in _py_strings("services/capability.py")
        # 故障排查提示：品牌名留在排障文档里（用户决策）。
        assert "opencode --version" in _py_strings("services/engine_host.py")
        # 审批卡 subject 属闸门语义区，本轮不动。
        assert "opencode 想" in _py_strings("services/approval.py")
