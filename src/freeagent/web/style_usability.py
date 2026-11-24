"""对话内搜索的专属样式（V1.132；简洁模式已于 V1.159 删除）。

为什么另立：style.py / style_workbench.py 都贴着 250 行上限（TestWebPackageStaysSmall），
搜索条是跨视图的通用呈现规则，不属于任何单一视图，单独成块最清楚。

所有规则只用已定义的 CSS 变量；颜色不单通道传信息（12.7.2）。
"""

from __future__ import annotations

__all__ = ["STYLE_USABILITY_CSS"]

STYLE_USABILITY_CSS = r"""
  /* 对话内搜索条（V1.132） */
  .fa-search {
    display: flex; align-items: center; gap: 8px; padding: 8px 10px;
    margin-bottom: 8px; background: var(--card-2);
    border: 1px solid var(--line); border-radius: var(--radius);
  }
  .fa-search input {
    flex: 1 1 auto; min-width: 0; padding: 6px 8px;
    border: 1px solid var(--line); border-radius: 8px;
    background: var(--card); color: var(--ink); font: inherit;
  }
  .fa-search .fa-search-cnt { font-size: 12px; color: var(--ink-2); white-space: nowrap; }
  .fa-search button { flex: 0 0 auto; }
  mark.fa-hit { background: var(--warn-soft); color: var(--warn); border-radius: 3px; }
  mark.fa-hit-cur { background: var(--accent-soft); color: var(--accent); outline: 1px solid var(--accent); }
"""
