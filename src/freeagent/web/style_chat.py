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

  /* 输入框左侧的"+"附件入口（V1.149）：替代原先单摆的拍照胶囊。 */
  #visionbar { position: relative; display: flex; align-items: flex-end; }
  .btn.attach {
    width: 44px; height: 44px; padding: 0; font-size: 22px; line-height: 1;
    border-radius: 10px;
  }
  .attach-menu {
    position: absolute; bottom: calc(100% + 8px); left: 0; z-index: 30;
    min-width: 240px; padding: 6px;
    background: var(--card); border: 1px solid var(--line);
    border-radius: 12px; box-shadow: 0 8px 24px rgba(20, 24, 30, .14);
  }
  .attach-item {
    width: 100%; text-align: left; padding: 9px 10px;
    background: var(--card-2); color: var(--ink);
    border: 1px solid var(--line); border-radius: 8px;
    font: inherit; font-size: 13px; cursor: pointer;
  }
  .attach-item:hover:not(:disabled) {
    border-color: var(--accent); background: var(--accent-soft);
  }
  .attach-item:disabled { opacity: .55; cursor: not-allowed; }
  .attach-hint { float: right; color: var(--ink-3); font-size: 12px; }
  .attach-note { padding: 7px 4px 2px; font-size: 12px; line-height: 1.5; color: var(--ink-3); }

  /* 对话工具"…"（V1.149）：搜索 / 清空 / 多选从输入栏挪到右上角。 */
  .chat { position: relative; }
  .chat-tools { position: absolute; top: 0; right: 0; z-index: 25; }
  .chat-tool-menu { left: auto; right: 0; min-width: 220px; }
  .chat-tool-menu button { white-space: nowrap; }
  /* 右端发送箭头（V1.150）：左 + 右 ↑，与 OpenCode 的输入栏同款骨架。 */
  .chatbar .send-arrow {
    width: 38px; height: 38px; padding: 0; border-radius: 10px;
    font-size: 18px; line-height: 1;
  }
  /* V1.153：输入框与 + / ↑ 做成一体容器，工具在下方那一行（OpenCode 同款）。 */
  .chatbar {
    display: flex; flex-direction: column; gap: 6px; align-items: stretch;
    border: 1px solid var(--line); border-radius: 14px;
    background: var(--card); padding: 10px 12px;
  }
  .chatbar:focus-within { border-color: var(--accent); }
  .chatbar textarea {
    border: none; background: transparent; padding: 0 2px;
    min-height: 40px; max-height: 140px;
  }
  .chatbar textarea:focus { outline: none; }
  .chatbar-tools { display: flex; align-items: center; gap: 8px; }
  .chatbar-tools #visionbar { margin-right: auto; }
  .chat-ctrl {
    max-width: 150px; padding: 6px 8px; font: inherit; font-size: 12px;
    color: var(--ink-2); background: var(--card-2);
    border: 1px solid var(--line); border-radius: 8px; cursor: pointer;
  }

  /* 三栏外壳（V1.154）：左工作区 / 中对话 / 右产物，两侧可收。 */
  .chat-shell { display: flex; gap: 12px; height: calc(100vh - 150px); min-height: 420px; }
  .chat-shell > .chat { flex: 1; height: 100%; min-height: 0; }
  .chat-rail, .chat-side {
    width: 210px; flex: none; overflow-y: auto; padding: 10px;
    background: var(--card); border: 1px solid var(--line); border-radius: 12px;
  }
  .chat-side { width: 260px; }
  .chat-rail-list { display: flex; flex-direction: column; gap: 4px; margin-top: 10px; }
  .chat-rail-item, .chat-side-file {
    text-align: left; padding: 7px 9px; font: inherit; font-size: 13px;
    background: var(--card-2); color: var(--ink-2);
    border: 1px solid var(--line); border-radius: 8px; cursor: pointer;
    overflow-wrap: anywhere;
  }
  .chat-side-files { display: flex; flex-direction: column; gap: 4px; margin-top: 8px; }
  .chat-side-file { cursor: default; }
"""
