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
<title>值守 · 个人事务助手</title>
</head>
<body>
<div class="wrap">
  <header>
    <h1>值守</h1>
    <span class="meta" id="hdr"></span>
  </header>
  <nav id="nav" hidden>
    <!-- 一级入口只放日常高频视图；其余低频视图收进「更多」下拉（V1.119）。
         视图本身一个不删，只是入口下沉 —— 下拉项仍带 data-view，接线同走 boot。 -->
    <button data-view="chat" aria-selected="true">对话</button>
    <button data-view="today">今天<span class="badge" id="b-today" hidden></span></button>
    <button data-view="all">事务</button>
    <div class="menu" id="more-menu">
      <button id="more-btn" aria-haspopup="true" aria-expanded="false">更多<span class="badge" id="b-more" hidden></span></button>
      <div class="menu-pop" id="more-pop" hidden>
        <!-- 三层分组（V1.121）：我的事 = 个人事务脉络；执行与通道 = 委派/后台/飞书；系统 = 设置。
             分组只是入口排布，视图与 data-view 接线一个不删。 -->
        <div class="menu-group">我的事</div>
        <!-- Harness（V1.143）：把「它为你累积了什么」（项目规矩 / 记忆 /
             事务与承诺 / 能力与设置）收在一处。放组首是因为它是这组的总览入口，
             而不是又一件具体的事。 -->
        <button data-view="harness">它记住了什么</button>
        <button data-view="closed">已结束</button>
        <button data-view="roles">角色</button>
        <button data-view="week">周回顾</button>
        <button data-view="model">画像</button>
        <button data-view="contacts">关系</button>
        <button data-view="promises">承诺<span class="badge" id="b-promises" hidden></span></button>
        <button data-view="pending">待处理<span class="badge" id="b-pending" hidden></span></button>
        <button data-view="nudge">催办<span class="badge" id="b-nudge" hidden></span></button>
        <div class="menu-group">执行与通道</div>
        <button data-view="workbench">工作台（进度与授权）</button>
        <button data-view="oc">委派总览</button>
        <button data-view="services">后台服务</button>
        <button data-view="feishu">飞书通道</button>
        <div class="menu-group">系统</div>
        <button data-view="settings">设置</button>
      </div>
    </div>
    <span class="spacer"></span>
    <!-- 刷新是动作按钮，不是视图：不挂 data-view，样式弱化（.btn.ghost）。 -->
    <button id="refresh" title="重新读取" class="btn ghost">刷新</button>
  </nav>
  <div id="root"></div>
  <p class="disclaimer" id="foot">启发式提示，不是评分 —— 助手不替你决定先做哪个。</p>
</div>
<div class="toast" id="toast"></div>
</body>
</html>"""
