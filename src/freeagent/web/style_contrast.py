"""辅助文本对比度（V1.162）：空态 / 说明 / 字段名从 ink-3 提一档到 ink-2。

不直接改 style.py 的 --ink-3 变量：那是全站最弱一级的字色（连导航分组标题
.menu-group 都用），全局提亮会抹平层级。这里只针对「正文里真正要读」的几处
说明文本提一档 —— 弱提示仍弱，但不再淡到融进白底。style.py 已满 250 行，
故另立模块，拼装接在 STYLE_CSS 之后靠顺序覆盖。
"""
from __future__ import annotations

__all__ = ["STYLE_CONTRAST_CSS"]

STYLE_CONTRAST_CSS = r"""
/* 辅助文本对比度（V1.162）：这几处是正文里要读的话，不是装饰性弱提示。
   从 --ink-3 提到 --ink-2：浅色 #8b95a3→#5a6472，深色 #7b8695→#a8b2bf。
   .note 米黄警示与 .menu-group 分组标题保持 ink-3 不动（各自另有层级语义）。 */
.meta, .acts .lbl, .disclaimer, .section-label, dt, .empty { color: var(--ink-2); }
"""
