"""页面骨架与静态标记。

导航、页脚、静态提示都在这里；动态部分由脚本填。
"""

from __future__ import annotations

__all__ = ["PAGE_MARKUP"]

PAGE_MARKUP = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>个人事务助手</title>
</head>
<body>
<div class="wrap">
  <header>
    <h1>个人事务助手</h1>
    <span class="meta" id="hdr"></span>
  </header>
  <nav id="nav" hidden>
    <button data-view="chat" aria-selected="true">对话</button>
    <button data-view="today">今天</button>
    <button data-view="all">全部未结束</button>
    <button data-view="closed">已结束</button>
    <button data-view="roles">角色</button>
    <span class="spacer"></span>
    <button data-view="feishu">飞书通道</button>
    <button data-view="settings">设置</button>
    <button id="refresh" title="重新读取">刷新</button>
  </nav>
  <div id="root"></div>
  <p class="disclaimer" id="foot">启发式提示，不是评分 —— 助手不替你决定先做哪个。</p>
</div>
<div class="toast" id="toast"></div>
</body>
</html>"""
