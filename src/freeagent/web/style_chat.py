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
  /* min-width:0 是窄屏不横向溢出的根因修复：flex 子项默认 min-width:auto，
     会被欢迎区卡片和输入栏那排选择器撑住不收缩（V1.160）。 */
  .chat-shell > .chat { flex: 1; height: 100%; min-height: 0; min-width: 0; }
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
  /* 派活后留对话页（V1.175）：右栏补的标签 / 授权路标 / 答复正文。 */
  .chat-side-lbl { color: var(--ink-3); font-size: 12px; margin-top: 10px; }
  .chat-side-ask { color: var(--warn); font-size: 12.5px; margin-top: 8px; line-height: 1.5; }
  .chat-side-answer {
    font: inherit; font-size: 12.5px; color: var(--ink-2);
    background: var(--card-2); border: 1px solid var(--line); border-radius: 8px;
    padding: 8px 9px; margin-top: 10px; overflow-wrap: anywhere;
  }

  /* 对话页撑满（V1.159）：.wrap 的 940px 是给列表类视图的阅读宽度，
     三栏外壳要占整屏。:has 不支持时静默保持旧宽度（fail-open 到现状，
     不破坏功能），所以这里不加任何 JS 视图切换逻辑。 */
  .wrap:has(.chat-shell) { max-width: none; padding: 16px 24px 24px; }
  /* 面板开合按钮做成栏内标题，不跟 .btn.ghost 那样虚线像占位符。 */
  .chat-panel-close {
    padding: 4px 8px; font: inherit; font-size: 12px;
    color: var(--ink-2); background: var(--card-2);
    border: 1px solid var(--line); border-radius: 8px; cursor: pointer;
  }
  .chat-panel-close:hover { color: var(--accent); border-color: var(--accent); }

  /* 项目面板（V1.165）：按钮外裹相对定位容器，面板向上弹，不撑开输入栏那排。 */
  .chat-proj-wrap { position: relative; display: inline-flex; }
  .chat-proj-btn { text-align: left; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
/* 派活按钮（V1.171）：实心强调，和四个次级下拉拉开主次。 */
.chat-dispatch-btn { background: var(--accent); color: #fff; border-color: var(--accent); }
.chat-dispatch-btn:hover { filter: brightness(1.08); }
  .chat-proj-menu {
    position: absolute; bottom: calc(100% + 8px); left: 0; z-index: 32;
    width: 300px; max-height: 60vh; overflow-y: auto; padding: 10px;
    background: var(--card); border: 1px solid var(--line);
    border-radius: 12px; box-shadow: 0 8px 28px rgba(0, 0, 0, .14);
    display: flex; flex-direction: column; gap: 8px;
  }
  .chat-proj-menu input {
    /* 覆盖全局 input[type=text] { flex: 1 1 260px }：面板是 flex column，
       不覆盖的话搜索框沿主轴被撑到几百像素高（V1.166 实测）。 */
    flex: none;
    width: 100%; box-sizing: border-box; padding: 7px 9px; font: inherit; font-size: 12px;
    color: var(--ink); background: var(--card-2);
    border: 1px solid var(--line); border-radius: 8px;
  }
  .chat-proj-list { display: flex; flex-direction: column; gap: 4px; max-height: 220px; overflow-y: auto; }
  .chat-proj-item {
    text-align: left; padding: 7px 9px; font: inherit;
    background: var(--card-2); border: 1px solid var(--line);
    border-radius: 8px; cursor: pointer;
  }
  .chat-proj-item:hover { border-color: var(--accent); }
  .chat-proj-name { font-size: 13px; color: var(--ink); overflow-wrap: anywhere; }
  .chat-proj-dir { font-size: 11px; color: var(--ink-2); overflow-wrap: anywhere; }
  .chat-proj-acts { display: flex; flex-direction: column; gap: 4px; border-top: 1px solid var(--line); padding-top: 8px; }
  .chat-proj-act {
    text-align: left; padding: 7px 9px; font: inherit; font-size: 12px;
    color: var(--ink-2); background: transparent; border: 1px solid transparent;
    border-radius: 8px; cursor: pointer;
  }
  .chat-proj-act:hover { color: var(--accent); background: var(--card-2); border-color: var(--line); }
  .chat-proj-area { display: flex; flex-direction: column; gap: 6px; font-size: 12px; color: var(--ink-2); }
  .chat-proj-area[hidden] { display: none; }
  .chat-proj-dirline { padding: 5px 2px; overflow-wrap: anywhere; }
  .chat-proj-formrow { display: flex; gap: 6px; }
  .chat-proj-dircrumb { padding: 5px 2px; font-size: 11px; color: var(--ink-2); overflow-wrap: anywhere; }
  .chat-proj-dirlist { display: flex; flex-direction: column; gap: 2px; max-height: 200px; overflow-y: auto; }
  .chat-proj-dirrow {
    text-align: left; padding: 6px 8px; font: inherit; font-size: 12px;
    color: var(--ink-2); background: var(--card-2);
    border: 1px solid var(--line); border-radius: 6px; cursor: pointer;
    overflow-wrap: anywhere;
  }
  .chat-proj-dirrow:hover { border-color: var(--accent); }
"""
