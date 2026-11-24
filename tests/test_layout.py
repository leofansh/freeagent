"""V1.161 全站宽度框架的静态守卫。

新样式是拼进单页 HTML 的 CSS 字符串，没有可单测的 Python 逻辑；真正观感靠
浏览器走查。守卫钉的是**接线**：模块有没有进页面、拼装顺序对不对、限宽规则
在不在 —— 这些改错了最不容易被发现，且会静默覆盖掉 style.py 的阅读宽度。
"""
from __future__ import annotations

from freeagent.web.page import INDEX_HTML
from freeagent.web.style import STYLE_CSS
from freeagent.web.style_layout import STYLE_LAYOUT_CSS


def test_layout_css_is_assembled():
    assert STYLE_LAYOUT_CSS in INDEX_HTML


def test_layout_fills_frame_but_limits_long_text():
    assert ".wrap { max-width: none;" in STYLE_LAYOUT_CSS
    # 正文限宽是「框架一致」与「长文可读」的折中，不能省。
    assert "max-width: 72ch" in STYLE_LAYOUT_CSS
    for cls in (".note", ".empty", ".disclaimer", ".card p"):
        assert cls in STYLE_LAYOUT_CSS


def test_layout_comes_after_base_style():
    # 靠拼装顺序覆盖 style.py:41 的 940px；放反了框架就撑不满。
    assert STYLE_CSS in INDEX_HTML
    assert INDEX_HTML.index(STYLE_CSS) < INDEX_HTML.index(STYLE_LAYOUT_CSS)
