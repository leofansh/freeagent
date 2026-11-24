"""全站宽度框架（V1.161）：与对话页一致撑满，正文限宽保可读。"""
from __future__ import annotations

__all__ = ["STYLE_LAYOUT_CSS"]

STYLE_LAYOUT_CSS = r"""
/* 全站宽度框架（V1.161）：与对话页一致撑满。
   .wrap 原 940px 是阅读宽度，框架统一到全宽后长段落一行难读，
   故给正文文本块限宽 —— 一致的是框架，不是把句子摊成 1900px。 */
.wrap { max-width: none; padding: 16px 24px 24px; }
.wrap .note, .wrap .empty, .wrap .disclaimer, .wrap .card p { max-width: 72ch; }
"""
