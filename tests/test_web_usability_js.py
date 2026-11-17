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
from freeagent.web.js_contacts import JS_CONTACTS
from freeagent.web.js_detail import JS_DETAIL
from freeagent.web.js_promises import JS_PROMISES
from freeagent.web.js_workbench_dirs import JS_WORKBENCH_DIRS
from freeagent.web.js_workbench_projects import JS_WORKBENCH_PROJECTS
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
            "对话页切进来也要刷头部：规则层/已配 Key 不能只有设置页看得到"
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
    """目录选择器三刀（V1.123）：面包屑跳转 / 快捷位置 + 盘符 / 当前层过滤。"""

    def test_dirs_module_is_assembled_into_page(self):
        assert "function wbBrowse" in INDEX_HTML
        assert "function dirSegments" in INDEX_HTML

    def test_quick_places_and_drives_rendered(self):
        assert "data.places" in JS_WORKBENCH_DIRS
        assert "data.drives" in JS_WORKBENCH_DIRS

    def test_breadcrumb_and_filter_present(self):
        assert "dirSegments(data.path)" in JS_WORKBENCH_DIRS
        assert 'id = "wb-dir-filter"' in JS_WORKBENCH_DIRS
        # 当前目录作为禁选项，打开下拉时不至于分不清「现在在哪」
        assert "here.disabled = true" in JS_WORKBENCH_DIRS


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

