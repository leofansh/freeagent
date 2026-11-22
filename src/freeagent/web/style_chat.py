"""对话首屏专属样式（V1.145）：居中欢迎区 + 规则层状态条。

为什么另立：``style.py`` 已贴 250 行上限，而这一层只服务对话首屏，
不该挤进跨视图的 ``style_usability.py``。所有规则只用已定义的 CSS 变量，
颜色不单通道传信息（12.7.2）—— 欢迎区靠位置与字号分层，不靠颜色。
"""

from __future__ import annotations

__all__ = ["STYLE_CHAT_CSS"]

STYLE_CHAT_CSS = r"""
  /* 空对话时把欢迎区放到可视区中央；有消息后 addMsg 摘掉 .is-empty，
     恢复从上往下排。justify-content 只作用主轴（纵向）。 */
  .chatlog.is-empty { justify-content: center; align-items: center; }

  .chat-welcome {
    width: 100%; max-width: 620px; margin: 0 auto; padding: 8px 4px 4px;
    text-align: center;
  }
  .cw-hero { font-size: 22px; font-weight: 600; color: var(--ink); }
  .cw-sub {
    margin-top: 8px; font-size: 13px; line-height: 1.6; color: var(--ink-2);
  }
  .cw-label {
    margin: 22px 0 10px; font-size: 12px; letter-spacing: .04em;
    color: var(--ink-3);
  }
  .cw-grid {
    display: grid; gap: 8px;
    grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  }
  .cw-card {
    text-align: left; padding: 11px 13px; cursor: pointer;
    background: var(--card); color: var(--ink);
    border: 1px solid var(--line); border-radius: var(--radius);
    font: inherit; font-size: 13px; line-height: 1.45;
  }
  .cw-card:hover { border-color: var(--accent); background: var(--accent-soft); }

  /* 规则层（离线）状态条：中性冷灰而不是警示色。它是「说明」不是
     「出错」，套 .note 的米黄会让人以为哪里坏了（V1.126 同一条）。 */
  .chat-status {
    display: flex; align-items: center; flex-wrap: wrap; gap: 8px;
    padding: 8px 10px; margin-top: 18px;
    background: var(--card-2); color: var(--ink-2);
    border: 1px solid var(--line); border-radius: var(--radius);
    font-size: 13px; line-height: 1.5; text-align: left;
  }
"""
