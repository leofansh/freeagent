"""页面样式。

刻意零外部资源：无 CDN、无字体文件、无框架。
"""

from __future__ import annotations

__all__ = ["STYLE_CSS"]

STYLE_CSS = r"""  :root {
    --bg: #f6f7f9;
    --card: #ffffff;
    --ink: #1c2027;
    --ink-2: #5a6472;
    --ink-3: #8b95a3;
    --line: #e3e7ec;
    --accent: #2f6f4f;
    --accent-soft: #e8f2ec;
    --warn: #8a5a12;
    --warn-soft: #fdf3e2;
    --info-soft: #eef2f7;
    --radius: 10px;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #14171c; --card: #1c2027; --ink: #e8ecf1; --ink-2: #a8b2bf;
      --ink-3: #7b8695; --line: #2b313a; --accent: #6fbf95;
      --accent-soft: #1e2f27; --warn: #e0b070; --warn-soft: #2e2718;
      --info-soft: #222831;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--ink);
    font: 15px/1.7 -apple-system, "Segoe UI", "PingFang SC", "Hiragino Sans GB",
          "Microsoft YaHei", "Noto Sans CJK SC", sans-serif;
    -webkit-font-smoothing: antialiased;
  }
  .wrap { max-width: 940px; margin: 0 auto; padding: 24px 20px 72px; }
  header { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; margin-bottom: 4px; }
  h1 { font-size: 19px; margin: 0; font-weight: 650; letter-spacing: .01em; }
  .meta { color: var(--ink-3); font-size: 13px; }
  nav { display: flex; gap: 6px; margin: 18px 0 16px; flex-wrap: wrap; align-items: center; }
  nav .spacer { flex: 1; }
  nav button, .btn {
    font: inherit; font-size: 13px; padding: 6px 13px; border-radius: 999px;
    border: 1px solid var(--line); background: var(--card); color: var(--ink-2);
    cursor: pointer; transition: .12s;
  }
  nav button:hover, .btn:hover { border-color: var(--accent); color: var(--accent); }
  nav button[aria-selected="true"] {
    background: var(--accent-soft); border-color: var(--accent); color: var(--accent);
    font-weight: 600;
  }
  /* 角色选择：必须有明确选中态，否则不知道选了谁 */
  .btn[aria-pressed="true"] {
    background: var(--accent-soft); border-color: var(--accent);
    color: var(--accent); font-weight: 600;
  }
  .btn.danger:hover { border-color: #b4442e; color: #b4442e; }
  .btn.primary { background: var(--accent-soft); border-color: var(--accent); color: var(--accent); }
  .acts { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 12px;
          padding-top: 12px; border-top: 1px dashed var(--line); }
  .acts .lbl { font-size: 12px; color: var(--ink-3); align-self: center; margin-right: 2px; }
  .card {
    background: var(--card); border: 1px solid var(--line);
    border-radius: var(--radius); padding: 16px 18px; margin-bottom: 10px;
  }
  .card.clickable { cursor: pointer; }
  .card.clickable:hover { border-color: var(--accent); }
  .row1 { display: flex; align-items: baseline; gap: 9px; flex-wrap: wrap; }
  .idx { color: var(--ink-3); font-variant-numeric: tabular-nums; min-width: 1.4em; }
  .title { font-weight: 600; }
  .meta { font-size: 13px; }
  .roles { color: var(--accent); font-size: 13px; }
  .tag {
    font-size: 12px; padding: 1px 8px; border-radius: 999px;
    background: var(--info-soft); color: var(--ink-2); white-space: nowrap;
  }
  .tag.open { background: var(--accent-soft); color: var(--accent); }
  .tag.warn { background: var(--warn-soft); color: var(--warn); }
  .score {
    font-variant-numeric: tabular-nums; font-size: 12px; color: var(--ink-3);
    border: 1px solid var(--line); border-radius: 6px; padding: 0 6px;
  }
  .reasons { margin-top: 8px; display: flex; flex-wrap: wrap; gap: 6px; }
  .reason {
    font-size: 12.5px; background: var(--info-soft); color: var(--ink-2);
    border-radius: 6px; padding: 2px 8px;
  }
  .reason b { color: var(--warn); font-weight: 600; }
  .note {
    background: var(--warn-soft); color: var(--warn); border-radius: 8px;
    padding: 9px 13px; font-size: 13.5px; margin-bottom: 12px;
  }
  .disclaimer { color: var(--ink-3); font-size: 12.5px; margin: 14px 2px 0; }
  .section-label {
    font-size: 12px; color: var(--ink-3); text-transform: none;
    margin: 20px 0 8px; letter-spacing: .04em;
  }
  dl { margin: 0; display: grid; grid-template-columns: 88px 1fr; gap: 6px 14px; font-size: 14px; }
  dt { color: var(--ink-3); font-size: 13px; }
  dd { margin: 0; }
  pre.artifact {
    background: var(--bg); border: 1px solid var(--line); border-radius: 8px;
    padding: 12px 14px; overflow-x: auto; font: 13px/1.65 ui-monospace,
    "SF Mono", Consolas, "Microsoft YaHei", monospace; white-space: pre-wrap;
    margin: 8px 0 0;
  }
  ul.records { list-style: none; padding: 0; margin: 6px 0 0; font-size: 13px; color: var(--ink-2); }
  ul.records li { padding: 3px 0; border-bottom: 1px dashed var(--line); }
  ul.records li:last-child { border-bottom: 0; }
  ul.records time { color: var(--ink-3); margin-right: 8px; font-variant-numeric: tabular-nums; }
  ol.next { margin: 6px 0 0; padding-left: 20px; }
  ol.next li { margin: 3px 0; }
  form.new { margin-bottom: 18px; }
  form.new .grid { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  /* password 也要吃到基础样式：设置页的 API Key 框就是它。之前漏了，
     于是那个框既没有统一外观、也没有铺满宽度，占位符被截成
     「填了会写进 ~\.free…」—— 而那正是用户最需要看清的一句
     （它要说清 Key 会写到哪个文件）。
     占位符由 js_settings_llm.py 用 stateDir 运行时拼出，真值见 paths.py：
     默认 ~/.freeagent/llm.env，可由 FREEAGENT_HOME 改。 */
  input[type=text], input[type=password], select, input[type=number] {
    font: inherit; font-size: 14px; padding: 7px 11px; border-radius: 8px;
    border: 1px solid var(--line); background: var(--card); color: var(--ink);
  }
  input[type=text] { flex: 1 1 260px; min-width: 0; }
  input[type=number] { width: 82px; }
  /* 编程页签的四个下拉要**占满整行**：模型名有长有短（「Nemotron 3.5
     Lightning Free」29 字 vs「Big Pickle」10），窄的下拉会把长名截断成
     「Nemotron 3.5 Lig…」，而那正好是最难认的一批。 */
  .field > select { width: 100%; box-sizing: border-box; }
  /* 日常可选模型的开关列表：网格而不是竖排 —— 85 个竖排要滚很久，
     而「勾/不勾」是个一眼能扫完的动作。 */
  .currow {
    display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr));
    gap: 4px 12px; margin: 6px 0 12px;
  }
  .curopt { display: flex; align-items: baseline; gap: 6px; font-size: 13px; }
  .curopt .tag { flex: none; }
  input:focus, select:focus { outline: 2px solid var(--accent); outline-offset: -1px; }
  input:disabled, select:disabled { opacity: .5; }
  form.set { margin: 0; }
  form.set .field { margin: 12px 0; }
  form.set .field > label { display: block; font-size: 13px; color: var(--ink-2); margin-bottom: 5px; }
  form.set .field input[type=text], form.set .field input[type=password] {
    width: 100%; box-sizing: border-box;
  }
  form.set .hint { font-size: 12px; color: var(--ink-3); margin-top: 5px; }
  form.set .bands { display: flex; gap: 14px; flex-wrap: wrap; margin-top: 8px; }
  form.set .bands > div { display: flex; align-items: center; gap: 6px; font-size: 13px; }
  form.set .check { display: flex; align-items: center; gap: 7px; font-size: 13px; }
  form.set input[type=checkbox] { width: 15px; height: 15px; }
  form.set .actions { margin-top: 18px; display: flex; gap: 8px; align-items: center; }

  /* 对话：首屏就是输入框。气泡分我/它两侧，理由跟在气泡里不另开面板。 */
  .chat { display: flex; flex-direction: column; height: calc(100vh - 150px); min-height: 420px; }
  .chatlog { flex: 1; overflow-y: auto; display: flex; flex-direction: column; gap: 12px; padding: 4px 2px 12px; }
  .msg { max-width: 82%; }
  .msg.me { align-self: flex-end; background: var(--accent); color: #fff; border-color: transparent; }
  .msg.me .meta { color: rgba(255,255,255,.75); }
  .msg .bubble { white-space: pre-wrap; }
  .msg .items { margin-top: 9px; display: flex; flex-direction: column; gap: 6px; }
  .msg .items .tcard {
    border: 1px solid var(--line); border-radius: 8px; padding: 7px 10px;
    background: var(--card); cursor: pointer; font-size: 13px;
  }
  .msg .items .tcard:hover { border-color: var(--accent); }
  .msg .items .why { color: var(--ink-3); font-size: 12px; margin-top: 3px; }
  .msg.kind-answer .bubble { border-color: var(--accent); }
  .msg.kind-cannot .bubble, .msg.kind-help .bubble { background: var(--card-2); }
  .chatbar { display: flex; gap: 8px; margin-top: 10px; align-items: flex-end; }
  .chatbar textarea {
    flex: 1; resize: none; font: inherit; font-size: 14px; line-height: 1.55;
    padding: 10px 12px; border-radius: 10px; border: 1px solid var(--line);
    background: var(--card); color: var(--ink); min-height: 44px; max-height: 140px;
  }
  .chatbar textarea:focus { outline: 2px solid var(--accent); outline-offset: -1px; }
  .chips { display: flex; gap: 7px; flex-wrap: wrap; margin-bottom: 10px; }
  .chip {
    font: inherit; font-size: 13px; padding: 5px 11px; border-radius: 999px;
    border: 1px solid var(--line); background: var(--card); color: var(--ink-2);
    cursor: pointer;
  }
  .chip:hover { border-color: var(--accent); color: var(--ink); }
  .err { color: #b4442e; font-size: 13px; margin-top: 8px; }
  .empty { color: var(--ink-3); padding: 34px 0; text-align: center; }
  a.back { color: var(--accent); text-decoration: none; font-size: 13px; }
  .toast {
    position: fixed; left: 50%; bottom: 26px; transform: translateX(-50%);
    background: var(--ink); color: var(--bg); padding: 9px 18px; border-radius: 999px;
    font-size: 13px; opacity: 0; pointer-events: none; transition: .2s;
  }
  .toast.show { opacity: 1; }

  /* --- 飞书通道页（设计方案 12.7）--- */
  /* 日志用等宽 + 限高：日志行又长又多，不限高会把整页撑到没法看，
     而用户只想看「刚才那几行出了什么」。 */
  .logbox {
    margin-top: 8px; max-height: 380px; overflow-y: auto;
    background: var(--bg); border: 1px solid var(--line); border-radius: 8px;
    padding: 8px 10px;
  }
  .logline {
    font-family: ui-monospace, SFMono-Regular, Consolas, "Courier New", monospace;
    font-size: 12px; line-height: 1.6; color: var(--ink-2);
    white-space: pre-wrap; word-break: break-word;
  }
  /* 按严重度着色，但**不用颜色当唯一信号** —— 色弱用户看不到差别。
     日志文本里本身就有 ERROR/WARNING 字样，颜色只是辅助。 */
  .log-err { color: #b4442e; }
  .log-warn { color: #8a6d1f; }
  /* 状态细节网格：窄屏也能两列，不至于挤成一长条。 */
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; }
  .grid .field { margin: 0; }"""
