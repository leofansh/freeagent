"""易用性专属样式（V1.132）：简洁模式 + 对话搜索。

为什么另立：style.py / style_workbench.py 都贴着 250 行上限（TestWebPackageStaysSmall），
这两块是跨视图的通用呈现规则，不属于任何单一视图，单独成块最清楚。

所有规则只用已定义的 CSS 变量；颜色不单通道传信息（12.7.2）。
"""

from __future__ import annotations

__all__ = ["STYLE_USABILITY_CSS"]

STYLE_USABILITY_CSS = r"""
  /* 简洁模式（V1.132）：只折叠工作台的过程信息，不动正文与答复。
     .wb-step 卡头「步骤（N）」刻意保留 —— 折叠是「过程还在，只是收起」，
     不是「这件事没发生」，头上那个计数就是证据。 */
  body.compact .wb-view .wb-steps,
  body.compact .wb-view .wb-step-more,
  body.compact .wb-view .wb-process { display: none; }

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
