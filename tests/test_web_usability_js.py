"""Web 易用性改动的静态守卫（js_boot / js_chat / js_promises / js_contacts）。

为什么是静态守卫：测试环境跑不起真 DOM / Playwright，而这几处是「形状与
上屏文案」问题 —— 判据用源码就能钉住。真机浏览器复验走
``tests/manual/ui_walkthrough.py``（隔离库起服务后跑）。

守的三件事：

1. 对话首屏也要刷头部状态，且规则层降级在首屏就有提示和去设置的入口；
2. 页脚跨视图切换时必须清空，上一视图的终端命令不许漏到下一页；
3. Web 空态/页脚不再夹 CLI 命令语法（角色改名这类确实只能终端做的，
   保留在角色页自己的页脚里，不算泄漏）。
"""
from __future__ import annotations

from freeagent.web.js_boot import JS_BOOT
from freeagent.web.js_chat import JS_CHAT
from freeagent.web.js_workbench_intake import JS_WORKBENCH_INTAKE
from freeagent.web.js_contacts import JS_CONTACTS
from freeagent.web.js_detail import JS_DETAIL
from freeagent.web.js_promises import JS_PROMISES
from freeagent.web.js_workbench_dirs import JS_WORKBENCH_DIRS
from freeagent.web.js_workbench_card import JS_WORKBENCH_CARD
from freeagent.web.js_workbench_projects import JS_WORKBENCH_PROJECTS
from freeagent.web.js_workbench_rail import JS_WORKBENCH_RAIL
from freeagent.web.js_workbench_page import JS_WORKBENCH_PAGE
from freeagent.web.js_workbench_intake import JS_WORKBENCH_INTAKE
from freeagent.web.style_workbench import STYLE_WORKBENCH_CSS
from freeagent.web.page import INDEX_HTML


def _body(source: str, fn_name: str) -> str:
    """按大括号配平取某个函数体（与 test_chat_js 同一套够用逻辑）。"""
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
    raise AssertionError(f"{fn_name} 的大括号没配平")


class TestChatHomeShowsState:
    def test_chat_branch_refreshes_header(self):
        """对话分支原先提前 return，头部状态栏只有别的视图刷得到。"""
        assert "await refreshHeader();" in JS_BOOT, (
            "对话页切进来也要刷头部：规则层降级不能只有设置页看得到"
        )

    def test_rule_based_banner_renders_on_chat_home(self):
        body = _body(JS_CHAT, "loadChat")
        # 判据用实现名，不用 key_configured：Ollama 这类本机服务不需要 Key，
        # 凭据判据为真，不该被误报成未配置。
        assert 'h.llm === "RuleBasedProvider"' in body, (
            "首屏横幅只在真的退回规则层时出现"
        )
        assert 'load("settings")' in body, "横幅要给直达设置页的入口"
        assert "去配置模型" in body, "入口按钮文案要面向 Web 用户"

    def test_banner_fetches_health_without_blocking_chat(self):
        body = _body(JS_CHAT, "loadChat")
        assert 'api("/api/health")' in body
        # 状态读不到不能挡住对话：探针必须包在自己的 try/catch 里
        assert body.count("catch") >= 2


class TestHeaderQuietWhenAllGreen:
    """头部只报降级，不回显那些配一次就不再变的静态状态。"""

    def _header(self) -> str:
        return _body(JS_BOOT, "refreshHeader")

    def test_gate_uses_implementation_name(self):
        # 判据必须与对话横幅同口径：Ollama 不需要 Key，凭据判据会误报。
        assert 'h.llm === "RuleBasedProvider"' in self._header()

    def test_all_green_leaves_header_empty(self):
        # 只查代码，不查注释 —— 注释里解释"为什么不显示这些"本就会提到它们。
        body = "\n".join(
            line.split("//")[0] for line in self._header().splitlines()
        )
        assert ': "";' in body, "全绿时头部要留空，不占首屏地产"
        # 这些是配一次就不再变的回显 + 内部实现类名（还是历史遗留错名），
        # 都不该再上屏。
        for noise in ("已配 Key", "精力档位已启用", "DeepSeekProvider", "智能层 "):
            assert noise not in body, f"头部不该再回显静态状态/实现细节：{noise}"

    def test_error_clears_stale_warning(self):
        body = self._header()
        # 读不到状态时若留着上一次的降级文案，等于把旧事实当当前事实挂着。
        assert '$("#hdr").textContent = "";' in body, (
            "状态读不到时头部要清空，不能把上一次的降级告警继续挂着"
        )


class TestFooterDoesNotLeakAcrossViews:
    def test_footer_cleared_on_view_switch(self):
        assert '$("#foot").textContent = "";' in JS_BOOT, (
            "切视图先清页脚：否则上一视图的说明（含终端命令）会漏到下一页"
        )

    def test_roles_terminal_note_stays_on_roles_page(self):
        """角色改名/合并/静置确实只能终端做 —— 这条说明留在角色页自己的
        页脚里是事实，不许被当成泄漏删掉；泄漏靠切视图清空来挡。"""
        assert "/role-rename" in JS_DETAIL


class TestViewSwitchRaceDoesNotPaintStaleView:
    """连续快速切视图时，上一次 load() 的异步回包晚到，会把页面画成旧视图。
    守卫：boot 层每次进入记自己的目标视图，await 回来发现已被取代就放弃重绘。"""

    def test_boot_marks_own_target_and_discards_stale_continuation(self):
        assert "const mine = next;" in JS_BOOT
        assert "const superseded = () => view !== mine;" in JS_BOOT
        # 至少在一个异步回包后作废
        assert "if (superseded()) return;" in JS_BOOT

    def test_chat_discards_continuation_after_navigating_away(self):
        body = _body(JS_CHAT, "loadChat")
        assert "view !== \"chat\"" in body, (
            "loadChat 自己还有 await；切走后回来，旧回包不许把对话壳画到新视图上"
        )


class TestWebCopyAvoidsCliSyntax:
    """Web 空态/页脚给的是 Web 可操作的出路，不甩 CLI 命令给普通用户。"""

    def test_today_empty_has_no_cli_pin_command(self):
        assert "/today-pin" not in JS_BOOT, (
            "今天空态应引导用上面的表单记事务，不再让人去终端敲 /today-pin"
        )

    def test_promises_empty_has_no_cli_syntax(self):
        assert "/promise 记" not in JS_PROMISES, (
            "承诺空态应引导在对话里记下，不再甩 /promise 命令语法"
        )

    def test_contacts_copy_has_no_cli_syntax(self):
        assert "关系=名字" not in JS_CONTACTS
        assert "/contact <名字>" not in JS_CONTACTS
        assert "/promise 记" not in JS_CONTACTS


class TestWorkbenchDirsPicker:
    """目录选择器（V1.127 重做）：行列表 + 路径手输 + 最近目录；面包屑 /
    快捷位置 / 盘符 / 当前层过滤保留。"""

    def test_dirs_module_is_assembled_into_page(self):
        assert "function wbBrowse" in INDEX_HTML
        assert "function dirSegments" in INDEX_HTML

    def test_quick_places_and_drives_rendered(self):
        assert "data.places" in JS_WORKBENCH_DIRS
        assert "data.drives" in JS_WORKBENCH_DIRS

    def test_breadcrumb_and_filter_present(self):
        assert "dirSegments(data.path)" in JS_WORKBENCH_DIRS
        assert 'id = "wb-dir-filter"' in JS_WORKBENCH_DIRS

    def test_row_list_and_path_input_present(self):
        assert 'id = "wb-dir-list"' in JS_WORKBENCH_DIRS
        assert 'id = "wb-dir-path"' in JS_WORKBENCH_DIRS
        assert "dir-row" in JS_WORKBENCH_DIRS

    def test_recent_dirs_rendered(self):
        assert "data.recent" in JS_WORKBENCH_DIRS


class TestWorkbenchProjectsPicker:
    """项目选择器（V1.124）：最近项目平铺 + 搜索 + 新建 + 选本地文件夹。"""

    def test_projects_module_is_assembled_into_page(self):
        for fn in ("wbInitProjects", "wbSelectProject", "wbBrowseImport",
                   "wbSubmitProject"):
            assert f"function {fn}" in INDEX_HTML, f"{fn} 没进拼装脚本"

    def test_trigger_menu_and_form_ids_present(self):
        for ident in ("wb-project-btn", "wb-project-menu", "wb-project-filter",
                      "wb-project-list", "wb-project-form", "wb-form-name",
                      "wb-form-note", "wb-form-dir"):
            assert f'id = "{ident}"' in INDEX_HTML, f"缺少 {ident}"

    def test_three_actions_are_present(self):
        # 对齐豆包：不使用项目 / 新建项目 / 选择本地文件夹，浏览退到二级。
        for label in ("不使用项目", "新建项目", "选择本地文件夹"):
            assert label in INDEX_HTML

    def test_list_rows_escape_name_and_directory(self):
        body = _body(JS_WORKBENCH_PROJECTS, "wbRenderProjectMenu")
        assert "esc(p.name)" in body
        assert "esc(p.directory)" in body

    def test_clear_selection_sends_empty_id(self):
        body = _body(JS_WORKBENCH_PROJECTS, "wbSelectProject")
        assert "project_select" in body


class TestWorkbenchVisualScope:
    """工作台视觉收口（V1.126）：中性现代风只挂 .wb-view，其余视图不受影响。"""

    def test_style_block_is_assembled_into_page(self):
        assert ".wb-view .btn.primary" in INDEX_HTML
        assert ".wb-view .note" in INDEX_HTML

    def test_page_wraps_body_in_scope(self):
        assert 'el("div", "wb-view")' in JS_WORKBENCH_PAGE
        assert "wb-label" in JS_WORKBENCH_PAGE
        assert "wb-field-row" in JS_WORKBENCH_PAGE
        assert "wb-check-row" in JS_WORKBENCH_PAGE

    def test_every_rule_is_scoped_to_workbench(self):
        # 兜底：裸 .note / .btn.primary 覆盖会染到对话、事务列表等其余视图，
        # 工作台样式里每一条选择器都必须带 .wb-view 前缀。
        for line in STYLE_WORKBENCH_CSS.splitlines():
            stripped = line.strip()
            if stripped.startswith((".note", ".btn.primary", ".card")):
                raise AssertionError(f"工作台样式漏了作用域前缀：{stripped}")


class TestWorkbenchStepsList:
    """步骤清单（V1.129）：紧凑行替代每条一张 .note；当前步高亮、长清单折叠。"""

    def test_steps_card_is_assembled_into_page(self):
        assert "function wbStepsCard" in INDEX_HTML

    def test_step_row_classes_present(self):
        for cls in ("wb-step", "wb-step-idx", "wb-step-name",
                    "wb-step-target", "wb-step-status", "is-current",
                    "is-hidden"):
            assert cls in JS_WORKBENCH_CARD, f"步骤行缺少 {cls}"

    def test_collapse_threshold_and_toggle(self):
        assert "WB_STEP_COLLAPSE_AT" in JS_WORKBENCH_CARD
        assert 'classList.toggle("is-open")' in JS_WORKBENCH_CARD

    def test_step_styles_scoped_to_workbench(self):
        assert ".wb-view .wb-step" in INDEX_HTML
        for line in STYLE_WORKBENCH_CSS.splitlines():
            stripped = line.strip()
            if stripped.startswith(".wb-step"):
                assert ".wb-view" in stripped, f"步骤样式漏了作用域前缀：{stripped}"


class TestSessionScopeButton:
    """「本会话内允许此类」按钮（V1.138）：前端必须带 scope 到端点。

    静态守卫盯着拼进页面的 JS 串 —— 按钮文案、scope 传参、端点 body 三处
    少一处，「记住同类」就断在半路（前端点了，服务层却不知道是整会话）。
    """

    def test_session_button_is_rendered(self):
        assert "本会话内允许此类" in JS_WORKBENCH_CARD

    def test_session_button_passes_scope(self):
        assert 'wbDecide(turn.id, true, "", "session")' in JS_WORKBENCH_CARD

    def test_decide_posts_scope_to_endpoint(self):
        assert "async function wbDecide(id, allow, answer, scope)" in JS_WORKBENCH_CARD
        assert "scope: scope || \"\"" in JS_WORKBENCH_CARD


class TestWorkbenchRail:
    """工作区常驻左栏（V1.139）：列表钉在最左，选完不再收起来。

    守四件事：模块真进了页面；三栏顺序是 工作区 / 正文 / 产物；行文本过
    ``esc``；「跑过几轮」读不到（null）与还没跑过（0）分成两句话说。
    """

    def test_rail_is_assembled_into_page(self):
        assert "function wbRailMount" in INDEX_HTML
        assert "wbRailMount()" in INDEX_HTML

    def test_rail_is_leftmost_column(self):
        body = _body(JS_WORKBENCH_PAGE, "loadWorkbench")
        rail = body.index("cols.appendChild(wbRailMount())")
        main = body.index("cols.appendChild(main)")
        side = body.index("cols.appendChild(side)")
        assert rail < main < side, "三栏顺序必须是 工作区 / 正文 / 产物"

    def test_rail_ids_present(self):
        for el_id in ("wb-rail-list", "wb-rail-filter", "wb-harness"):
            assert el_id in JS_WORKBENCH_RAIL, f"左栏缺少 {el_id}"

    def test_row_text_is_escaped(self):
        body = _body(JS_WORKBENCH_RAIL, "wbRailPaint")
        assert "esc(p.name" in body, "工作区名要先 esc 再进 innerHTML"
        meta = _body(JS_WORKBENCH_RAIL, "wbRailMeta")
        assert "esc(bits.join" in meta, "轮次 + 目录那一行同样要 esc"

    def test_unreadable_turns_is_not_zero(self):
        body = _body(JS_WORKBENCH_RAIL, "wbTurnsText")
        assert '"轮次读不到"' in body, "读不到不能说成「还没跑过」"
        assert "n == null" in body

    def test_projects_paint_the_rail(self):
        # 下拉与左栏共用同一份清单：选中态与轮次数只在一处画。
        assert JS_WORKBENCH_PROJECTS.count("wbRailPaint()") >= 2

    def test_note_edit_goes_through_project_update(self):
        body = _body(JS_WORKBENCH_RAIL, "wbRailSaveNote")
        assert '"/api/agent/project_update"' in body

    def test_rail_styles_scoped_to_workbench(self):
        assert ".wb-view .wb-col-rail" in INDEX_HTML
        # 按「这一行提到左栏」筛，而不是按前缀筛：真实规则都以 `.wb-view` 开头，
        # 按 `.wb-rail` 前缀筛一条也命中不了，守卫等于没有。
        for line in STYLE_WORKBENCH_CSS.splitlines():
            if "wb-rail" in line or "wb-harness" in line:
                assert ".wb-view" in line, f"左栏样式漏了作用域前缀：{line.strip()}"


class TestWorkbenchIntake:
    """一句话起手（V1.141）：模块进页面 + 卡片挂上 + 预填既有表单 + 追问不替人做主。"""

    def test_intake_is_assembled_into_page(self):
        assert "function wbIntakeMount" in INDEX_HTML
        assert "wbIntakeMount()" in INDEX_HTML
        assert "function wbIntake" in INDEX_HTML

    def test_mount_is_not_in_workbench_page(self):
        # V1.157：第二个输入框已收，派活只在对话页（V1.152 的「…→交给它执行」）。
        body = _body(JS_WORKBENCH_PAGE, "loadWorkbench")
        assert "wbIntakeMount()" not in body

    def test_calls_the_intake_endpoint(self):
        body = _body(JS_WORKBENCH_INTAKE, "wbIntake")
        assert '"/api/agent/intake"' in body

    def test_prerequisites_are_shown_before_the_user_types(self):
        """V1.147：卡片先探测前提，没授权项目 / 没连模型时输入框保持禁用。"""
        mount = _body(JS_WORKBENCH_INTAKE, "wbIntakeMount")
        assert "wbIntakeProbe()" in mount
        assert 'input.disabled = true' in mount and 'go.disabled = true' in mount
        probe = _body(JS_WORKBENCH_INTAKE, "wbIntakeProbe")
        assert '"/api/agent/intake/ready"' in probe
        assert "wbIntakeReady = res" in probe, "结果存模块级，重绘后不用重探"
        paint = _body(JS_WORKBENCH_INTAKE, "wbIntakeApply")
        assert "input.disabled = false" in paint and "go.disabled = false" in paint
        assert 'res.reason === "no_model" ? "settings" : "feishu"' in paint

    def test_recognized_prefills_the_existing_form(self):
        body = _body(JS_WORKBENCH_INTAKE, "wbIntake")
        for sel in ("#wb-dir", "#wb-text", "#wb-project-btn"):
            assert sel in body, f"认出来后要预填 {sel}"

    def test_followup_answer_is_sent_back(self):
        body = _body(JS_WORKBENCH_INTAKE, "wbIntake")
        assert "wbIntakeCarry = res.carry" in body
        assert "wbIntake((ans.value" in body, "下一句原样送回，不另起逻辑"

    def test_dynamic_text_is_not_inner_html_from_data(self):
        # 动态的项目名 / 简报 / 失败原因走 textContent，绝不拼进 innerHTML。
        body = _body(JS_WORKBENCH_INTAKE, "wbIntake")
        assert "result.textContent = res.question" in body
        assert "result.textContent = \"没认出来" in body

