"""导航收敛（V1.119，方案 B）的静态守卫。

一级入口只留日常高频视图（对话 / 工作台 / 今天 / 事务），其余低频视图收进
「更多」下拉。守的是形状：哪些视图在一级、哪些在下拉、聚合角标挂在哪、
刷新是动作不是视图。真机开合走 ``tests/manual/ui_walkthrough.py``。
"""
from __future__ import annotations

import re

from freeagent.web.js_boot import JS_BOOT
from freeagent.web.js_nav import JS_NAV
from freeagent.web.markup import PAGE_MARKUP
from freeagent.web.page import INDEX_HTML
from freeagent.web.style_nav import STYLE_NAV_CSS

# 一级日常入口；其余视图全部进「更多」下拉。
PRIMARY_VIEWS = {"chat", "workbench", "today", "all"}
MENU_VIEWS = {
    "closed", "roles", "model", "contacts", "promises", "pending",
    "nudge", "oc", "services", "feishu", "settings",
}
# 下拉内的三层分组（V1.121）：标签只做视线分组，不是可点视图。
MENU_GROUPS = {
    "我的事": {"closed", "roles", "model", "contacts", "promises", "pending", "nudge"},
    "执行与通道": {"oc", "services", "feishu"},
    "系统": {"settings"},
}


def _views_in(fragment: str) -> set[str]:
    return set(re.findall(r'data-view="([a-z]+)"', fragment))


def _pop_fragment() -> str:
    """下拉浮层内部那一段（more-pop 开标签到它的闭合 </div>）。

    浮层里有分组 ``<div class="menu-group">``，嵌套 div 不能按第一个
    ``</div>`` 截断 —— 否则第一组之后的按钮会被切掉。这里按 div 配平取整段。
    """
    after = PAGE_MARKUP.split('id="more-pop"', 1)[1]
    depth = 1
    pos = 0
    while depth:
        open_i = after.find("<div", pos)
        close_i = after.find("</div>", pos)
        if close_i == -1:
            break
        if open_i != -1 and open_i < close_i:
            depth += 1
            pos = open_i + 4
        else:
            depth -= 1
            pos = close_i + 6
    return after[: pos - 6]


class TestPrimaryEntries:
    def test_primary_views_stay_at_top_level(self):
        for view in PRIMARY_VIEWS:
            assert f'data-view="{view}"' in PAGE_MARKUP, f"一级入口缺 {view}"

    def test_primary_views_are_not_inside_the_menu(self):
        leaked = PRIMARY_VIEWS & _views_in(_pop_fragment())
        assert not leaked, f"一级视图被收进了下拉：{leaked}"

    def test_more_menu_button_exists(self):
        assert 'id="more-btn"' in PAGE_MARKUP
        assert 'id="more-pop"' in PAGE_MARKUP


class TestMenuEntries:
    def test_all_low_frequency_views_are_in_menu(self):
        in_menu = _views_in(_pop_fragment())
        missing = MENU_VIEWS - in_menu
        assert not missing, f"下拉里缺视图：{missing}"

    def test_no_extra_view_in_menu(self):
        extra = _views_in(_pop_fragment()) - MENU_VIEWS
        assert not extra, f"下拉里出现了未登记的视图：{extra}"

    def test_js_nav_menu_set_matches_markup(self):
        arr = JS_NAV.split("new Set([", 1)[1].split("])", 1)[0]
        listed = set(re.findall(r'"([a-z]+)"', arr))
        assert listed == MENU_VIEWS, f"MENU_VIEWS 与标记不一致：{listed}"


class TestMenuGroups:
    def test_all_group_labels_present(self):
        for label in MENU_GROUPS:
            assert f'<div class="menu-group">{label}</div>' in PAGE_MARKUP

    def test_views_land_in_the_right_group(self):
        pop = _pop_fragment()
        # 按分组标题切开，标题之间的按钮就属于这一组。
        segments = re.split(r'<div class="menu-group">([^<]+)</div>', pop)
        by_label: dict[str, set[str]] = {}
        for i in range(1, len(segments) - 1, 2):
            label = segments[i]
            by_label[label] = _views_in(segments[i + 1])
        assert set(by_label) == set(MENU_GROUPS)
        for label, views in MENU_GROUPS.items():
            assert by_label.get(label) == views, f"分组「{label}」归属不符：{by_label.get(label)}"

    def test_groups_union_equals_menu_views(self):
        assert set().union(*MENU_GROUPS.values()) == MENU_VIEWS


class TestMenuBadge:
    def test_more_button_carries_aggregate_badge(self):
        assert 'id="b-more"' in PAGE_MARKUP
        # 承诺逾期 + 待处理 + 催办三计数聚合到更多按钮，否则藏在下拉里看不到
        assert 'setBadge("b-more"' in JS_BOOT
        assert "s.promises_overdue" in JS_BOOT
        assert "s.pending" in JS_BOOT
        assert "s.nudge" in JS_BOOT

    def test_per_item_badges_stay_in_menu(self):
        for badge in ("b-promises", "b-pending", "b-nudge"):
            assert f'id="{badge}"' in PAGE_MARKUP


class TestRefreshIsActionNotView:
    def test_refresh_button_has_no_view_binding(self):
        refresh_line = next(
            line for line in PAGE_MARKUP.splitlines() if 'id="refresh"' in line
        )
        assert "data-view" not in refresh_line, "刷新是动作按钮，不该被当成视图"


class TestHiddenAttributeRespected:
    def test_hidden_reset_rule_present(self):
        # .badge 的 inline-block 与 .menu-pop 的 flex 会压过 UA 默认的
        # [hidden]{display:none}；没有这条复位，零计数角标会露成空胶囊、
        # 下拉首屏也可能显示出来。
        assert "[hidden]" in STYLE_NAV_CSS
        assert "display: none !important" in STYLE_NAV_CSS


class TestNavModuleWiredIntoPage:
    def test_js_nav_block_is_assembled(self):
        assert "const MENU_VIEWS" in INDEX_HTML
        assert "function closeMore" in INDEX_HTML
