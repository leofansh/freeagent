"""设置页分组标题（V1.132）。

为什么单独一块：js_settings.py 已 249 行，贴着 250 行上限
（TestWebPackageStaysSmall）。分组标题就一个 helper，放这里既解行数，
又让「分组」这件事有个名字。

不影响字段挂载位置 —— 标题和字段一样 append 进 form.set，吃同一套样式
（test_web.py::test_llm_section_is_mounted_inside_the_styled_form 钉住）。
"""

from __future__ import annotations

__all__ = ["JS_SETTINGS_GROUPS"]

JS_SETTINGS_GROUPS = r"""// 设置页分组标题（V1.132）：和字段一样挂进 form.set，吃同一套样式。
function settingsGroupLabel(text) {
  return el("div", "section-label", esc(text));
}
"""
