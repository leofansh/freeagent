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
from freeagent.web.js_chat_projects import JS_CHAT_PROJECTS
from freeagent.web.js_chat_intake import JS_CHAT_INTAKE
from freeagent.web.js_settings import JS_SETTINGS
from freeagent.web.js_vision import JS_VISION
from freeagent.web.js_settings_groups import JS_SETTINGS_GROUPS
from freeagent.web.js_workbench import JS_WORKBENCH
from freeagent.web.js_workbench_page import JS_WORKBENCH_PAGE
from freeagent.web.js_workbench_side import JS_WORKBENCH_SIDE
from freeagent.web.page import INDEX_HTML
from freeagent.web.style_chat import STYLE_CHAT_CSS
from freeagent.web.style_contrast import STYLE_CONTRAST_CSS
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


def _css_rule(source: str, selector: str) -> str:
    """取一条 CSS 规则的声明体（从选择器到配平的右大括号）。"""
    start = source.index(selector)
    end = source.index("}", start)
    return source[start:end + 1]


class TestCompactModeRemoved:
    """V1.159：简洁模式已删 —— 它在对话页没有 .wb-view 作用域，点下去毫无效果。

    工作台步骤清单自带「展开全部 N 步 / 收起」的局部开关（V1.130），
    全局 toggle 是冗余的第二真源；连同 localStorage 偏好一起拆干净。
    """

    def test_no_compact_button_or_state_in_the_page(self):
        assert "compact-btn" not in INDEX_HTML
        assert "fa_compact" not in INDEX_HTML
        assert "body.compact" not in INDEX_HTML
        assert "body.compact" not in STYLE_USABILITY_CSS
        assert not (_SRC / "web" / "js_compact.py").exists()


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

    def test_close_listeners_read_live_dom_not_captured_nodes(self):
        """V1.160 补记：关闭监听只绑一次，回调里必须现查 DOM。

        renderChatShell 每次重建 .chat，闭包捕获的 menu/btn 重绘后已不在文档里，
        Esc / 点外关闭会永久失效（V1.142 文件清单同一类坑）。静态守卫钉住
        「回调里现查 #chat-tool-menu / #chat-more-btn」，防以后改回捕获旧节点。
        """
        tools = _body(JS_CHAT_SEARCH, "chatToolsMount")
        bound = tools[tools.index("if (!chatToolsBound)"):]
        # 两个关闭回调各自现查当前 DOM，不直接用外层的 menu / btn。
        assert bound.count('const m = $("#chat-tool-menu"), mb = $("#chat-more-btn")') == 2
        assert bound.count("if (m) m.hidden = true") == 2
        assert bound.count('if (mb) mb.setAttribute("aria-expanded", "false")') == 2
        # 回调里不许直接改外层捕获的 menu / btn。
        keydown = bound[bound.index('"keydown"'):]
        assert "menu.hidden" not in keydown and "btn.setAttribute" not in keydown

    def test_scripts_assembled_in_order(self):
        assert JS_CHAT_SEARCH in INDEX_HTML
        # 搜索依赖 chat 建立的 #chatlog / .msg .bubble 结构，必须排在 chat 之后。
        assert INDEX_HTML.index(JS_CHAT) < INDEX_HTML.index(JS_CHAT_SEARCH)


class TestChatShell:
    """对话页三栏外壳（V1.159）：中栏为主，两侧默认收起、按需从「…」打开。"""

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
        # 项目是按钮（豆包式面板挂载点），其余三个仍是原生 select（V1.165）。
        for sel in ("chat-ctrl-project", "chat-ctrl-model", "chat-ctrl-mode",
                    "chat-ctrl-variant"):
            assert f'"{sel}"' in fn
        assert 'el("button", "chat-ctrl chat-proj-btn")' in fn
        assert "chatProjMount(proj" in fn, "项目选择器交给面板模块挂载"
        assert '"/api/agent/prefs_save"' in fn and '"/api/oc/options"' in fn
        shell = _body(JS_CHAT_SHELL, "chatShellMount")
        assert "chatBarControls()" in shell

    def test_bar_order_puts_variant_next_to_model(self):
        """V1.164：都插在箭头前，声明顺序即显示顺序——推理档紧跟模型、授权档收尾。

        项目是按钮（el），其余是 select（mk），故按各声明行的特征串定位；
        不能用 "chat-ctrl-project" 裸串——幂等守卫那行也含它，会取到错误位置。
        """
        fn = _body(JS_CHAT_SHELL, "chatBarControls")
        markers = ('const proj = el(', 'const model = mk("chat-ctrl-model"',
                   'const variant = mk("chat-ctrl-variant"',
                   'const mode = mk("chat-ctrl-mode"')
        order = [fn.index(m) for m in markers]
        assert order == sorted(order), "显示顺序须为 项目 / 模型 / 推理档 / 授权档"

    def test_variant_selector_cascades_and_has_no_fake_choice(self):
        """V1.163：推理档常驻，但无可用档位时置灰、不造假选择；换模型清档。

        对齐豆包 / OpenCode 的「推理强度常驻输入栏」形态，又守住
        「多数模型 variants=[]」的现实——只有「不指定」一项时就是禁用。
        """
        fn = _body(JS_CHAT_SHELL, "chatBarControls")
        # 选项依附 oc.variant 段；空值项恒在，靠是否有非空档决定是否禁用。
        assert "oc.variant && oc.variant.options" in fn
        assert "usableVariants = variantOpts.filter((o) => o.value)" in fn
        assert "variant.disabled = usableVariants.length === 0" in fn
        assert '"当前模型没有可选推理档"' in fn
        # 重填前清空，换模型重列不堆积旧档位。
        assert "sel.textContent = \"\"" in fn
        # 换模型：保存时带 variant:"" 清档，并重拉该模型档位重填。
        assert "await save({ model: model.value, variant: \"\" })" in fn
        assert "const fresh = await api(\"/api/oc/options\")" in fn
        assert "variant.onchange = () => save({ variant: variant.value })" in fn
        assert '"/api/agent/prefs_save"' in fn, "推理档走既有白名单写路径"

    def test_panels_start_hidden_and_toggle_has_one_source(self):
        """V1.159：第一眼只有对话；栏内按钮与「…」菜单共用同一开合函数。"""
        shell = _body(JS_CHAT_SHELL, "chatShellMount")
        assert "left.hidden = true" in shell and "right.hidden = true" in shell
        toggle = _body(JS_CHAT_SHELL, "chatPanelToggle")
        assert "panel.hidden = !open" in toggle
        assert 'chatPanelToggle("rail", false)' in shell
        assert 'chatPanelToggle("side", false)' in shell
        assert "left.hidden = !" not in shell and "right.hidden = !" not in shell

    def test_chat_tools_menu_can_open_both_panels(self):
        tools = _body(JS_CHAT_SEARCH, "chatToolsMount")
        assert 'chatPanelToggle("rail")' in tools and 'chatPanelToggle("side")' in tools
        assert "打开工作区" in tools and "打开产物" in tools
        # 菜单文案每次打开都按当前状态刷新，不能写着"打开"点了却关掉。
        assert "railItem.textContent = rail.hidden" in tools
        assert "sideItem.textContent = side.hidden" in tools

    def test_shell_fills_the_window(self):
        """撑满靠 CSS :has，不靠 JS 切视图；不支持时静默保持旧宽度。"""
        assert ".wrap:has(.chat-shell)" in STYLE_CHAT_CSS
        assert "max-width: none" in STYLE_CHAT_CSS
        # 面板按钮是栏内标题，不再用虚线 ghost 当占位符。
        assert ".chat-panel-close" in STYLE_CHAT_CSS

    def test_narrow_window_opens_one_panel_at_a_time(self):
        """V1.160：窄屏开一侧自动收另一侧，阈值按视口宽算、不记"后开哪个"。"""
        assert "const CHAT_NARROW = 1000" in JS_CHAT_SHELL
        toggle = _body(JS_CHAT_SHELL, "chatPanelToggle")
        assert "window.innerWidth < CHAT_NARROW" in toggle
        assert "if (open &&" in toggle, "只在打开时互斥，关闭不影响另一侧"
        assert "other.hidden = true" in toggle
        assert 'which === "rail" ? $("#chat-side") : $("#chat-rail")' in toggle

    def test_center_can_shrink_without_overflow(self):
        """V1.160：min-width:0 是 flex 子项不撑破视口的根因修复。"""
        assert ".chat-shell > .chat" in STYLE_CHAT_CSS
        assert "min-width: 0" in STYLE_CHAT_CSS

    def test_paint_clears_data_area_not_toggle_buttons(self):
        """V1.170：paint 前清空数据区容器，不连收起按钮一起清。"""
        shell = JS_CHAT_SHELL
        mount = _body(shell, "chatShellMount")
        assert "chat-rail-body" in mount
        assert "chat-side-body" in mount
        assert "left.appendChild(railBody)" in mount
        assert "right.appendChild(sideBody)" in mount
        for fn, box_id in (("chatRailPaint", "#chat-rail-body"),
                           ("chatSidePaint", "#chat-side-body")):
            body = _body(shell, fn)
            assert f'$("{box_id}")' in body
            clear = body.index('box.textContent = "";')
            assert clear < body.index("appendChild")


class TestChatProjects:
    """V1.165：对话页豆包式项目面板——选项目 + 创建项目，共用同一写入口。"""

    def test_module_is_assembled(self):
        # 拼进页面的是 JS 源码内容，不是 Python 变量名。
        assert "function chatProjMount(" in INDEX_HTML, "模块须拼进页面才生效"

    def test_panel_has_two_entries_and_search(self):
        # V1.167 简化：只剩「不使用项目」+「选择本地文件夹」两条入口，
        # 删掉「＋创建新项目」表单路径（最终都走 project_save）。
        assert '"📁 选择本地文件夹"' in JS_CHAT_PROJECTS
        assert '"不使用项目"' in JS_CHAT_PROJECTS
        assert '"＋ 创建新项目"' not in JS_CHAT_PROJECTS, "表单路径已删"
        assert 'id = "chat-proj-filter"' in JS_CHAT_PROJECTS, "有搜索框"

    def test_reuses_same_endpoints_not_a_second_write_path(self):
        assert '"/api/agent/project_select"' in JS_CHAT_PROJECTS
        assert '"/api/agent/project_save"' in JS_CHAT_PROJECTS
        assert '"/api/agent/dirs?path="' in JS_CHAT_PROJECTS, "目录浏览复用既有端点"

    def test_close_listeners_read_live_dom(self):
        """V1.160 教训：重绘后节点不在文档里，回调不能捕获旧节点。"""
        bind = _body(JS_CHAT_PROJECTS, "chatProjBindClose")
        assert 'const p = $("#chat-proj-panel")' in bind
        assert "chatProjBound" in bind, "监听只绑一次"

    def test_dynamic_text_is_escaped(self):
        assert "esc(it.name" in JS_CHAT_PROJECTS
        assert "esc(it.directory" in JS_CHAT_PROJECTS
        assert "esc(data.path" in JS_CHAT_PROJECTS
        assert "esc(name)" in JS_CHAT_PROJECTS

    def test_dir_browser_has_quick_places_and_drives(self):
        """V1.168：跨盘纯点击——后端早返回 places/drives，轻量面板也须画。"""
        fn = _body(JS_CHAT_PROJECTS, "chatProjDirsLoad")
        assert "data.places" in fn, "画快捷位置"
        assert "data.drives" in fn, "画盘符（跨盘痛点的根因）"
        assert fn.count("chatProjDirsLoad(pl.path)") == 1, "快捷位置点下去仍走同一校验"
        assert fn.count("chatProjDirsLoad(d)") == 1, "盘符点下去仍走同一校验"
        assert "esc(pl.label" in fn and "esc(d)" in fn, "动态文本仍转义"

    def test_panel_inner_clicks_do_not_bubble_to_close_listener(self):
        """点目录 chip/行会重绘 area 把 e.target 移出 DOM，冒泡到 document 的
        关闭监听里 wrap.contains(e.target) 就成 false → 面板被误关（真实鼠标点击才暴露）。
        故面板内点击统一拦在 panel 层，不冒泡到 document。"""
        mount = _body(JS_CHAT_PROJECTS, "chatProjMount")
        assert 'panel.addEventListener("click"' in mount, "面板内点击拦在 panel 层"
        assert "ev.stopPropagation()" in mount, "不冒泡到 document 关闭监听"


class TestChatIntake:
    """V1.171：对话页「派活」按钮 —— 显式入口，确认闸门不绕过。"""

    def test_module_is_assembled(self):
        assert "function chatIntakeMount(" in INDEX_HTML

    def test_dispatch_button_is_wired_in_toolbar(self):
        shell = JS_CHAT_SHELL
        assert "chat-ctrl-dispatch" in shell
        assert "派活" in shell
        assert "chatIntakeMount(dispatch)" in shell

    def test_reuses_existing_endpoints_not_a_second_write_path(self):
        src = JS_CHAT_INTAKE
        assert 'post("/api/agent/intake"' in src
        assert 'api("/api/agent/projects")' in src
        assert 'post("/api/agent/prefs_save"' in src

    def test_progress_query_no_longer_blocked_and_confirm_gate_untouched(self):
        src = JS_CHAT_INTAKE
        # V1.173：点派活 = 明确派遣意图，含进度词也照派——不再拦。
        assert "CHAT_INTAKE_PROGRESS" not in src
        assert "这是查进度" not in src
        assert "V1.173" in src
        # 判非委派也照派：chatDelegate 出现 2 次（apply 里 + else 里）。
        assert src.count("await chatDelegate();") == 2
        # 确认闸门仍交 chatDelegate —— 本模块不复制 确认弹窗 / 不直接 post start。
        assert "window.confirm" not in src
        assert 'post("/api/agent/start"' not in src

    def test_carry_survives_redraw(self):
        # 追问上下文留模块级（V1.142 教训：跟着 DOM 走会点一下就没了）。
        assert "let chatIntakeCarry = null;" in JS_CHAT_INTAKE
        assert "chatIntakeCarry = res.carry" in JS_CHAT_INTAKE

    def test_dispatch_sits_last_before_arrow(self):
        """V1.172 人性布局：派活是动作、紧挨发送箭头，排在四个配置下拉之后。"""
        fn = _body(JS_CHAT_SHELL, "chatBarControls")
        mode = fn.index('const mode = mk("chat-ctrl-mode"')
        dispatch = fn.index("const dispatch = el(")
        insert = fn.index("tools.insertBefore(dispatch, arrow)")
        assert mode < dispatch < insert, "派活须在授权档之后创建、并插在发送箭头前"


class TestChatStaysOnDispatch:
    """V1.175：派活后留对话页，右栏接住进度 / 答复 / 改动，不跳工作台。"""

    def test_delegate_no_longer_jumps_to_workbench(self):
        fn = _body(JS_CHAT_SEARCH, "chatDelegate")
        assert 'load("workbench")' not in fn
        assert 'chatPanelToggle("side", true)' in fn
        assert "chatSidePaint()" in fn

    def test_side_panel_shows_answer_commands_and_polls(self):
        fn = _body(JS_CHAT_SHELL, "chatSidePaint")
        assert "turn.text" in fn and "chat-side-answer" in fn
        assert "turn.commands" in fn and "chat-side-lbl" in fn
        assert 'turn.status === "running"' in fn
        assert "chatSideArm()" in fn
        arm = _body(JS_CHAT_SHELL, "chatSideArm")
        assert "setInterval" in arm and 'indexOf(st.turn.status) < 0' in arm
        assert "clearInterval(chatSideTimer)" in arm

    def test_ask_card_is_a_signpost_not_a_fake_button(self):
        fn = _body(JS_CHAT_SHELL, "chatSidePaint")
        assert "turn.ask" in fn and "chat-side-ask" in fn
        assert "委派总览" in fn
        # 授权闸门没搬：右栏不直接 post decide / 不放允许拒绝按钮。
        assert "/api/agent/decide" not in fn


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

    def test_ghost_css_is_global(self):
        assert ".btn.ghost {" in STYLE_NAV_CSS, "ghost 不能只认 nav，要跨区域一致"

    def test_ghost_button_is_solid_not_dashed(self):
        """V1.162：虚线像没填完的占位符；次级按钮改实线 + 字色提一档。"""
        ghost = _css_rule(STYLE_NAV_CSS, ".btn.ghost {")
        assert "dashed" not in ghost, "虚线像占位符，已改实线"
        assert "color: var(--ink-2)" in ghost, "字色从 ink-3 提到 ink-2"
        assert "border-style" not in ghost, "button 默认即实线，不必再声明 border-style"


class TestContrastCSS:
    """V1.162：正文里真正要读的弱文本从 ink-3 提一档到 ink-2。"""

    def test_contrast_module_is_assembled(self):
        assert STYLE_CONTRAST_CSS in INDEX_HTML

    def test_readable_text_lifted_without_touching_global_token(self):
        assert "color: var(--ink-2)" in STYLE_CONTRAST_CSS
        for sel in (".meta", ".empty", ".disclaimer", ".acts .lbl",
                    ".section-label", "dt"):
            assert sel in STYLE_CONTRAST_CSS, f"{sel} 是正文要读的文本，应提一档"
        # 不全局改 --ink-3：那会抹平层级（导航分组 / 米黄警示仍用它）。
        assert "--ink-3:" not in STYLE_CONTRAST_CSS

    def test_contrast_comes_after_base_style(self):
        # 靠拼装顺序覆盖 style.py 里的 ink-3 规则（style.py 已满 250 行不能加）。
        from freeagent.web.style import STYLE_CSS
        assert INDEX_HTML.index(STYLE_CSS) < INDEX_HTML.index(STYLE_CONTRAST_CSS)

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
