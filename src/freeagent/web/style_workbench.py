"""工作台专属样式（V1.126）。

中性现代风收口：提示 / 空态 / 状态条从米黄改成中性冷灰，米黄只留给真警示；
主按钮实心；卡片加轻阴影。规则全部挂在 ``.wb-view`` 容器下，不影响其余视图。

``style.py`` 已顶到 250 行上限（``TestWebPackageStaysSmall`` 守着），故另立
本块，拼装时接在 ``STYLE_NAV_CSS`` 之后（见 ``page.py``）。
"""

from __future__ import annotations

__all__ = ["STYLE_WORKBENCH_CSS"]

STYLE_WORKBENCH_CSS = r"""  /* 工作台视图容器：以下规则全部限定在它之内，别漏到对话 / 事务列表。 */
  .wb-view { display: flex; flex-direction: column; gap: 12px; }

  .wb-view .card {
    background: var(--card); border: 1px solid var(--line); border-radius: 14px;
    padding: 18px; margin-bottom: 0;
    box-shadow: 0 1px 2px rgba(16, 24, 40, .05);
  }

  /* 字段标签：比正文弱一级，但压在输入框上面，不并排挤着。 */
  .wb-view .wb-label {
    font-size: 13px; font-weight: 600; color: var(--ink-2);
    margin: 14px 0 6px;
  }
  .wb-view .wb-label:first-child { margin-top: 0; }

  .wb-view .wb-field-row {
    display: flex; gap: 8px; flex-wrap: wrap; align-items: center;
  }
  .wb-view .wb-field-row input[type=text] { flex: 1 1 220px; min-width: 0; width: auto; }

  .wb-view input[type=text], .wb-view input[type=search],
  .wb-view select, .wb-view textarea {
    width: 100%; box-sizing: border-box;
    background: var(--card); border: 1px solid var(--line); border-radius: 10px;
    padding: 9px 12px; font: inherit; color: var(--ink);
  }
  .wb-view textarea { min-height: 88px; resize: vertical; line-height: 1.6; }

  /* 提示 / 空态 / 状态条：中性冷灰，不再用警示米黄铺一屏。真警示仍由
     文案前缀（⚠）与 danger 按钮承担，不靠底色喊人。 */
  .wb-view .note {
    background: var(--info-soft); color: var(--ink-2);
    border: 1px solid var(--line); border-radius: 10px;
    padding: 9px 13px; font-size: 13px; margin: 8px 0;
  }
  .wb-view .wb-check-row { display: flex; align-items: center; gap: 8px; }

  /* 路由 / 引擎行只有拿到内容才画：初始加载时容器是空的，留着底色会显示成
     一条空灰条（旧版是一条空米黄条），像少了一块文字。 */
  .wb-view #wb-route:empty, .wb-view #wb-engine:empty { display: none; }

  /* 主动作实心：描边浅绿在白卡上没有主次，实心才看得出「点这个」。 */
  .wb-view .btn.primary {
    background: var(--accent); border-color: transparent; color: #fff;
    font-weight: 600;
  }
  .wb-view .btn.primary:hover {
    background: var(--accent); border-color: var(--accent); color: #fff;
  }

  .wb-view .row1 { gap: 9px; }

  /* 目录行列表：点行进入下一层，底部按钮才选定当前目录（V1.127 重做）。 */
  .wb-view .dir-list { display: flex; flex-direction: column; gap: 4px; margin: 8px 0; }
  .wb-view .dir-row {
    display: block; width: 100%; text-align: left; cursor: pointer;
    padding: 8px 12px; border: 1px solid transparent; border-radius: 10px;
    background: transparent; color: var(--ink); font: inherit;
  }
  .wb-view .dir-row:hover { background: var(--card-2); border-color: var(--line); }
  .wb-view .note:empty { display: none; }

  /* 步骤清单（V1.129）：紧凑行，不再每条铺一张 .note 卡片。颜色只辅助文字，
     状态仍读标签本身 —— 不靠颜色单通道传达（12.7.2）。 */
  .wb-view .wb-steps { display: flex; flex-direction: column; gap: 2px; margin-top: 8px; }
  .wb-view .wb-step {
    display: flex; align-items: baseline; gap: 8px; padding: 4px 6px;
    font-size: 13px; border-radius: 8px;
  }
  .wb-view .wb-step-idx {
    color: var(--ink-3); min-width: 1.8em; font-variant-numeric: tabular-nums;
  }
  .wb-view .wb-step-name { font-weight: 500; }
  .wb-view .wb-step-target {
    color: var(--ink-2); flex: 1 1 auto; min-width: 0; overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap;
  }
  .wb-view .wb-step-status {
    margin-left: auto; font-size: 12px; color: var(--ink-3); white-space: nowrap;
  }
  .wb-view .wb-step.step-done { color: var(--ink-3); }
  .wb-view .wb-step.step-done .wb-step-name { font-weight: 400; color: var(--ink-2); }
  .wb-view .wb-step.step-failed .wb-step-status { color: var(--warn); }
  .wb-view .wb-step.step-running .wb-step-status { color: var(--accent); }
  .wb-view .wb-step.is-current { background: var(--card-2); }
  .wb-view .wb-steps:not(.is-open) .wb-step.is-hidden { display: none; }
  .wb-view .wb-step-more { margin-top: 8px; }

  /* 产物侧栏（V1.132）：步骤与答复在左，文件 / 命令 / 答复在右，同屏对照。
     窄屏折回一列 —— 侧栏退到流下面，不把正文挤成一条缝。 */
  .wb-view .wb-cols { display: flex; gap: 12px; align-items: flex-start; }
  .wb-view .wb-col-main {
    flex: 1 1 auto; min-width: 0; display: flex; flex-direction: column; gap: 12px;
  }
  .wb-view .wb-col-side { flex: 0 0 280px; min-width: 0; }
  /* 工作区左栏（V1.139）：工作区列表常驻最左，选完不再收起来；右卡是「这个
     工作区累积了什么」。三栏 = 工作区 / 正文 / 产物，窄屏逐级折。 */
  .wb-view .wb-col-rail {
    flex: 0 0 220px; min-width: 0;
    display: flex; flex-direction: column; gap: 12px;
  }
  .wb-view .wb-rail-card, .wb-view .wb-harness { padding: 12px 12px; }
  /* 列表给个上限：工作区多到十几项时，左栏不再往下把整页拉长；常见几项
     远到不了这个高度，选中行不会被滚出去。 */
  .wb-view #wb-rail-list {
    display: flex; flex-direction: column; gap: 4px;
    margin-top: 8px; max-height: 46vh; overflow-y: auto;
  }
  .wb-view .wb-rail-row {
    display: block; width: 100%; text-align: left; cursor: pointer;
    padding: 8px 10px; border: 1px solid transparent; border-radius: 10px;
    background: transparent; color: var(--ink); font: inherit;
  }
  .wb-view .wb-rail-row:hover { background: var(--card-2); border-color: var(--line); }
  /* 选中只加左侧一道实心边：左侧边框不挤压文字，且不只靠颜色（is-current 也在）。 */
  .wb-view .wb-rail-row.is-current {
    background: var(--card-2); border-color: var(--line);
    box-shadow: inset 3px 0 0 var(--accent);
  }
  .wb-view .wb-rail-name {
    font-size: 13.5px; font-weight: 600;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .wb-view .wb-rail-meta {
    font-size: 12px; color: var(--ink-3); margin-top: 2px;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  /* 一条一行：键在上、值在下 —— 备注与规则都可能是长句，并排会把按钮顶下去。 */
  .wb-view .wb-harness-line {
    display: flex; flex-direction: column; gap: 1px;
    font-size: 12.5px; margin: 0 0 8px;
  }
  .wb-view .wb-harness-head {
    display: flex; align-items: center; justify-content: space-between; gap: 8px;
  }
  .wb-view .wb-harness-edit { padding: 2px 8px; font-size: 12px; }
  .wb-view .wb-harness-k { color: var(--ink-3); }
  .wb-view .wb-harness-v { color: var(--ink-2); word-break: break-all; }

  /* 这个工作区有什么（V1.142）：右栏下块，一行一项「名字 / 目录或大小 /
     定位或进去」。只给 #wb-files 内的行排版 —— 产物块的行里是长绝对路径，
     换行比省略号更该保留（末段才是文件名）。 */
  .wb-view #wb-files { margin-top: 12px; }
  .wb-view #wb-files .wb-file-row {
    display: flex; align-items: center; gap: 8px;
    justify-content: space-between; margin: 0 0 2px;
  }
  .wb-view #wb-files .wb-file-path {
    flex: 1 1 auto; min-width: 0; overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap;
  }
  .wb-view #wb-files .wb-file-count {
    flex: 0 0 auto; color: var(--ink-3); font-size: 12px;
  }

  /* 安静模式（V1.142）：把配置表单与两侧栏一起收掉，宽度还给正文。
     顶上那条窄条是「收掉之后仍在哪个工作区」—— 没有它就是失明，不是安静。
     显示与否只靠 is-zen，不叠 hidden 属性（两者会互相盖掉）。 */
  .wb-view .wb-zen-strip { display: none; }
  .wb-view.is-zen .wb-zen-strip {
    display: flex; align-items: center; justify-content: space-between;
    gap: 10px; padding: 6px 12px; margin-bottom: 8px;
    border: 1px solid var(--line); border-radius: 10px;
    background: var(--card-2); font-size: 12.5px; color: var(--ink-2);
  }
  .wb-view.is-zen #wb-bar,
  .wb-view.is-zen .wb-col-rail,
  .wb-view.is-zen .wb-col-side { display: none; }

  /* 后台运行那一句：小字、恒显，不随引擎状态变（它是事实，不是状态）。 */
  .wb-view .wb-run-note { font-size: 12.5px; color: var(--ink-3); margin: 0 0 2px; }

  @media (max-width: 1100px) {
    .wb-view .wb-col-rail { flex: 0 0 172px; }
  }
  @media (max-width: 860px) {
    .wb-view .wb-cols { flex-direction: column; }
    .wb-view .wb-col-rail { flex: 1 1 auto; width: 100%; }
    .wb-view .wb-col-side { flex: 1 1 auto; width: 100%; }
  }
"""
