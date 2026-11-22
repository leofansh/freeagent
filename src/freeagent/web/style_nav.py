"""导航专属样式（V1.119）。

``style.py`` 已顶到 250 行上限；「更多」下拉与刷新动作按钮这几条规则随
``js_nav.py`` 一起拆到本块，拼装时接在 ``STYLE_CSS`` 之后。
"""

from __future__ import annotations

__all__ = ["STYLE_NAV_CSS"]

STYLE_NAV_CSS = r"""  /* hidden 属性复位：作者样式的 display（inline-block/flex）会压过 UA
     默认的 [hidden]{display:none}，零计数角标会渲染成空胶囊、下拉首屏
     也可能露出来。这里用 !important 把 hidden 重新抬回最高优先级。 */
  [hidden] { display: none !important; }
  /* 「更多」下拉：容器相对定位，浮层绝对定位压在内容之上。 */
  .menu { position: relative; }
  .menu-pop {
    position: absolute; top: calc(100% + 6px); left: 0; z-index: 30;
    /* 短窗口下别让底部条目被裁掉（提过三次才补）：超高就内部滚动。 */
    max-height: calc(100vh - 130px); overflow-y: auto;
    display: flex; flex-direction: column; gap: 4px; min-width: 160px; padding: 6px;
    background: var(--card); border: 1px solid var(--line); border-radius: 12px;
    box-shadow: 0 8px 24px rgba(20, 24, 30, .14);
  }
  .menu-pop button { text-align: left; width: 100%; }
  /* 分段标题：只做视线分组，不是可点视图；字号与字色压到比按钮弱一级。 */
  .menu-group { margin: 6px 4px 2px; color: var(--ink-3); font-size: 12px; }
  .menu-group:first-child { margin-top: 2px; }
  /* 次级按钮（刷新 / 搜索）：弱化边框与字色，不跟主按钮同级抢视线。
     统一成 .btn.ghost 而不是只认 nav button —— 聊天栏里的搜索与导航条上的
     刷新是同一类「工具」，样式必须跨区域一致（V1.148）。 */
  .btn.ghost { color: var(--ink-3); border-style: dashed; }
  .btn.ghost:hover { color: var(--accent); border-style: solid; }
  /* 开关态要比纯动作再清楚一档：详细/简洁是 toggle，不能和刷新长一样。 */
  .btn.ghost[aria-pressed="true"] {
    background: var(--accent-soft); border-style: solid;
    border-color: var(--accent); color: var(--accent);
  }
"""
