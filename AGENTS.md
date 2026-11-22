# 项目约定

> OpenCode 会自动加载它（`AGENTS.md`，向上查找，第一个匹配胜出）。
> 本文件是项目规矩的**唯一真源**——每次会话自动可见，不占委派简报字数。

## 这个项目的规矩

- 语言与运行时版本：Python 3.11+，**核心包零依赖**（只用标准库）。
  飞书通道是可选 extra（`pip install ".[feishu]"`），除此之外不加任何依赖。
- src 布局：源码在 `src/freeagent/`，运行要 `set PYTHONPATH=src`。
  测试怎么跑：`set PYTHONIOENCODING=utf-8` + `python -m pytest`（全量）。
  跑单个文件也行，但交付前必须全量绿。
- 不能碰的目录／文件：
  - `docs/个人事务助手设计方案.md` 是**实现契约**：枚举值/字段名/权重/阈值
    以它为准；`tests/test_doc_contract.py` 逐字对账（含 12.2 文件树），
    加新文件必须同步登记，改文档示例必须与实际输出一致。
  - `config.json` 绝不写 API Key（Key 只进状态目录的 `llm.env`，默认 `~/.steward/llm.env`）。
  - web 包单文件有 250 行上限（`TestWebPackageStaysSmall` 守着）。
- 提交信息格式：不用管——本仓由用户手动提交，agent 不主动 commit
  （用户明确要求时代劳，如 2026-10-21 的 V1.43–V1.50 一次提交）。
- 文档版本号纪律：设计文档头部「本次变更（V1.x）」记录最新变更，
  当前最新是 **V1.144**（Windows 桌面安装包第一层 —— 官方 embeddable Python 拼
  `dist/Steward`（runtime + app + `desktop_main.pyw` + `install.cmd`）、
  `desktop.py` 五条纪律（探活单实例 / 独立 `--user-data-dir` 窗口 / 窗口关=服务停 /
  `pythonw` 无 stdout 全落日志 / 打包版没有 Tk 故不许 tkinter）；
  `_pth` 追加 `..\app` 是唯一让 `import freeagent` 生效的地方；无 schema 变更；
  新增 `desktop.py`（已登记 §12.2）+ 不同树的 `tools/pack/build_portable.py`）。
  上一版 V1.143（Harness 统一入口 —— 「它记住了什么」一页四块只读回显：
  项目与规矩（`AGENTS.md` 只报文件事实，不解析内容）/ 记忆 / 事务与承诺（缺键 ≠ 0）/
  能力与设置；不新建存储、不调 `today.view()`；无 schema 变更；
  新增 `services/harness.py` / `web/endpoints_harness.py` / `web/js_harness.py`）。
  上一版 V1.142（继续借鉴 CLOAK 落三件 —— **安静模式**（一键收掉配置表单 /
  左栏 / 右栏，窄条写当前工作区 + Esc 退出，状态存 localStorage）、**工作区文件清单**
  （新 `GET /api/agent/files`，只读一层、不读内容，根收紧到工作区目录）、
  **「它在后台自己跑」一句**（把 V1.136 收过头的细节补回一句实话）；无 schema 变更；
  新增 `web/endpoints_agent_files.py` / `web/js_workbench_files.py` /
  `web/js_workbench_shell.py`）。
  上一版 V1.141（一句话起手 —— 工作台一句人话认项目 + 要做的事、预填表单、
  拿不准就追问下一句即答案；复用已配文本模型 `parse_delegation`，追问走规则不花钱；
  无 schema 变更；新增 `services/delegate_intake.py` / `web/endpoints_agent_intake.py` /
  `web/js_workbench_intake.py`）。
  上一版 V1.140（改名 —— FreeAgent 改成「值守 / Steward」：展示层
  （界面标题 / CLI / 启动脚本 / README / 文档标题）、命令层（`pyproject.toml`
  加 `steward` 三个入口，旧 `freeagent*` 留成别名）、数据目录层（`paths.py`
  收口原本三处重复的路径解析，目录改 `.steward`、变量改 `STEWARD_HOME`，
  旧名仍认且命中时打一行明示；旧 `.freeagent` 里有 `agent.db` 就沿用、不自动搬）；
  包名与文档文件名不动；无 schema 变更、无新文件）。
  上一版 V1.139（借鉴 CLOAK 的「个人 Harness」落两件 —— **工作区常驻
  左栏**（三栏 = 工作区 / 正文 / 产物，搜索 + 选中态 + 轮次数）、**选中工作区
  摆出「它已经记住了什么」**（备注可就地改 / 跑过几轮 / 本轮背景 / 规则来源）；
  轮次数走 `count_by_directory` 现算、不给档案加列；无 schema 变更；新增
  `web/js_workbench_rail.py`）。
  上一版 V1.138（按钮粒度授权 —— 授权卡第四枚「本会话内允许此类」，同类动作
  本会话免问；信号写 `decided_via="card_session"` 而非 `decision` 第三值
  —— `decide()` 会把非 `"allow"` 归一成 `deny`；无 schema 变更、无新文件）。
  上一版 V1.137（失败可恢复 —— 授权结论送回引擎有限重试，失败如实停住；
  新增 `services/permission_reply.py`；无 schema 变更）。
  上一版 V1.136（底座不突出显示 —— 界面 / 飞书用户可见文案不再点名
  引擎品牌、工作台顶栏收起 pid / 会话 / 目录内部细节；纯展示文案，无 schema
  变更、无新文件）。
  上一版 V1.135（工作台右栏加「变更」视图 + 产物 / 变更文件行可「在系统文件
  管理器中定位」；复用 engine_ledger + 受控端点；无 schema 变更；新增
  `web/endpoints_reveal.py`）。
  上一版 V1.134（实时面板不再挂幽灵行 —— 任务已删、进度行还在的
  孤儿投影读侧跳过；纯读侧，无 schema 变更、无新文件）。
  上一版 V1.133（参考豆包专业版/WorkBuddy/Today AI 的易用性四件套 ——
  周回顾加厚并上 Web、产物一键复制/导出、零配置首句引导、引擎用量可见；
  加厚 `/week` + 纯前端/服务层，无 schema 变更；新增 4 个模块）。
  上一版 V1.132（易用性向豆包/WorkBuddy 靠拢五件套 —— 快捷提示词
  填框可改、简洁模式开关（默认开）、对话内搜索与跳转、设置按用途分组、
  工作台产物侧栏同屏；纯前端，无 schema 变更；新增 5 个 JS/style 模块）。
  上一版 V1.131（修工作台快失败轮次漏终态事件 —— 先订阅事件流再发指令；
  idle 按 outcome 分 failed/interrupted/succeeded；停止标记当场收口；
  纯服务层，无 schema 变更、无新文件）。
  上一版 V1.130（修对话刷新后历史错乱 —— 发问时旧建议行只摘了
  DOM、没撤历史，刷新重放时退役 chips 插回消息中间；历史与界面保持 1:1；
  纯前端，无 schema 变更、无新文件）。
  上一版 V1.128（真机 v2.0.29 工作台三收口 —— 工具步骤按 `call_id`
  四段合成一行、正文剥 `@数字@` 标记、挂起授权兜底拉取；纯服务层，无 schema
  变更、无新文件）。
  上一版 V1.127（工作台目录选择器面板内重做 —— 行列表点行进入替代原生下拉、
  可见路径框可粘贴绝对路径回车校验、最近目录直达；后端列举跳重解析点、
  点目录排后；无 schema 变更、无新文件）。
  上一版 V1.126（工作台界面视觉收口 —— 中性现代风：提示 / 空态 /
  状态条改中性冷灰、主按钮实心、卡片轻阴影，米黄只留真警示；先只改工作台
  一页；新增 `web/style_workbench.py`；无 schema 变更）。
  上一版 V1.125（项目备注折进上下文 —— 选中项目的备注作为站立
  偏好随每轮带进引擎背景一行；无 schema 变更、无新文件）。
  上一版 V1.124（工作台项目档案选择器 —— **最近项目平铺 + 搜索 +
  新建项目 + 选择本地文件夹**，文件浏览退为二级；schema v35 新表
  `workbench_projects`；新增 `web/endpoints_agent_projects.py`、
  `web/js_workbench_projects.py`）。
  上一版 V1.123（工作台目录选择器 —— **面包屑跳转 / 快捷位置 +
  盘符入口 / 当前层过滤 / 当前目录选中态**；后端补 `drives`/`places`，
  新增 `web/js_workbench_dirs.py`；顺带修后台服务扫描的 powershell 绝对
  路径解析；无 schema 变更）。
  上一版 V1.122（规则层平行问候入口 —— **封闭招呼集合离线也回问候
  答复**，不再落澄清卡；夹带正事不接；纯服务层，无 schema 变更、
  无新文件）。
  上一版 V1.121（「更多」下拉三层分组 —— **我的事 / 执行与通道 /
  系统**；一级入口与接线不动；纯前端，无 schema 变更、无新文件）。
  上一版 V1.120（表达层接缝 `LLMProvider.phrase` —— **事实卡片
  由规则算死**、**模型只在卡片内组织措辞**、**给不出回退模板**；
  第一处接入问候；无 schema 变更、无新文件）。
  上一版 V1.119（导航信息架构收敛 —— **一级只留日常高频入口**、
  **其余视图收进「更多」下拉**、**承诺/待处理/催办计数聚合到更多角标**；
  顺手修 hidden 被作者 display 压过；新增 `js_nav.py` / `style_nav.py`，
  无 schema 变更）。
  上一版 V1.118（Web 易用性向「豆包」首屏习惯靠拢 —— **规则层状态
  首屏可见 + 直达设置**、**页脚不跨视图泄漏**、**Web 空态去 CLI 命令**；
  顺手修视图切换竞态；无 schema 变更、src 无新文件）。
  上一版 V1.117（工作台收尾 —— **清单原文转义**、**目录校验前置**、
  **回车提交**与**按钮置灰**；无 schema 变更、无新文件）。
  上一版 V1.116（易用性与安全性向「豆包工作」靠拢 —— **事后可见** +
  **失败后的下一步** + **自动档的代价说清**。**不加危险命令黑名单**：`executors`
  早就立过「不要枚举危险命令」，这一层加的是可见与可反驳 ——
  `TurnView.touched_files` / `.commands` 从 `steps` **现算**、不落库不加列
  （步骤清单已是全部工具事件的投影，再存一份就是第二个真源）；
  **命令那一列是安全项**：自动档下命令不弹卡也不进 `pending_approvals`，
  界面上不列就只有引擎自己知道；换档**只对下一轮生效**（界面上写明，
  否则人以为「刚切回逐次，它就该停下来问」）；失败后只**填回输入框**，
  不替人重发（重发花钱，且可能正因为它刚出事才停的）。
  上一版 V1.115（让工作台像「豆包工作」的那一半 —— **轮次落库 + 最近的活**、
  **步骤清单**、**上下文厚度**。三条依次落地，顺序本身就是结论：先让人
  「找得回刚才那件事」，再让「计划与进度可见」，最后补「它知道你忙什么」。
  schema v32 → v33（`workbench_turns`）→ v34（`context_note`），**一列一版**：
  v33 可能已被重放，改历史会让同一版本号出现两种形状。
  三条纪律（每一刀各一条）：**同刻定序不许按内容排**（`recent()` 用
  `rowid DESC`；连点两次会落在同一秒）；**形状识别只允许一份**
  （步骤清单复用 `tool_evidence`，否则进度卡与清单会对同一件事说两种话）；
  **注入只读、单行、读不出来要说出来**（不调 `today.view()` —— 它会顺延并
 在 `task_records` 留记录；换成 `scheduled_for()`）。
  更早版本（V1.114 → V1.52）
  历史见设计文档头部，**本文件只记当前最新，不重复整段历史**。

## 不要做（负面清单——边界，不是建议）

- **不要使用子任务**（`task` / 子代理 / 后台 explore-librarian 一律不用）。
  所有工作由当前会话直接完成：自己读码、自己改、自己跑测试。
  派生子任务的收益在这里抵不过上下文丢失与风格漂移的代价——
  本仓代码有强烈的「论证写在注释里」的风格，派生的代理接不住。
- 不要改测试来迁就实现；不要把 type check 用 `# type: ignore` 压掉。
- 不要动与本次需求无关的文件；Bugfix 只最小修复，不顺带重构。
- 不确定的安全/产品决策停下来问，不要替用户做假设。

## 代码风格

- 注释写「**为什么**」而不是「是什么」；踩过的坑把根因与教训写进注释，
  这是本仓最重要的资产（见 docs/postmortem/ 的格式）。
- 失败必须**fail-closed + 说得清原因**：拒绝要给下一步出路，
  不许静默降级（参考 `services/approval.py` 与设计文档 12.7.2）。
- 一份能力一份实现：两个通道（飞书/WEB/终端）共用同一服务层与同一写入口，
  不许为第二个通道复制逻辑（设计文档 12.1.1）。
- 新表必须走 `storage/db.py` 的 _MIGRATIONS，只 ADD COLUMN / CREATE TABLE，
  幂等；列用 ISO 文本时间戳。
  **唯一显式登记的 DML 例外**：v16（V1.37）给 `task_records` 加 `seq`
  序号列时，存量行要在同一步迁移里 `UPDATE ... SET seq = rowid WHERE
  seq IS NULL` 一次性回填（rowid 即当时的写入序，唯一能还原顺序的依据）。
  它只补序号、不碰业务字段、幂等（`seq IS NULL` 才跑）——
  除此之外任何迁移仍然不许写 UPDATE/DELETE。
- 测试用 `tests/conftest.py` 的临时库 + FrozenClock 夹具，时间必须可注入。

## 改能力时，文档要一起改（V1.96 补记的纪律）

**四份文档各管一段，改能力时四份一起看**：

| 文档 | 管什么 | 谁在查它 |
|---|---|---|
| `docs/个人事务助手设计方案.md` | **实现契约**（枚举/权重/阈值/命令表/结构树） | `test_doc_contract.py` 逐字比对 |
| `AGENTS.md`（本文件） | 当前进行中的工作、踩过的坑 | 每次会话自动加载 |
| `docs/现状速读.md` | **现状快照**（「现在能干什么、哪里有坑」） | ⚠️ **无测试会查** |
| `docs/使用说明书.md` | 面向使用者（怎么用、故障排查） | `test_doc_contract.py` 查「不许撒谎」类断言 |
| **`docs/真机验收矩阵.md`** | **审批链「人点过没有」的唯一真源** | `test_acceptance_matrix.py` 查「表有没有腐坏」 |

**第五份只在一类改动里要动**：碰了审批 / 停止 / 提问 / 超时 / 谁有资格点 ——
哪怕只是改了文案，也要去看矩阵里对应格子的「怎么验」还成不成立。**别在别处
写「某某仍未验」**，那是矩阵的活（V1.108 之前它被抄在十来处，必然烂掉几处）。

**动手之前另有一份，性质不同**：`docs/postmortem/lenses.md` 不是要「同步」的
文档，而是**改之前先对一遍**的索引 —— 8 条反模式 lens（默认值方向 / 闸门是
状态不是约定 / 一个事实一个写入口 / 用可观察事实不猜形状 / 文案不是契约 /
错判代价远大于漏判 / 标注说证据不说结论 / 预览与实发同源），每条带「本仓
已犯的例」。只过这次改动碰到的那几条，不用全过。**新增一篇复盘时顺手看它有没有
对应那条**（没有就加一条挂上去），否则它会像当年的现状速读一样落后而没人报红。

**为什么单列这条**（V1.96 盘点时踩到）：设计文档的变更块一直逐版本记全，
但 `现状速读.md` 停在 V1.84、`使用说明书.md` 停在 V1.83、`README.md` 最高只
提到 V1.39 —— **落后 12 版，而没有任何测试报红**。原因很具体：
`test_doc_contract.py` 只对「设计文档 + README + 说明书」做**不许撒谎**类
断言，**不查 `现状速读.md`**；而现状速读第 2 节标题写着「已实现且有测试守着」
—— 那句话本身成了最大的谎言。

两条推论：

- **只改设计文档 = 文档欠账**。设计文档是契约，改它测试会红；
  另三份是**面向人的**，改了没人拦，所以它们才是欠账累积的地方。
- **文档同步不占功能版本号**：纯文档同步挂在当版的变更块「补记」下
  （V1.96 的补记就是本轮），功能版本号只在代码或 schema 变了才顺延。
  但**要写进补记** —— 不写就成了「没人记得改过文档」。
- **注释里的乱码要清**（V1.96 发现）：本仓的注释承载论证，**少一个字就少一条
  论证**。历史上已积累 19 处 `U+FFFD`（替换字符）乱码散在 13 个文件里 ——
  它们**不进测试、不会报红**，只能靠 `grep -rn $'\ufffd'` 找出来。改文件时
  顺手扫一遍，别让新的进来。

## 当前进行中的工作（V1.157 完成 · 2026-11-23）

**收掉工作台的第二个输入框 —— 派活只在对话页；补「更多」浮层短窗口滚动（无 schema 变更、无新文件）。**

- **起因**：用户说「继续完成剩余工作」，清单里剩两件：工作台第二个输入框、浮层短窗口裁切。
- **收输入框**：`js_workbench_page.py` 移除 `#wb-text` / `#wb-send` 与一句话起手卡的挂载；工作台只留停止 / 注入 / 进度 / 授权 / 产物。`wbSend(force, presetText)` 在「仍交给引擎」重发时改用 `wbTurn.brief`，不读已删 DOM。
- **浮层滚动**：`.menu-pop` 加 `max-height: calc(100vh - 130px)` + `overflow-y: auto`。
- **不动**：写入口、审批闸门、授权两档、端点、schema（仍 35）、真机验收矩阵。
- **测试 / 走查**：`test_web_escape` 守重发回退；浏览器确认工作台只剩控制面、浮层可滚动、0 console error。
- **版本**：V1.156 → V1.157；无 schema 变更（仍 35）；无新文件。

## 上一版（V1.156 完成 · 2026-11-22）

**底部工具行补齐项目 / 模型 / 授权档选择器（OpenCode / WorkBuddy 同款位置；无 schema 变更、无新文件）。**

- **起因**：用户连发三张截图，说「在输入框的下方，加号和箭头中间，可以项目选择、模型选择和授权选择」。
- **改法**：`chatBarControls()` 把三个 `<select>` 插在 `.chatbar-tools` 里 + 与 ↑ 之间。项目来自 `/api/agent/projects`，模型来自 `/api/oc/options`，授权档来自 `/api/agent/state` 的 `modes`；改动走既有 `project_select` 与新 `POST /api/agent/prefs_save`（白名单写 directory/project_id/model/agent/variant/mode）。
- **不动**：写入口、审批闸门、授权两档含义、schema（仍 35）、真机验收矩阵。
- **测试 / 走查**：`TestChatShell` 守三选择器与端点、`test_agent_endpoints` 守白名单；浏览器确认三个下拉渲染、0 console error。
- **版本**：V1.155 → V1.156；无 schema 变更（仍 35）；无新文件。

## 上一版（V1.155 完成 · 2026-11-22）

**工作台从一级导航收进「更多 → 执行与通道」（无 schema 变更、无新文件）。**

- **起因**：用户说「可以收掉工作台了，对吧？」。
- **收什么 / 不收什么**：一级只留对话 / 今天 / 事务；工作台移到「更多 → 执行与通道」首位，标签「工作台（进度与授权）」。`data-view` / 路由 / 页面 / 端点一个没删 —— 它的执行 / 轮次 / 授权卡 / 停止按钮仍是唯一真源，对话页右栏还接不了授权卡，所以是**降入口不是删页面**。
- **测试**：`test_web_nav` 的 PRIMARY_VIEWS / MENU_VIEWS / MENU_GROUPS 同步。
- **版本**：V1.154 → V1.155；无 schema 变更（仍 35）；无新文件。

## 上一版（V1.154 完成 · 2026-11-22）

**对话页三栏外壳 —— 左工作区 / 中对话 / 右产物，两侧可收（OpenCode / WorkBuddy 同款；新增 `web/js_chat_shell.py`）。**

- **起因**：用户连发 OpenCode / WorkBuddy / 豆包截图，确认三家共同底座是「左工作区 + 中对话 + 右工具/产物 + 底部输入框」，只有豆包多「对话 / 工作」切换；明确「那就行动吧」。
- **改法**：`chatShellMount()` 把 `.chat` 包进三栏外壳；左栏列项目档案（`/api/agent/projects` → `project_select`），右栏显示最近一轮状态与改动文件（`/api/agent/state`，只读）；两侧按钮收起。中间永远是对话，不加模式切换（V1.152 已让对话能派活）。
- **不动工作台**：执行 / 轮次 / 授权卡链路原样；本轮只把观察面搬到对话页，第二个输入框另起一刀。
- **不动**：写入口、审批闸门、授权两档、schema（仍 35）、命令表、轮询、真机验收矩阵。
- **测试 / 走查**：`TestChatShell` 守接线 / 只读端点 / 收起；浏览器确认三栏渲染、0 console error。
- **版本**：V1.153 → V1.154；无 schema 变更（仍 35）；新增 `web/js_chat_shell.py`（已登记 §12.2）。

## 上一版（V1.153 完成 · 2026-11-22）

**输入区做成一体容器 —— + 与发送箭头并排在输入框下方那一行（纯展示；无 schema 变更、无新文件）。**

- **起因**：用户看了 OpenCode / 别人的布局，说「最左侧的加号和最右侧的箭头，都放到输入框下面，和输入框体现在一起」。
- **改法**：`chatbar` 改纵向容器（边框 / 圆角 / 底色），textarea 去边框融入，`#chatinput` 不变；新增 `.chatbar-tools` 行装 `#visionbar`（+）与 `#chatsend`（↑），+ 靠左、箭头靠右；焦点态移到容器 `:focus-within`。
- **不动**：按钮行为、粘贴图片、工具菜单、写入口、审批闸门、schema（仍 35）、真机验收矩阵。
- **测试 / 走查**：chatbar 结构守卫与按钮接线全绿；浏览器确认 + / ↑ 同一行、输入框融为一块。
- **版本**：V1.152 → V1.153；无 schema 变更（仍 35）；无新文件。

## 上一版（V1.152 完成 · 2026-11-22）

**对话成为唯一入口 —— "…"菜单加「交给它执行」，工作台退成观察面（第一刀；无 schema 变更、无新文件）。**

- **起因**：用户问「最佳实践是对话和工作台分开吗」，随后明确「按最佳实践修改吧，否则太奇怪了」。
- **这一刀**：`js_chat_search.py` 的 `chatDelegate()` 从对话输入框取话，读 prefs 的目录 / 档位，`window.confirm` 摆给人看，POST `/api/agent/start`（`force: true` = 人点了就是显式推翻判断）；成功后跳工作台看进度。入口在「…」菜单首项；工作台顶部加「写完一句话在对话里点…交给它执行，这里看进度 / 授权 / 设置」。
- **为什么是第一刀**：工作台的简报框连着执行 / 轮次 / 授权卡，一把拆掉会动写入口与授权呈现，风险不对等；彻底收掉第二个输入框须另起一轮并逐项对照验收矩阵。
- **不动**：写入口、审批闸门、授权两档、schema（仍 35）、命令表、轮询、真机验收矩阵。
- **测试**：`test_usability.py` 守确认框、两端点、force 与菜单接线。
- **版本**：V1.151 → V1.152；无 schema 变更（仍 35）；无新文件。

## 上一版（V1.151 完成 · 2026-11-22）

**+ 菜单借鉴豆包 / WorkBuddy 的分组，只列真有的入口（纯展示；无 schema 变更、无新文件）。**

- **起因**：用户连发 OpenCode / 豆包 / WorkBuddy 三张 + 菜单截图，说「可以借鉴一下」。借的是「分组 + 快捷键提示」这个形式，不是它们的功能清单。
- **添加组**：图片和文件（Ctrl+U，补了真能按的监听）、粘贴图片（Ctrl+V，聚焦输入框并提示）。**已有能力组**：角色 / 后台服务 / 飞书通道直达。不摆技能 / 插件 / 云盘 / 目标 / 计划这些本仓没有的项。
- **不动**：识别链路、视图分发、写入口、审批闸门、schema（仍 35）、真机验收矩阵。
- **测试 / 走查**：`test_usability` 守快捷键与三个导航入口；浏览器点开 + 菜单确认分组、0 console error。
- **版本**：V1.150 → V1.151；无 schema 变更（仍 35）；无新文件。

## 上一版（V1.150 完成 · 2026-11-22）

**输入栏改成 OpenCode 骨架 —— 左 + 右发送箭头，图片可直接粘进输入框（纯展示；无 schema 变更、无新文件）。**

- **起因**：用户说「借鉴 OpenCode 布局，最左边是 +，最右边是箭头，能否直接把图片拷贝到输入框？」。
- **右端箭头**：V1.149 删掉的发送按钮以紧凑 `↑` 回归（`#chatsend` / `.btn.primary.send-arrow`），回车仍是主路径，箭头照顾触屏 —— 上一版自记的代价补上。
- **粘贴图片**：`#chatinput` 的 `onpaste` 从 `clipboardData.items` 认 `image/*`，`preventDefault()` 后把 `File` 交给 `doVision()`，与 `+` 选图共用识别链路；非图片照常粘贴。
- **不动**：识别端点、写入口、审批闸门、schema（仍 35）、命令表、轮询、真机验收矩阵。
- **测试 / 走查**：`TestButtonHierarchy` 守箭头与粘贴接线；隔离库浏览器点过 + / ↑ 并验粘贴触发识别，0 console error。
- **版本**：V1.149 → V1.150；无 schema 变更（仍 35）；无新文件。

## 上一版（V1.149 完成 · 2026-11-22）

**对话输入区按豆包形态收口 —— "+" 收附件、右上角"…"收工具、回车即发送、页脚只留契约免责；顺带统一按钮三态（纯展示；无 schema 变更、无新文件）。**

- **起因**：用户连着指出「独立的拍照胶囊很难看（豆包是一个 +）」「发送按钮多余，回车就可以」「豆包搜索在顶部…里，我们独立搜索按钮别扭」，并问页脚那句能力边界是否多余。
- **"+" 附件**：`js_vision.py` 把独立拍照胶囊换成输入框左侧 `#visionbtn`（`+`），点开 `#attach-menu` 只列真有的「图片 / 拍照识物」+ 状态说明；点外面 / Esc 关闭，监听只绑一次。不编技能 / 插件 / 云盘这些没有的菜单项。
- **"…" 对话工具**：`js_chat_search.py` 加 `chatToolsMount()`，把搜索 / 清空 / 多选删除收进右上角菜单（有历史才给后两项）；输入栏只剩 `+` 与输入框。
- **删发送按钮**：`buildBar` 不再建 `#chatsend`；回车发送、Shift+回车换行进 placeholder。触屏无硬回车是代价，照实说。
- **页脚减负**：能力边界已在欢迎区（`menu.notice`）说过，页脚只留 `DISCLAIMER_MARKER` 那句；契约仍在页面里，测试守着。
- **按钮三态（V1.148）**：主动作 `.btn.primary`、工具 `.btn.ghost`、开关 `.btn.ghost[aria-pressed]`；`.ghost` 提升为全局类，跨导航 / 输入栏同款。删发送后主动作暂时空着。
- **不动**：行为 / 接线、视图分发、写入口、审批闸门、schema（仍 35）、命令表、轮询、真机验收矩阵。
- **测试 / 走查**：`TestButtonHierarchy`、`test_chat_js` 工具入口改挂菜单、`test_web` vision 入口守卫全绿；隔离库浏览器点过 `+` / `…` 菜单与按钮消失，0 console error。
- **版本**：V1.148 → V1.149；无 schema 变更（仍 35）；无新文件。

## 上一版（V1.147 完成 · 2026-11-22）

**一句话起手的两处「看着能用、点下去被拒」收口 —— 前提摆在输入之前、Key 配好视觉层立即重建（无 schema 变更、无新文件）。**

- **起因**：用户说「一句话起手，好像不管用」。隔离库实测：输入一句回「还没授权项目」，而同页工作台项目已选、`/api/health` 显示 DeepSeek 已配置 —— 人看到的是「配好了却不能用」。另一张截图问「已经配置了，为什么还提示没有 DEEPSEEK_API_KEY？」。
- **前提探测只读**：`services/delegate_intake.py` 加 `readiness(view, llm)`，与 `plan()` 共用前提文案常量；`GET /api/agent/intake/ready` 进 READ_ROUTES（不花模型）；前端 `wbIntakeMount()` 挂载即探测，**输入与按钮默认禁用**，就绪才放开；没项目给「去授权项目 → 飞书通道」，没模型给「去设置」，探测失败保守禁用说原因；结果存模块级 `wbIntakeReady`，重绘后由 `wbIntakeApply` 从缓存直接画 —— 浏览器走查抓到过初版把状态绑 DOM 上、重绘后永远停在「正在检查前提」（同 V1.142 文件清单的教训）。规则层明确当「没连模型」。
- **视觉层随配置重建（真 bug）**：`App.apply_config()` 原先只重建文本层 `llm`，`vision` 停在启动时的 `None`；设置页存完 Key 后健康接口按 `app.vision` 仍说「没有 DEEPSEEK_API_KEY」，配置与提示自相矛盾。现一并重建 `vision`，「配置立即生效」对视觉层也成立。
- **不动**：写入口、审批闸门、授权两档、schema（仍 35）、命令表、轮询机制、真机验收矩阵；V1.141「复用已配文本模型」的拍板未改。
- **测试**：`test_delegate_intake::TestReadiness`（4）、端点形状、`test_web_usability_js` 静态守卫、`test_vision` 的 apply_config 重建视觉层。
- **未真机复验**：隔离库 8811 浏览器点过，真机 8770 未跑；真机 W1–W5 引擎路径仍全 ⬜，真源只在 `docs/真机验收矩阵.md` 表 3。
- **版本**：V1.146 → V1.147；无 schema 变更（仍 35）；无新文件。

## 上一版（V1.146 完成 · 2026-11-22）

**「更多 → 执行与通道」的「编程」改名「委派总览」—— 页面功能没变，身份归位
（无 schema 变更、无新文件）。**

- **起因**：用户连问「执行与通道，需要强调『编程』吗？」「最初 OpenCode/编程是明确
  的一个功能，现在它成了编程底座，也有了独立的工作台，所以更多的编程需要调整」。
  第一性原理：导航写人要干的活、不写底座术语；最佳实践：改名只动身份、不动接线。
- **页面身份**：工作台（V1.114）接走了日常「说一句 → 交给它」的路径，旧「编程」页
  真正独有的是**全局委派实时面板**（跨项目进度 / 授权卡 / 提问，工作台只看单轮）与
  **四段选型配置**（与飞书共用）。名字停在「让引擎写代码」是旧身份，不是功能缺失。
- **只改名与文案**：导航项 + 页内标题都叫「委派总览」；`data-view="oc"`、路由、页面
  内容、执行与通道首位一律不动。不删两步委派卡，不升级角标（会和工作台授权卡成
  第二个真源，违背 V1.136「底座不突出显示」）。
- **裸 CLI 收口**：`delegate.py` / `engine_errors.py` 的页签指向改「委派总览」页；
  `js_oc_cards.py` / `endpoints_oc_dispatch.py` 的「它得在跑（python -m ...）」改
  「先在「后台服务」里确认它在跑」；`js_delegate_projects.py` 的重启提示改
  「到「后台服务」里重启桥接与执行器」。裸 CLI 只留终端命令表与故障排查文档。
- **注释同步**：直接称呼该页的注释 / docstring 改「委派总览页」；`js_workbench.py`
  历史注释留旧名并注明「今委派总览」；`executors.py` / `opencode_server.py` 的
  「编程」指任务类别、不是页面，原样保留。
- **不动**：写入口、审批闸门、授权两档、schema（仍 35）、命令表、轮询机制、
  `/api/oc/*` 形状、真机验收矩阵。
- **测试**：`test_web_nav.py` 加 oc 入口名守卫；`test_usability.py` 加网页上屏串
  守卫（无裸 CLI / `tools\run_feishu.bat`、错误文案指新页名）。
- **未真机复验**：改名后未在真机 8770 点过；真机 W1–W5 引擎路径仍全 ⬜，真源只在
  `docs/真机验收矩阵.md` 表 3。
- **版本**：V1.145 → V1.146；无 schema 变更（仍 35）；无新文件。

## 上一版（V1.145 完成 · 2026-11-22）

**对话首屏重做 —— 空对话从「左上角一句 + 两排胶囊 + 黄色可关闭引导卡」改成居中欢迎区：
招呼 / 能力边界 / 「试试这样问」/ 例子格（点一下填框，不发送）；无 schema 变更；
新增 `web/style_chat.py`。**

- **起因**：用户说「这第一个对话界面很不友好，你看看 opencode 和 workbuddy」，附了截图
  （本会话看不到图）。核对现状：`renderChatShell` 空分支先 `addMsg(..., "kind-help")`
  再 `addChips(menu.can_do)`，`js_onboard` 又往顶上插一张 `.note`（米黄警示底）的引导卡
  —— 第一屏没有视觉重心，且那条黄底让人以为出错了。
- **改成什么**：`.chat-welcome` 居中块 —— `.cw-hero`「你好，我是值守」/ `.cw-sub` 用
  `menu.notice` 原文 / `.cw-label`「试试这样问」/ `.cw-grid` 把 `menu.can_do` 排成卡片。
  点一下只填 `#chatinput` 并触发 `input`（与 V1.132 chips 同一条：填框可改，不替人发）。
- **空对话才居中**：没有历史时给 `.chatlog` 加 `is-empty`，`.chatlog.is-empty` 纵向居中；
  `addMsg` 一进来就摘掉 `is-empty` **并 `remove()` 掉 `.chat-welcome`** —— 两处必须在同一处
  收口。浏览器走查抓到过初版只摘类、欢迎区仍堆在对话流顶部的真 bug；静态守卫只看
  「渲染出来了」，看不出「状态切换后该走没走」。
- **去掉可永久关闭的引导卡**：空对话本来就该友好，让人先点「不再显示」才肯给好界面是
  反模式；欢迎区只在无历史时出现，发一句话自然消失，刷新回来还是它 —— 不需要 localStorage。
- **规则层横幅改中性**：`.note` → 新 `.chat-status`（卡片底 + 细边），文案与「去配置模型」
  不变；空对话时并进欢迎区底部，有历史才置顶。它说的是状态，不是「出事了」。
- **落点**：新 `web/style_chat.py`（`STYLE_CHAT_CSS`；`style.py` 已贴 250 行、`style_usability`
  是跨视图，故另立）；`web/js_onboard.py` 保留文件名与 `JS_ONBOARD`，内容换成
  `renderChatWelcome(menu)`；`js_chat.py` 空分支改调它并管 `is-empty`、横幅改类名；
  `js_boot.py` 删掉 `onboardMaybe()`；`page.py` 拼 `STYLE_CHAT_CSS`。
- **不动**：写入口、审批闸门、授权两档、schema（仍 35）、命令表、轮询机制、`CAN_DO` /
  `/api/chat` 形状、真机验收矩阵（不碰审批 / 停止 / 提问 / 超时）。
- **测试**：`tests/test_usability_v133.py` 的 `TestOnboard` 改写为 `TestChatWelcome`
  （招呼 / 用 `menu.can_do` 出例子 / 点一下只填框不发送 / `esc(menu.notice` / 由
  `renderChatShell` 摆 / 发第一句话时欢迎区必须消失）；`test_chat_js` / `test_web` /
  `test_web_usability_js` / `test_web_js_syntax` 全绿；`js_chat.py` 249 行（≤ 250）；
  **全量回归 exit 0、20 skip，与基线一致**。
- **互动浏览器走查（隔离库 8811 + 会话内浏览器）**：欢迎区渲染（7 张 `cw-card`、无
  `#fa-onboard`、横幅中性）→ 点卡片只填 `#chatinput`、不新增消息 / 不发 POST → 发一句话
  后 `welcome:0 / is-empty:false / msgs:2` → 清空历史后欢迎区回来；控制台 0 error、无
  4xx/5xx。上面那个 bug 就是这一轮点出来的。
- **未真机复验**：新首屏已在隔离库浏览器里点过，但真机 8770 未跑；真机 W1–W5 引擎路径
  仍全 ⬜，真源只在 `docs/真机验收矩阵.md` 表 3。
- **版本**：V1.144 → V1.145；无 schema 变更（仍 35）；新增 `web/style_chat.py`
  （已登记 §12.2）。

## 上一版（V1.144 完成 · 2026-11-22）

**Windows 桌面安装包第一层 —— 官方 embeddable Python 拼 `dist/Steward`，双击即用，
不再需要装 Python / 开黑窗 / 记端口（无 schema 变更；新增 `desktop.py` 与
`tools/pack/build_portable.py`）。**

- **起因**：用户连问「所有人都做桌面客户端，我们为什么不做？符合人性吗？」
  「能不能借鉴 CLOAK 把 OpenCode 和 Steward 打包成一个安装包」。承认过去的判断
  用错了层：**「引入壳会破坏核心零依赖」只对 Electron 成立**，正确边界是
  「核心零依赖，壳可以是可选 extra」。桌面客户端的产品价值 = 把「一个需要启动的
  服务」变成「一个一直在的东西」（V1.142 那句「它在后台自己跑」就是缺这个壳的症状）。
- **分层（用户已同意）**：① 本层只包 Steward，可真跑；② 安装器可选「一并装
  OpenCode」官方 x64 独立二进制 + MIT 声明，**只当兜底**（未落）；③ 不做自动更新、
  不做「OpenCode 官方壳」、不钉死单一引擎版本。放弃引擎可换是架构前提
  （我们被其 V2 事件流绊过两次：V1.128 / V1.131）。
- **产物形态**：`dist\Steward` = `runtime\`（embeddable 3.11.9，`python311._pth`
  追加 `..\app`）+ `app\freeagent\`（src 副本）+ `desktop_main.pyw` + `install.cmd`
  + `uninstall.cmd` + `VERSION.txt`，27.1 MB。装到 `%LOCALAPPDATA%\Steward`，
  桌面 `Steward.lnk` 指向 `runtime\pythonw.exe`（指向 `python.exe` 会闪黑窗）。
- **为什么不 PyInstaller**：单文件 exe 是杀软误报重灾区（重打包哈希一变就被拦），
  且把运行时变成不可见的一坨。embeddable 里看得见 `python.exe` / `python311.zip`。
- **`desktop.py` 五条纪律**：探活单实例（只认 `"ok": true`，端口上别的服务不算）/
  窗口必须是独立浏览器进程（`--user-data-dir=<状态目录>\window`，否则 `--app` 只是
  把 URL 交给已运行的 Edge、自身立刻退出，`wait()` 永远等不到人关窗口）/
  窗口关 = 服务停（fail-closed，无托盘就没有第二个可见停止口，代价是在跑的那轮中断）/
  `pythonw` 没有 stdout 故一切失败落 `desktop.log` / 打包版不含 Tcl-Tk 故不许 tkinter
  （V1.143 留白的「原生选文件夹」在打包版直接失效）。
- **三个真 bug（都是真跑才露的，已修 + 已加守卫）**：
  ① `pythonw` 下 `sys.stdout/stderr` 是 `None`，`http.server` 每请求写访问日志 →
  `None.write` 抛 `AttributeError` → 连接被关掉，curl 空回复（exit 52）而日志无线索；
  `python.exe` 跑一辈子复现不了。修法 `_bind_stdio()` 把 None 流兜到 `desktop.log`。
  ② PATH 被裁剪的机器上 `xcopy` / `powershell` 都不在 PATH → 两个 bat 里一律走
  `%SystemRoot%\System32\...` 全路径（实测空 PATH 下装成功）。
  ③ uninstall 直接 `rmdir` 自己所在目录 → cmd 读下一行失败报
  「The system cannot find the path specified.」、收尾文案全丢 → 删除交给
  `start /b` 起、脚本退出后才跑的 helper（PowerShell 延迟 900ms）。
- **实测证据**：headless 与带窗口两条路径 `/api/health` 都 `ok`；杀窗口进程组 →
  日志「窗口已关，停服务」+ 端口无响应 + pythonw 退出；`install.cmd`（空 PATH）→
  快捷方式生成；按快捷方式的 TargetPath/Arguments 启动 → 8770 health ok；
  `uninstall.cmd` → 程序目录与 lnk 消失、`~/.steward` 一行未动。
- **代价照实说**：产物是「目录 + install.cmd」（不是 MSI，不是每天一版）；**未签名**，
  网上下来的 zip 带 Mark-of-the-Web，解压后可能被 SmartScreen 拦一次，只有代码签名
  能根治。
- **刻意没做**：第二层（可选装 OpenCode）；托盘 / 通知 / 开机自启；把包放到
  **另一台没装 Python 的机器**上跑（本轮只在本机验）；自动更新（第三层，不做）。
- **不动**：schema（仍 35）、写入口、审批闸门、授权两档、步骤投影、轮询机制、
  provider 协议、命令表、真机验收矩阵（不碰审批 / 停止 / 提问 / 超时），
  `src` 里既有模块一行未改。
- **测试**：`tests/test_desktop.py` 28 条（浏览器发现 / 窗口命令行四要素 /
  探活只认 ok:true / 日志写不进去不抛 / `_bind_stdio` 重绑且写入可见 / 零依赖 AST
  扫描含禁 tkinter / 构建脚本五个守卫含 ASCII 与系统全路径与「删除交给 helper」/
  假 runtime zip 验组装与缺件 SystemExit）。**全量回归 exit 0、20 skip，与基线一致**
  （3 条 `--deselect` 同前；注意本机 PATH 若缺 `System32\WindowsPowerShell\v1.0`，
  `test_reset.py::test_detects_real_services_on_this_machine` 会假红 —— 那条真机测试
  靠 PATH 里的 `powershell` 认进程，是既有弱点，非本轮引入）。
- **版本**：V1.143 → V1.144；无 schema 变更（仍 35）；新增 `desktop.py`
  （已登记 §12.2）与不同树的 `tools/pack/build_portable.py`（构建期工具，
  不进 12.2 —— 那份树只收 `src/freeagent`）。

## 上一版（V1.143 完成 · 2026-11-22）

**Harness 统一入口 —— 一页看清「它为你累积了什么」：项目与规矩 / 记忆 /
事务与承诺 / 能力与设置四块，全是已有事实的回显（无 schema 变更；新增三个模块）。**

- **起因**：CLOAK 主线还剩 Harness 没做全。V1.139 只做了**工作区级**的累积块
  （`#wb-harness`，标题「这个工作区累积了什么」），缺一处回答「换掉这个工作区之后，
  它还记着我什么」。本版补全局那一页 —— 用户的「继续」即按「先 ② 聚合页、再 ③
  原生选文件夹」执行。
- **为什么不新增一张表**：Harness 的价值在「把散在四处的东西摆到一处」，不是再存一份。
  再存一份必有漏写的写入口，两边数随后漂移（同 V1.115 不另存 `touched_files`、
  V1.139 不给档案加 `turns` 列）。故四块各读各的真源，**零 schema 变更**。
- **三条纪律（`services/harness.py` docstring）**：① 只读 —— 不写表、不建表、
  不顺延事务，**不调 `today.view()`**（会顺延并往 `task_records` 留记录，V1.115 的坑）；
  ② 一块读不出来不拖垮其它块 —— 失败进 `notes` 点名原因，**绝不把「读不出来」写成 0**；
  ③ 不解析 `AGENTS.md` 内容 —— 只报在不在 / 行数 / 大小 / 何时改过，解释权在引擎。
- **四块取舍里的具体判据**：行数只读前 8192 字节，超了 `lines=None` + note（报一个
  「前 8KB 的行数」当全体行数等于编数）；记忆条数 `len < 200` 才精确，撞上限写「至少」
  进 notes；三个计数**每项独立 try、失败就不给键**（缺键 ≠ 0，前端画「读不到」）；
  能力面如实说「没有可反复调用的能力库（Skills）」，不凑数。
- **落点**：`services/harness.py`（`RuleFact` / `ProjectFact` / `MemoryItem` /
  `HarnessView` + `_rule_fact` / `_projects` / `_memory` / `_workload` / `_skills` /
  `_settings`，`view(app, *, mode="")` → `_build`，`as_dict` 直出 `asdict`）；
  `web/endpoints_harness.py`（`mode` 由端点用 `load_prefs` 取一次传入 —— 服务层
  不读 web 偏好；补 `kind="harness"`）；`web/js_harness.py`（`harnessNum` 缺键→「读不到」、
  `harnessSize`、`harnessRuleLine`、`renderHarness`）；接线四处：
  `routes.py` 的 `/api/harness`、`markup.py` 的「我的事」组首 `data-view="harness"`
  （文案「它记住了什么」）、`js_nav.py` 的 `MENU_VIEWS`、`js_boot.py` 的分发分支、
  `page.py` 拼 `JS_HARNESS`（在 `JS_WEEK` 之后，只依赖 `JS_CORE`）。
- **一处实现细节值得记**：`_settings` 的 `state_dir` 必须传 `config.home` ——
  无参会按 `STEWARD_HOME` / 旧名 / `~/.steward` 解析，测试与多实例下就报错路径。
  初版还留过一个 `... if False else _build(...)` 的三元残留，已去掉。
- **刻意没做**：四块做成可编辑（这页是回显不是管理，改入口都在原处）；用一页替代
  那些视图（各有动作）；解析 `AGENTS.md` 里的规矩条目；Skills 能力库本身（存储 +
  注入 + 授权，新能力面，单独立项）。
- **同轮留白（诚实记账）**：原计划同轮做「原生选文件夹」薄补丁（`tkinter` 的
  `askdirectory`，替代磨了两版的目录浏览面板），**本版没落**。卡点是结构性的：
  `web/server.py` 的 `do_POST` 整段持 `app.lock`，同步弹窗会冻结整个服务（停止按钮、
  轮次轮询全停）直到人选完；tkinter 又必须在同一线程建全部对象，而 `serve_forever`
  占着主线程。要么改锁粒度、要么两段式（`pick/start` + `pick/result` 轮询），
  两条都要动 `server.py` 的结构 —— **等用户点头再动**，不夹带。
- **不动**：schema（仍 35）、写入口、审批闸门、授权两档、步骤投影、轮询机制、
  provider 协议、命令表、真机验收矩阵（不碰审批 / 停止 / 提问 / 超时）。
- **测试**：`tests/test_harness.py`（18 条：规矩四态 / 记忆三态 / 事务缺键不给 0 /
  Skills 说不有 / 设置三态 + `data_dir` / 只读纪律静态守卫 + 端点注册 / 前端四处接线
  + 「读不到」文案）；`tests/test_web_nav.py` 的 `MENU_VIEWS` 与
  `MENU_GROUPS["我的事"]` 同步登记 `harness`（菜单用正则反查 + 分组配平，不登记必红）；
  `test_doc_contract.py` §12.2 登记三个新文件。**全量回归 exit 0、20 skip，与基线一致。**
- **隔离库走查（8811）**：`tests/manual/ui_walkthrough.py` 17 视图（自动从导航枚举，
  新增的 `harness` 直接覆盖）0 pageerror / 0 控制台 error / 0 坏响应；实测四块都渲染
  （`AGENTS.md` 130 KB 时如实写「行数没数完」+ 原因）。走查顺手改一处文案重复：
  规矩行原本前后各说一遍「行数没数完」，现在前缀只报大小、原因留给服务层的 note。
- **未真机复验**：Harness 页只在隔离库的模拟浏览器里点过（真机 8770 未跑）；
  真机 W1–W5 引擎路径仍全 ⬜，真源只在 `docs/真机验收矩阵.md` 表 3。
- **版本**：V1.142 → V1.143；无 schema 变更（仍 35）；新增
  `services/harness.py` / `web/endpoints_harness.py` / `web/js_harness.py`。

---

## 上一版（V1.142 完成 · 2026-11-22）

**继续借鉴 CLOAK 落三件 —— 安静模式（一键收起外壳）、工作区文件清单（右栏只读
一层）、「它在后台自己跑」那一句（把 V1.136 收过头的细节补回一句实话；无 schema
变更；新增三个模块）。**

- **起因**：用户说「继续借鉴CLOAK」。先核 CLOAK 官网能力清单（8 项：多任务并行 /
  团队群聊 / 文件与图片 / Zen 模式与语音 / 多模型 / 微信接入 / Skills 与任务中心 /
  产物与交付中心）与本仓现状，排除三类：团队向（群聊 / 团队后台 / 微信）、要动引擎
  并发（多任务并行）、属新能力面（Skills 安装与授权、语音）。剩三条按「与已有改动
  是否配套 + 成本」排序，用户回「都做」。
- **① 安静模式**（`web/js_workbench_shell.py` 的 `wbZenBoot`）：收掉顶部配置表单
  （`#wb-bar`）、左栏工作区、右栏产物（`.wb-view.is-zen` 三条 CSS），宽度还给正文
  与输入框。**收掉不等于失明** —— 顶上留窄条写「安静模式 · 当前工作区」+ 退出按钮，
  Esc 同退；状态存 `localStorage`（`wbZen`，存不下就按「没开」）；挂载在
  `loadWorkbench` 末尾（页面重建必须重新挂）。
- **② 工作区文件清单**（`web/endpoints_agent_files.py` + `web/js_workbench_files.py`）：
  新 `GET /api/agent/files`，右栏拆两块 —— 上块 `#wb-side-turn` 归本轮产物（每轮清空
  重画），下块 `#wb-files` 归「这个工作区有什么」（常驻）。三条约束：根**只能**来自
  工作区档案或偏好目录（绝对路径与 `../` 一律拒绝，与「定位」端点同一套 `realpath` +
  `relative_to`）；一层不递归、上限 300、超了如实给 `truncated` + `total`；**一个字节
  都不读**（只 `scandir` + `stat`）。跳过的重解析点如实计数（`skipped_links`），判据
  复用 `workspace._is_reparse`（只此一份）。
- **③ 「它在后台自己跑」**（`wbRunNote`）：V1.136 把内部细节收干净，副作用是收过头 ——
  人不知道关掉这一页之后那一轮还在不在跑。这句话恒显、不随引擎状态变（事实不是状态），
  并指向停止口。
- **顺带改正一句假声明**：`agent_dirs` 原 docstring 写「这是**唯一**能读到工作目录里
  有什么的端点」，现在改成两条通道的分工（一条只列目录、一条连文件列）。
- **刻意没做**：文件内容预览（编码 / 超大文件 / 二进制是另一件事）；`#wb-files` 的手输
  路径入口（有它根就白收）；语音；用 `/api/delegate/browse` 顶替（它默认关闭、只列目录、
  语义是委派白名单浏览）。
- **不动**：schema（仍 35）、写入口、审批闸门、授权两档、步骤投影、轮询机制、provider
  协议、命令表。
- **测试**：`tests/test_agent_files.py`（14 条：没选工作区是 `state` 不是空列表 / 未知
  project / 无目录 / 偏好回退 / 绝对路径拒绝 / `../` 拒绝 / 子目录不存在 / 目录在前 /
  条目形状无内容字段 / 子目录 rel 与 parent / 根 parent 空 / 上限 truncated 且 total 如实
  + 前端接线 6 条）；`test_usability.py::TestWorkbenchSide::test_fill_function` 的清空
  目标换成 `#wb-side-turn`（容器契约变了，不是迁就实现）。**全量回归 exit 0、20 skip**
  （`--deselect` 掉 3 条环境性真机测试，理由同 V1.139）。
- **隔离库走查抓到一处真 bug（教训）**：新探针 `tests/manual/zen_files_probe.py`
  （非交付物）40/40 通过、0 pageerror；`ui_walkthrough.py` 16 视图 0 error。第一遍
  探针就红了 —— `wbFilesPaint` 开头把 `wbFilesPath` 重置成空，`wbFilesOpen` 刚设好的
  层当场被抹掉，**点目录永远原地不动**。两边单看都对（一边设路径、一边重画），错在
  **先后顺序**，只有真 DOM 点一遍才露。修法：`wbFilesPaint(at)` —— 「画哪一层」由
  **调用点**说（切 / 建 / 删工作区传空回根），并加
  `test_files_paint_takes_the_level_from_the_caller` 钉住「paint 里不许重置路径」。
  教训：**「重画时顺手归一化状态」是反模式** —— 归一化属于触发点（切工作区），
  写在重画函数里等于每次重画都推翻调用方的意图。
- **未真机复验**：安静模式与文件清单只在隔离库的模拟浏览器里点过（真机 8770 仍是旧
  进程，页面常量启动时拼装，要重启才见新界面）；真机 W1–W5 引擎路径仍全 ⬜，真源只在
  `docs/真机验收矩阵.md` 表 3。
- **版本**：V1.141 → V1.142；无 schema 变更（仍 35）；新增
  `web/endpoints_agent_files.py` / `web/js_workbench_files.py` /
  `web/js_workbench_shell.py`。

---

## 上一版（V1.141 完成 · 2026-11-22）

**一句话起手 —— 工作台一句人话认出「改哪个项目 + 做什么」，预填表单，拿不准
就追问、下一句即答案（借鉴飞书插件的对话式起手；复用已配文本模型；无 schema
变更；新增三个模块）。**

- **起因**：用户拿 `opencode-feishu-plugin` 问「飞书交互有没有可借鉴的」，明确
  「按你的建议落地三件」（#5 失败可恢复 → #4 按钮粒度授权 → #1 对话式起手，
  前两件已落在 V1.137 / V1.138）。插件的「一句话起手」= 一句话 → 定目录 →
  预填表单 → 点创建，拿不准就追问、下一条消息即回答。本仓工作台本就有
  「选目录 + 写一句话 + 交给它」这道表单，缺的只是「一句话把前两项一起填上」。
- **AI 承接用哪个模型（用户拍板）**：复用**已配文本模型**（`app.llm`，与能力路由 /
  表达层同一套接缝），落点是已有协议 `LLMProvider.parse_delegation(text,
  known_projects) -> DelegationIntent`（契约本就是「抽不出就空串，上层追问」）。
  规则层 `RuleBasedProvider.parse_delegation` 如实返回空意图 → 离线 fail-closed，
  不猜项目、不预填。不给 provider 加新方法（一份能力一份实现）。
- **追问补缺是规则，不调模型**：模型只在首次抽一次；「下一句 = 答案」是确定性的 ——
  要 brief 就原样当 brief，要 project 就拿下一句去授权清单做大小写不敏感精确比对。
  追问再调模型既多花计费，又把「人的回答」交给模型自由发挥，不划算。
- **不变量**：项目名必须原样命中 `CapabilityView.labels()` 才认（猜错项目的代价是
  在错误仓库里动手）；空字段是要追问不是失败；目录按 labels 与 projects 的下标
  对齐回查；认不出 / 没连模型 / 没授权项目都如实说原因，绝不替人预填或起一轮。
- **落点**：新 `services/delegate_intake.py`（`plan` 纯函数，carry 记住追问上下文）、
  新 `web/endpoints_agent_intake.py`（POST `/api/agent/intake`，会花模型一次请求故
  只 POST，进 agent_post 派发表）、新 `web/js_workbench_intake.py`（识别卡 + 追问
  回答框）由 `js_workbench_page` 一行挂进正文，认出来后预填既有 `#wb-dir` /
  `#wb-text` 与项目按钮，人确认后再点既有的「交给它」。动态文本一律 textContent / esc()。
- **真机矩阵没动**：#1 是新 UI / 端点，不碰审批闸门、授权两档、停止 / 提问 / 超时，
  矩阵表 1 各格「怎么验」仍成立。
- **不动**：写入口、审批闸门、授权两档、步骤投影、schema、命令表、轮询机制、provider 协议。
- **测试**：`test_delegate_intake.py`（首次解析各分支 + carry 追问不花钱）、
  `test_agent_endpoints.py`（intake 已知动作 + 空文本拒绝）、`test_web_usability_js.py`
  （模块进页面 / 挂卡片 / 调端点 / 预填 / 追问原样送回 / 动态文本不进 innerHTML）。
  `test_web_js_syntax.py`（node --check）与 `test_doc_contract.py`（§12.2 三文件登记）均绿。
  **全量回归 exit 0、20 skip（`--deselect` 掉 3 条环境性真机测试，理由同 V1.139）。**
- **未真机复验**：#1 的识别 / 追问流程没在真机浏览器点过；真机 W1–W5 引擎路径仍全
  ⬜，真源只在 `docs/真机验收矩阵.md` 表 3。
- **三项交付完成**：#5（V1.137）/ #4（V1.138）/ #1（本版）全部按序落地。

---

### 上一版 V1.140（同日完成）

**改名 —— 产品名从 FreeAgent 改成「值守 / Steward」，三层一起换；包名不动
（无 schema 变更、无新文件）。**

- **起因与授权**：用户只说了「FreeAgent 改个名字吧」，没给名字。我先量影响面
  （`FreeAgent` 590 / `from freeagent` 1044 / `freeagent` 任意大小写 2319 /
  `FREEAGENT_` 145 / `.freeagent` 56 / `FreeAgentError` 281），再问两件：名字
  （用户定 `Steward` / 值守）、层次（用户答「三层，做」）。
- **「彻底」的边界**：用户能接触到的是**界面 / 命令 / 目录**三面；包名
  `src/freeagent` 是内部实现，改它不增加任何用户可见的一致性，只断 1044 处
  import，并把 13 版未提交的功能改动绞进同一个 diff。**这次不动包名。**
- **展示层**：`markup.py`（title「值守 · 个人事务助手」、h1「值守」）、
  `cli/app.py`（启动语 / 命令表标题 / `/ask` 帮助里的自称）、`web/serve.py`
  横幅、`run_web.bat`（提示改 Steward；home 三行改成如实说法）、README
  （标题 + Startup 名 `Steward-bridge.cmd`，并写明旧那份要先删）、
  `pyproject.toml` description、三份文档的文件内标题。
- **命令层**：`[project.scripts]` 加 `steward` / `steward-feishu` /
  `steward-feishu-doctor`，旧三个 `freeagent*` **留成别名**。
- **目录层（顺带还债）**：`paths.py` 变唯一解析点；`db.resolve_db_path` 与
  `oc_selection._curated_path` / `_selection_path` 原本各写一遍
  `Path.home()/".freeagent"`（三处重复，历史上漂移过一次），现在全改调
  `state_dir`。`oc_selection` 顺带修掉「不认 HOME 环境变量、漏传 home 就写进
  开发者真实目录」这个真 bug。
- **旧名不静默**：`FREEAGENT_HOME` 仍认；`~/.freeagent` 有 `agent.db` 而
  `~/.steward` 没有库时沿用旧目录。两种命中都往 stderr 打一行明示（只打一次）。
  判据是「有没有 `agent.db`」而不是「目录在不在」—— `resolve_db_path` 会
  `mkdir` 空目录，拿空目录当「已在用新的」会把旧库静默甩开。
  **不自动迁移**：搬真库不可逆，搬不搬由用户下令。
- **刻意没做**：包名；`docs/个人事务助手设计方案.md` 这类文件名（契约稳定路径，
  改名断所有交叉引用，留结构刀）；`deepseek.py` 4 处「给一个个人事务助手做
  分类」的模型指令措辞（改它是行为改动）。
- **测试**：`test_config.py::TestHomeCompat` 5 条 + `TestHomeEnvWhitespace`
  改按 `paths.DEFAULT_DIR_NAME` 并把 `Path.home` 钉到临时目录；
  `test_manual_contract.py` 把 `LEGACY_HOME_ENV` 纳入手册必写集合。
- **全量回归**：exit 0、20 skip，与基线一致（`--deselect` 掉 3 条环境性真机
  测试，理由同上一版）。

### 上一版 V1.139（同日完成）

**借鉴 CLOAK 的「个人 Harness」落两件 —— 工作区常驻左栏、选中工作区摆出
「它已经记住了什么」（纯读后端 + 前端；无 schema 变更；新增
`web/js_workbench_rail.py`）。**

- **起因**：用户先问「了解一下 cloak」，补一句「一个智能体」后锁定 43cloak.com
  这支（桌面 Agent，底座无感跑，核心叙事是「用户级 Harness：你持续补充，它就
  持续贴近你的工作方式」）。我列了「三条值得抄 / 两条别抄」，用户回「按你的
  建议落地吧」。本轮落前两件；第三件（桌面客户端）**单独立项，不做**。
- **① 工作区常驻左栏**：新 `web/js_workbench_rail.py` 的 `wbRailMount()` 由
  `loadWorkbench` 一行接进 `.wb-cols` 的**最左**，两栏变三栏（工作区 220 /
  正文 flex / 产物 280；1100px 收 172px，860px 折一列）。行 = 名 + 「N 轮 ·
  目录」；选中态 `is-current` **加**一道左侧实心边（颜色只辅助，不单通道）；
  搜索按名 / 目录筛；空态给「选本地文件夹」。**下拉保留**，两个入口共用同一份
  `wbProjects`，选中与轮次数只在一处画（`wbLoadProjects` / `wbInitProjects` 里调）。
- **② 累积块 `#wb-harness`**：四行都是已有事实的回显 —— 备注（原本前端没有
  编辑入口，本版补上，走 `project_update`）、跑过几轮、本轮背景（读正文那一个
  `#wb-inject`，**不在左栏另放开关**）、规则（项目里的 `AGENTS.md` 由后台引擎
  自己读）。
- **轮次数现算**（`WorkbenchTurnRepo.count_by_directory`），不给
  `workbench_projects` 加列：轮次表是唯一真源，加一列必有漏加的写入路径，两个
  数字随后漂移（同 V1.116 不另存 `touched_files`）。`agent_projects` 每项补
  `turns`；**turn_repo 缺席 / 读失败回 `null` 并说「轮次读不到」，0 说「还没
  跑过」** —— 把前者写成 0 是替人下结论。
- **刻意不抄**：①「完全访问权限」默认放开 —— 抄的是「一处可见的档位切换」，
  `ask` / `auto_dir` 与 fail-closed 一字不动；② 团队后台（成员 / 分组 / 额度 /
  审计）—— 会引入服务端、账号与额度，破坏本地零依赖。桌面客户端单独立项。
- **不动**：写入口、审批闸门、授权两档、步骤投影、轮次落库、schema、命令表、
  轮询机制、真机验收矩阵。动态文本一律先 `esc()`。
- **测试**：`test_workbench_projects.py::TestProjectCounts`（只数本目录 / 0 与
  null 分开 / 无 turn_repo 回 null）、`test_web_usability_js.py::TestWorkbenchRail`
  （拼装 / 三栏顺序 / 转义 / 读不到不说成 0 / 备注走 project_update / 样式只挂
  `.wb-view`）、`test_web_js_syntax.py`。**定向 61 条绿**。
- **全量回归**：除 3 条环境性真机测试外全绿（`test_opencode_projects_live.py::
  test_can_really_query_opencode`、`test_prompt_payload.py` 两条 live —— 机器上
  并行跑着 9 个 OpenCode 进程，测试起的发现服务 60s 起不来；这三条与本轮改动
  无交集，`git` 未提交前如实记在这里）。
- **浏览器走查**：隔离库 8811 跑 `tests/manual/ui_walkthrough.py`（16 视图 0
  pageerror / 0 控制台 error / 0 坏响应）+ 新探针 `tests/manual/rail_probe.py`
  （13/13：三栏顺序、220px、行数、点行选中三处同步、搜索筛、备注落库、关背景
  当场改口、左栏没有第二个勾选框）。截图 `tests/manual/shots/rail/rail.png`。
  走查改过一版：备注与「改」并排会把按钮顶到第二行 → 改成「键在上、值在下」。
- **未真机复验**：真实引擎路径 W1–W5 仍全 ⬜，真源只在 `docs/真机验收矩阵.md` 表 3。

---

## 上一版（V1.138 完成 · 2026-11-22）

**按钮粒度授权 —— 授权卡加「本会话内允许此类」一枚按钮，同类动作本会话内免问，
换动作 / 换一轮 / 换目录仍问（借鉴飞书插件的授权范围粒度；无 schema 变更、无新文件）。**

- **起因**：用户拿 `opencode-feishu-plugin` 问「飞书交互有没有可借鉴的」，明确
  「按你的建议落地三件」（#5 失败可恢复 → #4 按钮粒度授权 → #1 对话式起手）。
  插件把授权范围做成按钮粒度（允许一次 / 始终允许 / 本会话内允许 / 拒绝）；本仓
  此前只有「允许这一次 / 拒绝 / 停止」三枚，长任务里同类动作要反复点。本版落第二件（#4）。
- **三轮 question 拍板**：① 纯内存、不加 schema ② 换档清除联动不做（保持现状）
  ③ 飞书也做、复用字段传信号。**key 粒度 = 动作类型 `permission`**（点一次
  「本会话内允许 edit」后本会话内所有 edit 免问，换 bash / write 仍弹卡）。
- **信号落 `decided_via` 而非 `decision`（关键）**：`decision` 是 `Literal["allow","deny"]`
  两值枚举，`approval.py` 的 `decide()` 把任何非 `"allow"` 归一成 `deny` —— 写第三值
  会在第一次点击就被拒。故新档仍写 `decision="allow"`，把信号写进既有文本字段
  `decided_via="card_session"`（原有 `"card"` / `"stop"`），零判据改动、不加列。
- **「本会话」边界**：委派 = `run_with_tool_gate` 一次调用内局部变量
  `session_allows: set[str]`，函数返回即失效；工作台 = `_Turn` 一轮内同名字段。
  换授权档不清除；不做「永远允许」（换一轮 / 换目录即失效，面板 note 明说）。
- **修法**：① 飞书 `_tool_card` 三按钮加第四枚「本会话内允许此类」（`type=default`，
  不抢 primary / danger）；桥接白名单收 `allow_session`，落库写 `decision="allow"` +
  `via="card_session"`，卡面回执写明范围；执行器 `_ask_one_tool` 发卡前查同类命中
  直接返回 `("allow","")` 免问，拿到决策后只读 `decided_via=="card_session"` 才记入集合。
  ② 工作台 `AskCard` 加 `permission` 字段（`label` 是展示文案不能当 key）；`decide()`
  收 `scope="session"` 时记 `turn.ask.permission`（空不记，fail-closed）；`_handle_permission`
  挂卡前查同类命中，命中则直接 `reply_permission(...,"allow")` 不挂卡、不置 WAITING；
  面板按钮经 `/api/agent/decide` 的 `scope` 字段透传（该文件贴 250 行上限，改动只加 2 行）。
- **真机矩阵动了**：表 1 新增 `A13`（飞书话题内点本会话内允许）/ `A14`（Web 面板点本会话内
  允许）两验点，各写「怎么验」+「为什么不能顺带证明」，同步 `test_acceptance_matrix.py`
  编号全集（A1–A14）。新按钮 = 新验点，旧格子「怎么验」仍成立。
- **不动**：闸门判定、授权两档底层含义、步骤投影、schema、命令表、轮询机制。
- **测试**：`test_card_action.py`（via 落 card_session / allow_once 仍 card）、
  `test_user_stop.py`（第四按钮 type=default）、`test_agent_turn.py`（同类免问 / 换动作仍问 /
  空 permission 不记）、`test_tool_gate.py`（`_ask_one_tool` 同类免问 / 换动作仍发卡 / 只认
  card_session）、`test_agent_endpoints.py`（scope 透传）、`test_web_usability_js.py`（面板按钮
  静态守卫）。**全量回归 exit 0，20 skip 与基线一致。**
- **未真机复验**：A13/A14 及整套「同类免问」未在真机点过（新按钮 + 跨进程信号 + 内存集合
  从没真跑）；真机 W1–W5 引擎路径仍全 ⬜，真源只在 `docs/真机验收矩阵.md` 表 3。
- **后续一件**：#1 对话式起手（动手前先确认 AI 承接用哪个模型）。

---

## 上一版（V1.137 完成 · 2026-11-21）

**失败可恢复 —— 人批了但结论没送回引擎时，有限重试后如实停住，不再假装成功
（借鉴飞书插件「审批放行被静默吞掉」的修复；无 schema 变更；新增
`services/permission_reply.py`）。**

- **起因**：用户拿 `opencode-feishu-plugin` 问「飞书交互有没有可借鉴的」，明确
  「按你的建议落地三件」（#5 失败可恢复 → #4 按钮粒度授权 → #1 对话式起手）。
  插件 v0.2.22 修过一类故障：人点了「允许」、卡片也改了，但放行指令被静默吞掉，
  会话卡到超时。本仓同一处两个形态：`delegate.py` 的 `oc.reply_permission(...)`
  无 try/except，`agent_turn.py` 用 `contextlib.suppress(Exception)` 吞掉 ——
  卡面显示「已允许」，引擎那边还挂着请求。本版落第一件（#5）。
- **修法**：新 `services/permission_reply.py` 的 `deliver(send, ...)` 一份能力一处
  实现（12.1.1）：零参 callable 把 rid / 决策绑好，有限重试（3 次、间隔 0.4s）、
  `sleep` 可注入、失败回 `ReplyResult(ok=False, error=...)` 带原因。两个调用点各按
  通道呈现：委派侧重试用尽 → 进度卡写「授权送回失败」+ 原因、abort 会话、收尾
  `ok=False` 且摘要给「重新发起」这条出路；工作台侧重试失败 → 轮次 `FAILED`、
  备注 + 原因、abort。自动档异常来卡的拒答与提问答复也走同一份 `deliver`，
  不再静默。
- **刻意不做卡片内「🔁 重试」按钮**：桥接是独立进程，只靠 `pending_approvals`
  表与主进程通信；卡内按钮要加 schema 列 + 双向信号 + 主进程轮询，代价远超收益。
  本版用自动重试 + 如实呈现 + 停住本轮替代，交付时如实说明这层形态差异。
- **不动**：闸门判定、授权两档、步骤投影、schema、命令表、轮询机制、真机验收矩阵
  （正常路径行为不变，表 1 允许/拒绝格「执行器按结论继续」仍成立，已核对）。
- **测试**：`tests/test_permission_reply.py`（7）、`tests/test_agent_turn.py` 补授权与
  提问 reply 失败两条、`tests/test_tool_gate.py` 补委派 reply 失败一条（失败时停住 +
  摘要给出路）。目标文件 115 条全绿。
- **文档同步**：设计文档头部 V1.137 变更块 + §12.2 登记 `permission_reply.py`；
  使用说明书故障排查补「点了允许但卡面没生效」一条；现状速读委派行补一句。
- **未真机复验**：reply 送不到引擎这条失败路径未在真机触发；真机 W1–W5 引擎路径
  仍全 ⬜，真源只在 `docs/真机验收矩阵.md` 表 3。
- **后续两件**（按用户选的顺序）：#4 按钮粒度授权（动手前先确认「本会话内允许」
  与 auto_dir 的边界）、#1 对话式起手（动手前先确认 AI 承接用哪个模型）。

---

## 上一版（V1.136 完成 · 2026-11-20）

**底座不突出显示 —— 界面 / 飞书的用户可见文案不再点名引擎品牌，工作台顶栏收起
pid / 会话 / 目录等内部细节（纯展示文案；无 schema 变更、无新文件）。**

- **起因**：用户要「OpenCode 作为 Agent 底座不应突出显示，参考之前 WEB 界面的
  设计文档」，并拿飞书截图点名「让 opencode 改代码」那枚入口按钮。核对第 21 章
  （V1.114）与 12.6 Web UI 的规范性原文——「引擎藏到后台（用户无感）」「你不用懂
  OpenCode」——当前把品牌名与内部细节摆前台与之自相矛盾，改造是对的。
- **两项决策**（用户明确选）：① 界面 / 飞书用户可见文案里的品牌名一律换中性说法
  （执行引擎 / 后台引擎 / 它 / 桌面端）；品牌名只留代码注释与故障排查文档，故障
  说明保留语义但不点名、不给出 `opencode --version` 这类点名命令。② 工作台顶栏
  收起 pid / 会话 ID / 目录，只留「就绪 / 在跑」+ 用量一行。
- **落地**：`js_workbench` 引擎行删 pid/session/directory 三行，保留 `Token 读不到：`
  分支与 `#wb-engine`；飞书入口「让 opencode 改代码」→「让它改代码」并与「照着发」
  模板逐字同步；能力层「让 opencode 改 <项目>」指令模板改中性（router 的 `_VERBS`
  含「改」仍触发）；oc_discovery / oc_selection / oc_cards / delegate_projects 等
  故障与说明文案中性化；`_executors` 执行器事实名 `opencode` 保留（事实清单不是突出显示）。
- **不动**：写入口、审批闸门、schema、命令表、轮询机制、真机验收矩阵（本轮只改展示
  文案、不动闸门语义）；审批卡 subject（`opencode 想…`）与 CLI 命令表逐字对账不碰；
  注释 / docstring 里的品牌名保留（lens L5 文案不是契约、L3 一份能力一份实现）。
- **测试**：`tests/test_usability.py::TestEngineNotHighlighted`（剥注释后静态扫 JS 串 +
  AST 取 Python 字符串字面量，禁旧文案、留保留项；该模块 25 条全绿）。
- **文档同步**：设计文档头部 V1.136 变更块 + 第21章 / 12.6 补「底座不突出显示 /
  用户无感」规范措辞；本文件同步。纯文档补记不占功能版本号。
- **未真机复验**：真机 W1–W5 引擎路径仍全 ⬜，真源只在 `docs/真机验收矩阵.md` 表 3。

---

## 上一版（V1.135 完成 · 2026-11-19）

**工作台右栏加「变更」视图 + 产物 / 变更文件行可「在系统文件管理器中定位」
（复用 engine_ledger + 受控端点；无 schema 变更；新增 `web/endpoints_reveal.py`）。**

- **起因**：用户拿 WorkBuddy / 豆包要「学习他们的易用性」，核对官方文档后
  从 A–F 清单里选 A+B。真缺口不是从零造：`engine_ledger`（V1.110）早能读引擎
  会话改动账（路径+次数），产物侧栏（V1.132）早列出碰过的文件，缺的是
  把「改了什么」摆进右栏、以及从文件行一步跳到系统文件管理器。
- **两处修法**（用户两问均明确选真实现，不选保守替代）：
  ① **变更视图**：`EngineHost.ledger()` 读 `engine_ledger.read_session_ledger`
  （15s TTL 缓存、换会话清缓存、读不到返回 `unreadable` 带原因，**不显示 0**），
  `agent_state` 暴露 `ledger`，前端 `wbPaintLedger` 三态——`unreadable` 说原因 +
  「别把「没记录」当成「没改」」、`none` 说「不等于没做事」、`changed` 逐文件
  列路径+次数。`_ledger_dict` 故意返回结构化字段不拼人话（lens L5：文案不是契约）。
  账里只有路径与次数没有逐行差异时**如实说**，不编 diff。
  ② **定位**：新端点 `agent_reveal`（`web/endpoints_reveal.py`）走 win32 `explorer
  /select,` / darwin `open -R` / 其他 `xdg-open` 目录，`subprocess.Popen` 参数数组
  + `shell=False`；**验根内**（realpath + relative_to，同 delegate_browse），越界拒绝
  且不起进程；失败说得清、给下一步。前端 `wbLocateRow` 把路径存 `dataset.path`
  （不进 innerHTML），产物行与变更行都有「定位」按钮。
- **为什么不复用 touched_files 当变更数据源**：那是 steps 现算的派生物、无次数、
  无会话账语义；`engine_ledger` 才是 V1.110 验证过的真改动账（真机一会话 431 次调用可读），
  一份能力一份实现（lens L3）。
- **不动**：写入口、审批闸门、schema、轮询机制、真机验收矩阵；C（输入框 `/`
  意图面板）单列下一刀。动态文本一律先 `esc()` 或走 `textContent`。
- **测试**：`tests/test_engine_host.py::TestLedger`（读+TTL/无会话/无 call 接口/halt 清缓存）、
  `tests/test_agent_endpoints.py::TestReveal`（验根内/越界拒绝/无根报错/state 带 ledger/
  未知动作列 reveal）、`tests/test_usability.py` 三态文案与定位守卫（转义强度不降）。
- **未真机复验**：真机 token / 改动账形状未在浏览器真机核实；`W1`–`W5` 真机引擎
  路径仍全 ⬜，真源只在 `docs/真机验收矩阵.md` 表 3。

---

## 上一版（V1.134 完成 · 2026-11-19）

**实时面板不再挂幽灵行 —— 任务已删、进度行还在的孤儿投影读侧跳过
（纯读侧；无 schema 变更、无新文件）。**

- **起因**：真机面板出现一张「(事务已不存在) · 正在执行 · 18/18 次」的卡，
  「已经 N 秒没动静」涨到 2789313（≈32 天）仍不动。查本机库确认：
  `delegate_progress` 只剩一行，`updated_at` 冻结在 2026-10-18，对应
  `tasks` 行已删 —— 执行器当年被强杀没走收口，任务后来被删，投影行没人清。
- **为什么不自愈**：`delete_task` 虽在服务层清进度行，但强杀 + 老库留的孤儿
  行绕过它；执行器启动恢复 `reclaim_abandoned` 明确跳过已删事务；实时面板
  旧修法只是 `get()` 换 `find()`，坏行照常显示为「(事务已不存在)」且永远陈旧。
- **修法**：`web/endpoints_delegate_live.py` 投影行循环里 `if task is None:
  continue` —— 孤儿投影不显示、但**不删数据**（留取证）；待批项仍走
  `by_task` 第二循环照常显示（不藏待批项）。一次性清理了本机那行幽灵数据。
- **不动**：写入口、审批闸门、schema、轮询机制、真机验收矩阵。
- **测试**：`tests/test_web_delegate_live.py` 改「坏行降级展示」为「孤儿行隐藏
  + 好行照常」，补「藏进度行不连带藏待批卡」；单文件 58 条全绿。
- **未真机复验**：面板里幽灵行消失已由本机库清理 + 代码守卫覆盖；
  `W1`–`W5` 真机引擎路径仍全 ⬜，真源只在 `docs/真机验收矩阵.md` 表 3。

---

## 上一版（V1.133 完成 · 2026-11-18）

**参考豆包专业版 / WorkBuddy / Today AI 的易用性四件套 —— 周回顾加厚并上 Web、
产物一键复制 / 导出、零配置首句引导、引擎用量可见（加厚 `/week` + 纯前端 /
服务层，无 schema 变更；新增 4 个模块）。**

- **起因**：用户拿三家 Agent 对比表要「参考这 3 个 Agent」，四项全选并授权按
  价值排序。核对后真缺口不是从零造：`/week` 早已存在（六分区），只差加厚 +
  Web 视图；草稿 / 简报 / 答复只在库里，缺复制 / 导出；规则层本就能离线聊，
  缺首访引导；引擎账（V1.110）只读改动、不报 token，缺用量可见。
- **四处修法**（按价值排序，不碰写入口与闸门）：
  ① **周回顾加厚 + Web**：`WeekView` 末尾加三个带默认值字段 `decisions` /
     `promises` / `turn_count`（旧构造兼容），`WeekService` 接三 repo；
     `render_week` 与 CLI 出「本周拍板 / 承诺 / 交给引擎 N 轮」三段；新
     `web/serialize_week.py` + `web/js_week.py` 出 Web 视图，进「更多 → 我的事」。
     拍板 / 承诺逐字冻结，不解析 `options_json`、不给建议。
  ② **产物直接可交**：`js_artifact.artifactCopyBar` 对任何有正文的稿无条件给
     「复制这一版 / 导出 .md」；`js_workbench_side` 产物栏给「复制答复 / 导出 .md」。
     Blob 下载纯前端无端点；文件名逐字符白名单过滤，反斜杠 / 引号走
     `String.fromCharCode`（V1.114 的坑，`test_web_js_syntax` 咬过）。
  ③ **零配置首句引导**：新 `web/js_onboard.py`，首访插卡片，三个例子点一下
     **填进输入框不发送**（与 chips 同交互）；「不再显示」写 localStorage
     `fa_onboard`，读不到按首次显示处理（fail-closed 到引导）。
  ④ **用量可见**：新 `services/engine_usage.py` 读 `GET /api/session/{sid}` 的
     `tokens`，多形状兼容 + fail-closed（认不出报 unreadable 带原因，**绝不显示
     0**）；`EngineHost.usage()` 加 15s TTL 缓存、换会话清缓存；`agent_state`
     暴露 usage，`wbPaint` 显「Token 输入 / 输出」或「Token 读不到：原因」。
     真机 token 形状未验，UI 如实标注。
- **不动**：写入口、审批闸门、授权两档、步骤投影、schema、真机验收矩阵；
  动态文本一律先 `esc()` 或走 `textContent`。
- **测试**：新 `tests/test_engine_usage.py`（15）、`tests/test_usability_v133.py`
  （14）、`tests/test_week.py::TestWeekTraces`（7）；`test_web_nav.py` 登记 week；
  `node --check` 全过；全量回归 exit 0，20 skip 与基线一致。
- **浏览器走查**：隔离库 8811 走 16 视图（加了周回顾页）+ onboard 卡片 / 周回顾
  九分区 / 复制导出按钮 / 用量两种文案，16 项人工模拟全 PASS；0 pageerror /
  0 控制台 error / 0 坏响应。探针 `tests/manual/v133_probe.py`（非交付物）。
- **未真机复验**：真实引擎路径 `W1`–`W5` 仍全 ⬜，真源只在
  `docs/真机验收矩阵.md` 表 3。
- **补记（2026-11-19，纯文档，不占功能版本号）**：把当天三轮讨论的结论落进文档，
  无代码与 schema 改动。①"完全本地化 = 自我封闭？"要拆两半——本地的是数据面
  （记录 / 记忆 / 审批 / 账本在自有 SQLite + 环境变量），能力面一直借外部模型；
  封闭"关于你的一切"、开放"关于世界的能力"是信任边界，不是关门。②"核心零依赖"
  理由见设计文档 §12.1 / §19.2（隐私半径 / 断网可用 / 克隆即跑 / 零依赖是保证性质
  不是护城河）；不变量是数据不出本机 / 断网 / 克隆即跑，不是零行库。③总判据
  **守性质别守形式**：理由是信任 / 数据模型 / 失败行为→坚持，是当年只能这样→重问；
  坚持不替用户做取舍 / 等候一等公民 / fail-closed / 一份能力一份实现 / 可查可改可删，
  可重评的是零依赖表述、向量检索、CLI 中心、微信入口（都只是"该问"非决定）。
  ④AgentCore 品类错位、借鉴价值低，唯一对得上的 token 可审计接 V1.133 用量可见，
  台阶可见→归因→管控但 token 形状未真机核实仍不做闸门。落点：现状速读 §10.5、
  设计方案 〇.7 元纪律后补记；使用说明书无使用动作不动；矩阵不动。

---

## 上一版（V1.132 完成 · 2026-11-18）

**易用性向豆包 / WorkBuddy 靠拢五件套 —— 快捷提示词填框可改、简洁模式开关
（默认开）、对话内搜索与跳转、设置按用途分组、工作台产物侧栏同屏
（纯前端；无 schema 变更；新增 5 个 JS/style 模块）。**

- **起因**：用户要「参考豆包和 WorkBuddy 提高 FreeAgent 的易用性」，五项全选
  并授权我按价值排序。竞品共性是把「起头成本」与「信息过载」当成易用性主战场：
  场景标签点一下填框（可改）、过程可折叠、长对话能搜、设置按用途分组、产物与
  过程同屏。FreeAgent 已有 chips / 步骤清单 / 停止 / 轮次历史，缺的正是这五处。
- **五处修法**（按风险顺序落）：
  ① **chips 填框**：`addChips` 的 `b.onclick` 从 `sendChat(s)` 改成把原文填进
     `#chatinput` + focus + 触发 `input`，人能改完再发（V1.130 的退役 chips
     清理逻辑一字未动）。
  ② **简洁模式**：新 `web/js_compact.py`（localStorage `fa_compact`，默认开，
     fail-closed 到简洁）+ 新 `web/style_usability.py`；按钮由 JS 挂进 `#nav`
     （不动 markup），`body.compact` 只折工作台的 `.wb-steps` / `.wb-step-more`
     / `.wb-process` 足迹卡，步骤头「步骤（N）」保留 = 折叠而非删除。
  ③ **对话搜索**：新 `web/js_chat_search.py`，Ctrl/Cmd+F 或「搜索」按钮开面板，
     命中用 `<mark>` 包、逐条跳（Enter 下一条）+ 计数；高亮前用 `dataset.faOrig`
     还原原文防 mark 嵌套，动态文本一律 `textContent`，不进 `innerHTML`。
  ④ **设置分组**：新 `web/js_settings_groups.py` 一个 `section-label` helper，
     `renderSettings` 在模型 / 超时 / 排序三处前各插一组标题，字段仍挂 `form.set`。
  ⑤ **产物侧栏**：新 `web/js_workbench_side.py` 填 `#wb-side`（文件 / 命令 / 答复），
     `js_workbench_page` 把原流搬进 `.wb-col-main` 并排右栏，窄屏 @media 折一列；
     `wbPaint` 每次重画同步侧栏（无轮次也清残留）。
- **不动**：写入口、审批闸门、授权两档、步骤投影、schema、真机验收矩阵；
  命令列原文照旧先 `esc()` 再上屏。
- **测试**：新 `tests/test_usability.py`（五件套接线 / 转义 / 顺序静态守卫）；
  `test_chat_js.py::TestChipsFillInput` 钉填框不发送；`node --check` 全过。
  全量回归 exit 0（基线 V1.131 全绿，20 skip 与基线一致）。
- **浏览器走查**：隔离库 8811 走 15 视图，0 pageerror / 0 console error /
  0 坏响应；简洁按钮 / 搜索面板 / 侧栏 / 分组标题逐项人工模拟通过。
- **未真机复验**：真实引擎路径 `W1`–`W5` 仍全 ⬜，真源只在
  `docs/真机验收矩阵.md` 表 3。

---

## 上一版（V1.131 已完成 · 2026-11-18）

**修工作台快失败轮次漏终态事件 —— 先订阅事件流再发指令；idle 按 `outcome`
收口；停止标记当场收 stopped（纯服务层；无 schema 变更、无新文件）。**

- **复现**：真机一轮 0.8s 内因 `provider.no-route`（模型不可路由）失败，
  FreeAgent 轮次却一直 `running`：步骤空、正文空、状态条一直「引擎在跑」，
  停止看不生效，干到 600s 超时才收口，还误报「它可能还在改文件」。引擎库
  会话 `idle_outcome=failed`、没跑任何工具；隔离探针证实失败终态帧是
  `session.execution.failed`（`data.error.message`），别名与错误提取链路
  本身完好。
- **根因**：`_run` 先 `prompt()` 后建事件流；V2 `/api/event` 是 volatile 流，
  订阅前发出的帧直接丢。快失败的终态帧在指令后不到 1s 发出，流还没挂上，
  于是收不到结束信号。
- **修法**：① `ensure` → `events()`（当场 `urlopen` 建连）→ `prompt()`；
  ② idle 读 `outcome`：`failed` 带引擎错误记 failed、`interrupted` 记
  stopped、缺字段按完成兜底；别名补 `session.execution.interrupted` → idle；
  ③ 循环每条事件先看停止标记，当场收 stopped，不等下一条事件。
- **不动**：授权两档与闸门、步骤投影、40 工具上限、600s 超时兜底；模型
  no-route 属选型/凭据层问题，不在本事件链路里改。
- **已知边界**：volatile 流中段溢出丢帧仍靠 600s 超时兜底；按 durable
  日志/消息端点回填终态的轮询未做，留下一刀。
- **测试**：`test_agent_turn.py` 补订阅先于指令 / idle failed 带文案 /
  idle interrupted 记 stopped；`test_executors.py` 补 interrupted 别名。
  全量回归 exit 0，20 skip 与基线一致（19 变异探针 + 1 符号链接）。
- **未真机复验**：no-route 快失败那轮的真实收口要在真机再起一次不可路由
  模型观察；`W1`–`W5` 真机引擎路径仍全 ⬜，真源只在 `docs/真机验收矩阵.md` 表 3。

---

## 上一版（V1.130 已完成 · 2026-11-18）

**修对话刷新后历史错乱 —— 发问时旧建议行只摘 DOM、没撤历史，刷新重放时退役
chips 插回消息中间（纯前端；无 schema 变更、无新文件）。**

- **修法**：`sendChat` 摘旧 `.chips` DOM 的同时，`history_` 过滤掉既有
  `who === "chips"` 条目并 `chatSave` 落盘；回放路径 `addChips(..., false)`
  不参与清理。
- **测试**：`test_chat_js.py::TestChipsHistoryPurge`；探针
  `tests/manual/chat_refresh_probe.py`（非交付物，未登记 §12.2）。

---

**工作台步骤清单呈现收口 —— 每条一张整宽 `.note` 卡片改成卡内紧凑行，状态颜色
辅助文字、当前步高亮、超 8 步折叠（纯前端呈现层；无 schema 变更、无新文件）。**

- **起因**：真机一轮 15 条步骤，每条都是整宽带边框底色的 pill 叠成灰墙，行内
  只有「序号. 工具 · 状态」，视觉重量与信息量错配；长清单全展开把答复挤到屏外。
- **前端**：渲染器收进 `js_workbench_card.py` 的 `wbStepsCard(steps)`（`js_workbench.py`
  贴 250 行上限，只留调用）；行结构 `wb-step-idx / name / target / status`，目标
  为空不拼双空格；最后一条 running 高亮 `is-current`，已执行弱化；超 8 步收起早期
  步骤，「展开全部 N 步 / 收起」。
- **不动**：`StepView(name, target, status)` 形状、服务层收口纪律（无完成证据不
  替步骤判完成）、授权闸门；状态三值文字不变，颜色只辅助、不单通道传颜色。
- **测试**：`test_web_usability_js.py::TestWorkbenchStepsList` 守拼装 / 行类名 /
  折叠阈值与开关 / 样式作用域前缀；`test_web_escape.py` 守步骤名与目标过 `esc()`。
- **未真机复验**：紧凑行与折叠的浏览器观感待隔离库走查；`W1`–`W5` 真机引擎路径
  仍全 ⬜，真源只在 `docs/真机验收矩阵.md` 表 3。

---

## 上一版（V1.128 已完成 · 2026-11-17）

**真机 v2.0.29 工作台三处收口 —— 工具步骤按 `call_id` 四段合成一行、正文剥
`@数字@` 残留标记、挂起授权兜底拉取（纯服务层；无 schema 变更、无新文件）。**

- **起因**：真机一轮 15 条步骤目标全空且全卡「进行中」、正文漏出 `@17@`、最后
  一条 shell 授权在 FreeAgent 侧没弹卡。隔离实例抓 SSE 帧 + OpenAPI schema 互证：
  V2 工具四段（`input.started` / `called` / `success` / `failed`）同一 `id`
  串联，name 只在 started、参数在 called、结果在 success/failed。旧适配器把 called
  当内部 tool 却要 `data.name`（called 不带 name → 丢弃），success/failed 旧版没接。
- **步骤投影**：别名表补 `success`/`failed` → `tool.done`；started 带
  `call_id`+name，called 带 `call_id`+参数+显式 running，done 按事件名定
  completed/error。轮次层内存 `call_index` 配对（不落库），命中早先行补目标 /
  收终态；V1 形状退回同名同目标合并；丢开始帧又认不出名字不编名硬列行。预算
  仍只数 `kind == "tool"`。
- **收口兜底**：失败 / 停止把残留 running 记 failed；完成态无完成证据不往完成倒。
- **标记清理**：`@纯数字@` 在替换前 / 落库前 / 旧库回读三处剥离；不误伤邮箱与
  @提及。
- **授权兜底**：起轮前 + 轮内 3 秒节流各拉一次 `pending_permissions`；同一
  `parse_request` 判据，认不出跳过，rid 去重（实时 / 兜底共用 `seen_cards`）；
  拉取失败回空，不在兜底层放行。**闸门判定未改**；最后一条授权未弹卡根因仍待
  真机复验（事件丢失 / 前端未带出 / 会话沿用 saved 范围，三选一）。
- **测试**：`test_agent_turn.py` 补三段合成一行 / failed / 不编名 / 标记剥离 /
  兜底捞卡 / 去重坏载荷；`test_executors.py::TestV2ToolLifecycle` 守四段形状；
  `test_engine_host.py::TestPendingPassthrough` 守透传与失败回空。
- **未真机复验**：兜底能否兜住最后一条 shell 授权，要在真机 8770 再跑一轮观察；
  `W1`–`W4` 真机引擎路径仍全 ⬜，真源只在 `docs/真机验收矩阵.md` 表 3。

---

## 上一版（V1.127 已完成 · 2026-11-17）

**工作台目录选择器面板内重做 —— 行列表点行进入替代原生下拉、可见路径框可
粘贴绝对路径回车校验、最近目录直达；后端列举跳重解析点、点目录排后
（无 schema 变更、无新文件）。**

- **起因**：旧面板把一整层目录塞进原生 `<select>`，选中即下钻，导航与选择挤在
  一个控件；家目录首屏混着 Windows 遗留 junction 空壳与点目录；路径框隐藏不能
  粘贴；没有最近用过的目录。口径已拍板：面板内重做（不做居中浮层）；跳重解析点，
  点目录保留但排后。
- **后端**：`agent_dirs` 用 `os.scandir` 枚举，跳过 symlink 与 Windows junction
  （`FILE_ATTRIBUTE_REPARSE_POINT 0x400`；属性读不到按不列处理 fail-closed）；
  普通目录在前、点目录在后；补 `recent`（项目档案最近使用倒序、去重、top 5）。
- **前端**：`.dir-row` 行列表点行进入，底部「选用此目录」选定当前层；路径框
  `#wb-dir-path` 粘贴绝对路径回车走同一道 `resolve_workdir()` 校验；最近目录
  chips；面包屑 / 快捷位置 / 盘符 / 当前层过滤保留；行名先 `esc()` 再进 DOM。
- **安全边界不动**：仍只列目录、不读内容、不递归、上限 300；枚举不是白名单；
  选定前后同一道目录校验。
- **测试**：`test_agent_endpoints.py::TestDirs` 补点目录排后 / 非重解析点 /
  最近目录去重；`test_web_usability_js.py` 守行列表 / 路径框 / recent；
  `test_web_escape.py` 转义断言改指新行结构。
- **浏览器复验**：隔离库 8811 走查 15 视图 0 pageerror / 0 控制台 error / 0 坏
  响应；行列表进入 / 上一级 / 过滤命中 / 路径回车跳转 / 坏路径就地报错均人工
  模拟通过。`W1`–`W4` 真机引擎路径仍全 ⬜，真源只在 `docs/真机验收矩阵.md` 表 3。

---

## 上一版（V1.126 已完成 · 2026-11-17）

**工作台界面视觉收口 —— 中性现代风：提示 / 空态 / 状态条改中性冷灰、主按钮
实心、卡片轻阴影，米黄只留给真警示；先只改工作台一页（无 schema 变更；
新增 `web/style_workbench.py`）。**

- **起因**：工作台满屏复用 `.note`（`--warn-soft` 米黄底 + `--warn` 棕字），
  提示条、授权代价说明、注入勾选行、路由 / 引擎状态、历史空态全是警示色；
  主按钮浅绿描边，主次不清。
- **作用域**：规则全部挂 `.wb-view` 容器，工作台 DOM 收进这一层；其余 14 个
  视图本轮不动。颜色只复用现有 CSS 变量，不重写全局令牌；深色模式吃既有覆盖。
- **拆块守 250 行**：`style.py` 已贴上限，工作台样式另立 `style_workbench.py`，
  拼装接在 `STYLE_NAV_CSS` 之后；已登记 §12.2。类名与 id 不动（转义守卫钉住
  `.note` 形状）；审批两档、后端、schema 一律不碰。
- **走查顺手收两处**：空路由 / 引擎容器初始无内容时 `:empty` 隐藏，不留空
  底色条；路由外层容器去掉 `.note`，不与内层提示条叠两层底色。
- **测试**：`test_web_usability_js.py::TestWorkbenchVisualScope` 守样式块进
  拼装、包裹层 / 标签类在位、每条规则都带 `.wb-view` 前缀（裸 `.note` /
  `.btn.primary` 覆盖会染到别的视图）。
- **浏览器复验**：全量回归后走隔离库有头走查；`W1`–`W4` 真机引擎路径仍全 ⬜，
  真源只在 `docs/真机验收矩阵.md` 表 3。

---

## 上一版（V1.125 已完成 · 2026-11-17）

**项目备注折进上下文 —— 选中项目的备注作为站立偏好随每轮带进引擎背景一行
（无 schema 变更、无新文件）。**

- **接缝**：`workbench_context.build_context` 加 `project_note` 入参；端点
  `agent_start` 按偏好 `project_id`（缺选中关系时按目录兜底）从档案读出备注，
  压成单行后传入。服务层不碰档案表，档案读取入口仍在端点（一份能力一份实现）。
- **纪律同 21.5**：只读、单行（换行 / 多段空格压成一个空格）；备注排在今天的
  活前面；档案已删 / 仓储缺席 → 不带备注，不把「读不到」说成「没有备注」；
  注入失败不挡这一轮发不出去。
- **测试**：`test_workbench_context.py` 守备注前置 / 计数 / 空备注缺席；
  `test_agent_endpoints.py::TestProjectNoteInjection` 守选中档案备注带进
  `context` 与 `context_note`、档案缺失不带备注。
- **浏览器复验**：随全量回归后做隔离库走查；`W1`–`W4` 真机引擎路径仍全 ⬜，
  真源只在 `docs/真机验收矩阵.md` 表 3。

---

## 上一版（V1.122 已完成 · 2026-11-16）

**规则层平行问候入口 —— 封闭招呼集合离线也回问候答复，不再落澄清卡
（纯服务层；无 schema 变更、无新文件）。**

- **起因**：V1.120 表达层回退模板只在能力通道被接管、模型给不出措辞时
  生效；纯规则层「你好 / 在吗」走不到问候渲染，落到澄清卡（速读 #18）。
  飞书侧有关键词菜单入口，Web / 终端此前没有平行入口。
- **封闭集**：你好 / 您好 / 哈喽 / 嗨 / 在吗 / 在不在 / 你好吗 / hi / hello，
  只认招呼词 + 语气词 / 标点变体；夹带正事（「你好，下周二交周报」）不接，
  记事仍归写入口。除招呼外的能力话题，离线仍闭嘴不猜。
- **不动**：写入口、审批闸门、表达层事实卡片契约；`phrase` 协议不变。
- **测试**：`test_capability_route.py` 规则层招呼组（离线回问候、不白问
  模型、夹带正事不接、非招呼仍闭嘴）；`test_clarify_gate.py` 招呼不再进
  澄清卡，澄清选项断言改用「收到」。
- **浏览器复验**：隔离库 8811 有头走查 15 视图，0 pageerror / 0 console
  error / 0 坏响应；规则层「你好 / 在吗？」回问候答复，夹带正事正常记事；
  DOM 消息流确认 `kind-answer` 问候气泡上屏。8770 真机 DeepSeek 回模型
  措辞，不含授权项目名。`W1`–`W4` 真机引擎路径仍全 ⬜，
  真源只在 `docs/真机验收矩阵.md` 表 3。

---

## 上一版（V1.121 已完成 · 2026-11-16）

**「更多」下拉三层分组 —— 我的事 / 执行与通道 / 系统（纯前端；无 schema 变更、无新文件）。**

- **组织轴**：FreeAgent 是「事务脉络 + 授权目录」，不是通用助手的「会话归档
  + 项目分组」。下拉 11 项平铺时三类混在一起，按用途分段：
  - 我的事：已结束 / 角色 / 画像 / 关系 / 承诺 / 待处理 / 催办
  - 执行与通道：编程 / 后台服务 / 飞书通道
  - 系统：设置
- **不动的**：一级入口（对话 / 工作台 / 今天 / 事务 / 更多）、`data-view`
  接线、聚合角标、刷新动作按钮。分组标题不是视图，不带 `data-view`。
- **不引入**：项目分组 / 专家人格 / 技能插件市场 —— 授权目录是安全边界，
  不当聊天归档；与本地零依赖、写闸门优先基线冲突，属未拍板项。
- **测试**：`test_web_nav.py` 补分组归属守卫（标题齐全、每组集合正确、
  并集等于下拉全集）；浮层截取按 div 配平，适配嵌套分组标题。
- **浏览器复验**：隔离库 8811 有头走查 15 视图，0 pageerror / 0 控制台 error /
  0 坏响应；线下浮层三组归属与守卫一致。`W1`–`W4` 真机引擎路径仍全 ⬜，
  真源只在 `docs/真机验收矩阵.md` 表 3。

---

## 上一版（V1.120 已完成 · 2026-11-16）

**表达层接缝 —— 事实卡片由规则算死，模型只在卡片内组织措辞，给不出回退模板
（`LLMProvider.phrase`；无 schema 变更、无新文件）。**

- **起因**：问候回复全程规则套模板，且曾把授权项目清单甩进招呼；链路里 LLM
  只做闭集选路 / 抽取，没有自然语言表达层。这一刀补「事实 → 措辞」，不是
  开放自由答题。
- **接缝**：`phrase(facts, *, tone) -> str | None`。`facts` 是规则算出的条目，
  模型只润色；名称 / 数字 / 时间原样保留，facts 之外不许补。离线 / 失败 / 空串
  返回 `None`，上层回退模板。
- **两种实现**：`RuleBasedProvider.phrase` 恒 `None`；`DeepSeekProvider` 约束
  提示词 + 代码收口（去围栏 / 引号、压空白、200 字上限）；失败降级规则层，
  最后才落既有模板。
- **第一处接入：问候**。事实卡片只有「记事」「今天清单」两条，不带授权项目；
  模型措辞优先，回退「你好！说一件事我就记下来；问「今天」我给你清单。」
  写入口与审批闸门不动，表达层不触发写入。其余视图 / 清单转述未接，留下一刀。
- **测试**：`test_llm.py`（规则层 `None`）、`test_deepseek.py`（正常 / 空事实
  不调用 / 空输出降级 / 去围栏引号）、`test_capability_route.py`（措辞优先、
  卡片不含项目名、`None` 回退模板）。
- **浏览器复验**：8770 真机 DeepSeek 发「你好」，回包为模型措辞、不含项目名；
  隔离库规则层 8811 走查 15 视图，0 pageerror / 0 console error / 0 坏响应。
  **已知边界**：纯规则层寒暄不落问候模板，落到澄清卡（速读 #18）；
  `W1`–`W4` 真机引擎路径仍全 ⬜，真源只在
  `docs/真机验收矩阵.md` 表 3。

---

## 上一版（V1.119 已完成 · 2026-11-16）

**导航信息架构收敛（方案 B）—— 一级只留日常高频入口，其余视图收进「更多」下拉
（无 schema 变更；新增 `web/js_nav.py`、`web/style_nav.py`）。**

- **一级入口**：对话 / 工作台 / 今天 / 事务 / 更多；下拉收已结束、角色、画像、
  关系、承诺、待处理、催办、编程、后台服务、飞书通道、设置。视图一个不删，
  `data-view` 分发与服务层不动。
- **聚合角标**：承诺逾期 / 待处理 / 催办三计数聚合到「更多」按钮（`b-more`），
  避免藏在下拉里不展开就看不到；今天角标仍挂一级。
- **刷新是动作不是视图**：不挂 `data-view`，虚线幽灵按钮弱化，不跟标签同级抢视线。
- **hidden 复位（顺带修的真 bug）**：`.badge` 的 `inline-block` / `.menu-pop` 的
  `flex` 压过 UA 默认 `[hidden]{display:none}`，零计数角标露成空胶囊、下拉首屏
  也可能露出；补全局 `[hidden]{display:none !important}`。
- **拆块守 250 行**：`js_boot` / `style` 均已贴上限，下拉开合 / 选中态 / 菜单
  清单拆 `js_nav.py`，导航样式拆 `style_nav.py`，已登记 §12.2。
- **测试**：新增 `tests/test_web_nav.py`（一级 / 下拉清单、聚合角标、刷新无视图
  绑定、hidden 复位、模块进拼装）。
- **浏览器复验**：隔离库 8811，15 视图经下拉全部可达，0 pageerror / 0 console
  error / 0 坏响应。

---

## 上一版（V1.118 已完成 · 2026-11-16）

**Web 易用性向「豆包」首屏习惯靠拢 —— 规则层状态首屏可见 + 直达设置 /
页脚不跨视图泄漏 / Web 空态去 CLI 命令；顺手修视图切换竞态
（无 schema 变更、src 无新文件）。**

- **规则层降级首屏可见**：对话分支原先提前 `return`，头部状态栏只有别的
  视图刷得到。补 `refreshHeader()`；`/api/health` 判实现名
  `RuleBasedProvider` 时在对话流顶部放提示条 + 「去配置模型」按钮直达设置页。
  **判据用实现名不用 `key_configured`**：Ollama 这类本机服务不需要 Key，
  凭据判据为真，误报「未配置」会把本机用户引去配一个不存在的 Key。
- **页脚泄漏修复**：`#foot` 是跨视图持久元素，`load()` 切视图先清空；
  各视图自写自己的页脚。角色改名 / 合并 / 静置确实只能终端做，那条说明
  留在角色页自己脚下，属事实陈述，保留。
- **Web 空态去 CLI 语法**：今天 / 承诺 / 关系三处空态与页脚原先夹
  `/today-pin`、`/promise 记 … 关系=名字`、`/contact <名字>`；改成 Web
  可操作的出路（表单记下、对话里提到谁就挂账、承诺页结账）。终端命令表
  不动 —— 那是终端入口契约。服务端回复里残留的 CLI 语法（`endpoints_write`
  两处）留作下一刀。
- **切换竞态（走查暴露）**：连续快速切视图时旧 `load()` 的异步回包晚到，
  把页面画成旧视图（遍历完飞书页回对话，飞书回包盖掉对话壳、输入框消失）。
  `load()` 记自己的目标视图，回包后被新切换取代就放弃重绘；`loadChat`
  内部回包后核对 `view === "chat"`。
- **测试**：新增 `tests/test_web_usability_js.py`（静态守卫：头部刷新、
  横幅判据与入口、页脚清空、文案去 CLI、角色终端说明保留、切换作废）。
- **浏览器复验**：隔离库 8811，`ui_walkthrough.py` 走 15 视图，
  0 pageerror / 0 console error / 0 坏响应；对话发一句本地问题拿到规则层
  答复；横幅按钮点得到设置页。**仍未验**：`W1`–`W4` 真机引擎路径仍全 ⬜，
  真源只在 `docs/真机验收矩阵.md` 表 3。

---

## 上一版（V1.117 已完成 · 2026-11-16）

**工作台收尾 —— 清单原文转义 / 目录校验前置 / 回车提交 / 按钮置灰
（无 schema 变更、无新文件）。**

- **安全：清单原文不许进 `innerHTML`**。V1.116 的文件 / 命令清单注释写着
  「全部走 textContent」，实现却把引擎给的路径 / 命令塞进 `el()` 第三参
  （innerHTML）；目录名来自本机文件系统同理。这几处一律先过 `esc()`，
  静态守卫钉在 `tests/test_web_escape.py::TestWorkbenchRenderEscapes`。
- **目录校验唯一入口 `engine_host.resolve_workdir()`**：绝对 + 解析 + 是目录，
  宿主 `ensure`、目录浏览、`/api/agent/start` 三处共用一份。开跑前就挡下
  相对 / 不存在目录：不写 preferences、不起后台一轮失败（原先要等到后台线程
  `ensure` 才炸，人只看到一张失败卡）。
- **易用性**：输入框回车发出（Shift+回车换行）、提问框回车答复；
  「停止 / 交给它」按本轮状态置灰（没在跑停不掉，上一轮没完再发只吃到
  「还在跑」）。
- **顺手清静态文案的 markdown 星号**：前端不解析 markdown，``**…**`` 会
  原样印出星号；工作台 / 飞书通道 / 画像页三处残留去掉。守卫扫整个 web 包
  JS 上屏串，只排除 `js_delegate_live`（`mdish` 有意解析 markdown 子集）。
- **测试**：`test_engine_host.py::TestResolveWorkdir`（3）、
  `test_agent_endpoints.py` 加 2 条前置拒绝、`test_web_escape.py` 加工作台
  转义 6 条 + 全 JS 星号守卫 1 条。
- **浏览器走查（本轮补）**：隔离库起服务，`tests/manual/ui_walkthrough.py`
  点遍 15 个视图，0 pageerror、0 console error、0 坏响应；对话与工作台各发
  一句本地问题（规则层答复，不起真引擎）。走查清掉一处死调用：飞书页遗留的
  `GET /api/feishu/bridge`（启停早统一到 `/api/services`），连同未用的
  `ctl` 参数链一并删。**仍未验**：`W1`–`W4` 真机引擎路径仍全 ⬜，
  真源只在 `docs/真机验收矩阵.md` 表 3。
- **上手入口（补记）**：`tools\run_web.bat` 加解释器探测（PATH `python` →
  `py` → 标准安装目录 `Python3*`）与 3.11 版本闸，旧版本候选跳过、失败给安装指引；
  `pip install -e .` 三个命令入口已实测可脱离 `PYTHONPATH` 跑通（doctor 入口 exit 0）。
  未做桌面包，路线三另立项。

---

## 上一版（V1.116 已完成 · 2026-11-16）

**易用性与安全性向「豆包工作」靠拢 —— 事后可见 / 下一步 / 自动档的代价说清
（无 schema 变更、无新文件）。**

- **「碰过的文件」「它跑过的命令」**：`TurnView.touched_files` / `.commands`
  从 `steps` 过滤工具名得到（`edit`/`write`/`multiedit`/`patch`/`apply_patch`
  vs `bash`/`shell`/`pwsh`/`powershell`/`cmd`），**同一目标只列一次**，
  认不出目标的不列（空一行会被读成「它动了一个我不知道的文件」）。
- **为什么不落库**：步骤清单已经是这一轮全部工具事件的投影；再存一份就是
  第二个真源，两份必然有一份过期。
- **不加危险命令黑名单**：`executors` 的既有立场是「不要枚举危险命令」。
  这一层加的是**可见**（命令原文逐条摆出来）与**可反驳**（换档只对下一轮
  生效、失败后可「照这句话再来一次」——只填回输入框，不替人重发）。
- **空白要分开**：`files` / `commands` 返回**空列表**而非 `None`。
- **测试**：`tests/test_agent_turn.py::TestArtifactAndCommandFootprint`（4）、
  `tests/test_agent_endpoints.py::TestFootprint`（2）。
- **仍未验**：界面三块（文件 / 命令 / 再来一次）**没在浏览器里点过**；
  `W1`–`W4` 仍全 ⬜（真源只在 `docs/真机验收矩阵.md` 表 3）。

---

## 上一版（V1.115 已完成 · 2026-11-16）

**让工作台像「豆包工作」的那一半 —— 轮次落库 + 最近的活 / 步骤清单 /
上下文厚度（新增 `services/workbench_context.py`；`agent_turn.py` 大改；
schema v32 → v33 → v34）。**

V1.114 的骨架已对（引擎常驻、后台跑、前台能发话），差的正是**工作台**这一半，
按影响排序落三刀：① 关掉网页刚才那件事就没了 ② 进度只有一行「正在执行」、
看不见计划 ③ 每一轮从零开始、不知道你在忙什么。

- **① 落库 + 最近的活**：新表 `workbench_turns`（v33）+ `context_note` 列（v34），
  `GET /api/agent/turns?limit=` 列最近几轮，点开接着看。列表**不含正文** ——
  列出来是为了「找得到」，不是为了让首页变成聊天记录。
  `ok` 由 `status` 推（两个字段各自赋值必然漂移）；`recent()` 用 `rowid DESC`
  （**连点两次会落在同一秒**）；进程残留的 `running/waiting` 在构造时收口成
  `failed`（幂等）；**落库失败不阻断执行**，只在备注里说「关掉网页就看不到它了」。
- **② 步骤清单**：工具事件 → `StepView(name, target, status)`，三值
  `running`/`done`/`failed`；**形状识别复用 `tool_evidence`**（一份能力一份
  实现：进度卡认得出的，清单必须认得出）；同一 `(name,target)` 只更新状态不
  追加行；工具预算**只数 `kind == "tool"`**（`tool.started` / `part.updated`
  也带工具信息，一起计会让 40 次上限提前用完）。
- **③ 上下文厚度**：`workbench_context.build_context(app, query)` 把「今天的活 +
  未结清承诺 + 相关记忆」折成**一行**，跟在人话**后面**发出去
  （`{人话} [上下文] {背景}`），界面显示「背景：…」，开关默认开但看得见。
  三条纪律：**只读**（不调 `today.view()` —— 它会顺延并在 `task_records`
  留记录，改用 `scheduled_for()`）、**单行**（含换行的 prompt 会被 opencode
  当复杂任务升级）、**读不出来 ≠ 没有**（`degraded` 不许报成「今天没事」）。
- **测试**：`tests/test_workbench_history.py`（9）、`tests/test_workbench_context.py`（8）、
  `tests/test_agent_turn.py` 加 `TestSteps`（6）、`tests/test_agent_endpoints.py`
  加 `TestTurnsList`（3）与 `TestContextInjection`（3）。全量见「跑测试」那节。
- **真机未验（不是「应该没问题」）**：工作台列表 / 步骤清单 / 注入开关**没在浏览器里点过**，
  引擎真机路径（真起 `opencode.cmd`）也未跑；`W1`–`W4` 仍全 ⬜，真源只在
  `docs/真机验收矩阵.md` 表 3。

### V1.115 踩到的坑

1. **`el(tag, cls, html)` 的第三参是 innerHTML**：动态内容一律 `esc()`；
   `el("span", null, "...")` 合法（`if (cls)` 有守卫），但**静态文案**才能这么写。
2. **`wbSend` 曾经传 `{engine: {}}`**，界面画出「引擎：undefined」。现在 `wbPaint`
   只在 `st.engine` 存在时画引擎行，历史回看传 `{turn: …}`。
3. **看历史时必须停轮询**（`wbHistId` 闸）：1–2 秒一次的轮询会把历史画面换成
   最新一轮，人以为「点不动」。
4. **schema 版本号硬断言有两处**（`tests/test_daily.py`、`tests/test_question_pending.py`），
   加一版就要改两处 + 各补一行注释 —— 忘一处会红在「看起来无关」的文件里。
5. **测试替身 `_app()` 没有 `today`/`promise`/`memory`**：`agent_start` 现在会读
   上下文，于是**每个 start 测试都在测「读不出来」那条路**。想验「带上了什么」
   就得在替身上挂那三个服务。

---

## 上一版（V1.114 已完成 · 2026-11-15）

**把 OpenCode 变成常驻底座 —— 引擎藏后台 / 前台选目录与模型 / 自主判断
（新增第 21 章；新增 `services/engine_host.py`、`services/agent_router.py`、
`services/agent_turn.py`、`web/endpoints_agent.py`、
`web/endpoints_agent_workspace.py`、`web/js_workbench.py`、
`web/js_workbench_card.py`、`web/js_workbench_page.py`；无 schema 变更）。**

目标是「以 OpenCode 为底座的个人智能体」：① 引擎藏后台（用户无感，不再靠
命令起进程）② 前台直接选工作目录与 LLM 模型 ③ FreeAgent 自主判断这句话要
不要、怎么交给它。**三个必达其实是一件事** —— 前提是引擎得先是**常驻对象**，
而不是某条命令的副产物（改造前每次派发起一个新进程，实测 6–7 秒、用完即弃，
于是没有「在不在」、没有「会话」、接不上上一句）。

- **复用判据 = 同目录 + 同授权档 + 还答话**。模型 / 工作模式 / 推理档**不算**
  指纹：V2 里它们是会话级设置，换模型不该让服务重起 6 秒。授权档之所以算，
  因为它是**项目级覆盖层** —— 同一个服务上换档，旧规则继续生效，而「界面显示
  自动、实际仍在逐次问」是最难查的一类不一致。
- **授权两档，默认朝拒绝倒**：`ask`（逐次弹卡）/ `auto_dir`（只把 `edit`、
  `write`、`multiedit`、`apply_patch`、`patch`、`bash`、`shell` 从 ask 翻
  成 allow）。`external_directory` 与 `task`（子代理，**动作名就是 `task`，
  不是 subagent**）仍 `deny`，`webfetch`/`websearch` 仍 `ask`，兜底 `*: ask`
  不动 —— 这一档是「在你选的那个目录里放手干」，不是关闸门。
- **自动档下若引擎仍来问一次**：那是覆盖层没生效。处置是**拒绝 + 留备注**，
  不是顺手放行；失效方向只能往收紧走。
- **判断层是纯函数**（`agent_router.decide`）：含路径 / 动作词 / 「你来帮我做」
  → 交；只有目标物或英文动作词 → **弱证据也交，但理由里要说清它是弱证据**；
  以 `/` 开头、问自己记录、没目录、引擎不可用 → 不交。**宁可漏判**（代价是
  再说一遍）**不可错判**（代价是动了不该动的文件）。结论与依据一起回给界面，
  另给一个「仍交给引擎」的按钮。
- **一轮执行不是新的 agent 循环**：模型与工具都在 opencode 那侧。这一层只做
  「起转 / 读事件 / 把卡摆给人 / 收口」。五态 `running`/`waiting`/`done`/
  `failed`/`stopped` —— `waiting` 必须与 `running` 分开（「卡住」与「在思考」
  长相一样）。收口三条（点停 / 工具 >40 / 总时长 >600s）**每条都要真的停下来**。
- **常驻必须自带收口**：`App.close()` 先 `engine.close()` 再关库。漏一次，
  用户项目里就留一份 `opencode.jsonc` 覆盖层 —— 他自己在那个目录跑 opencode
  会莫名被问授权，而且没人会想到是 FreeAgent 留的。
- **测试**：`tests/test_engine_host.py`（15）、`tests/test_agent_router.py`（9）、
  `tests/test_agent_turn.py`（20）、`tests/test_agent_endpoints.py`（13）。
  替身 `FakeServer` 记 `instances` 验复用，`ScriptedHost` + `FakeAdapter` 验
  事件流；**一律不用 `sleep`**（用 `wait()` / `wait_for_ask()` 的事件等待，
  `SLEEP_BUDGET` 不用登记）。端点那 13 条守的是形状：判为「不用动手」时
  **一轮都不许起**、`force` 必须能推翻规则且留痕、不认识的 POST 动作要报。
- **真机未验（不是「应该没问题」）**：工作台那四格记在
  `docs/真机验收矩阵.md` 表 3（`W1`–`W4` 全 ⬜）—— 卡不落 `pending_approvals`，
  是从事件流直接摆出来、答复直接回给常驻引擎，所以表 1/2 的 ✅ **不能顺带证明它**。

### 上一版（V1.114）踩到的坑

1. **`r"""` 里写 `\\"` 会原样进 JS**。`data.path.endsWith("\\")` 拼进去就是
   `endsWith("\")` —— 浏览器侧语法错。反斜杠一律用
   `String.fromCharCode(92)` 拼，**不要写字面量**。`test_web_js_syntax` 咬这个。
2. **250 行上限是按职责拆、不是按行数硬切**：`js_workbench` 拆成
   state（刷新链）/ card（卡与发送）/ page（装配与目录模型）；
   `endpoints_agent` 拆成「状态机这侧」与「目录 + 开一轮」。
   拆完**必须同步设计文档 §12.2**（逐字文件名），否则 `test_doc_contract` 反向扫描报红。
3. **`routes_write.py` 贴着上限**（本轮为三个新端点差点顶破，最后做成
   `endpoints_agent.agent_post` 一张派发表 + 两行分支）。再往里加东西前，
   先想「是不是该换一处放」。
4. **`test_prompt_payload` / `test_reset` / `test_svcs` 的三条真机测试**在
   `PATH` 里缺东西时会**报红而不是跳过**：缺 `opencode` 时 `needs_opencode`
   认不出；缺 `powershell.exe`（`svcs.py` 用 `["powershell", …]` 扫进程）时
   直接 `WinError 2`。跑测试前把这两处补进 `PATH` 即可全绿：

   ```powershell
   $env:PATH = "C:\Windows\System32\WindowsPowerShell\v1.0;C:\Users\fanli\AppData\Roaming\npm;" + $env:PATH
   ```

   这不是本轮改动引入的，但「环境类红点」与「真坏了」必须一眼分得开。

---

## 上一版（V1.113 已完成 · 2026-11-04）

**同一板不记两遍 —— 决策日志的幂等与合并边界
（7.6.6 新增规范；`services/decision.py` 改返回 `RecordResult`；无 schema 变更）。**

V1.112 的收尾自查发现：**连按两次 `/weigh 1`，日志里出现两条逐字相同的记录。**
那**不是两次拍板**，是**一次拍板被记了两遍** —— 对峙时会对着质两次同一件事，
而人看到的却是「我拍过两板」。**噪音不是历史。**

- **判据是「逐字相同」**（冻结清单全文 + 选中的序号），**不是「同一组任务」**：
  **顺序是板的一部分**（主人选的是「第 1 个」），顺序变了就是板的样子变了 ——
  宁可多记一条，也不把两次不同的思考合并成一条。
- **合并有边界**：换了选择 = **另一板**（两条都留，那是真实历史）；
  跨天 = **另一次想**（另记一条，且**对峙日按各自拍板日 + 7 天算** ——
  合成一条会让后来的那次**提前对质**）。
- **只跟「今天最近的那条」比，不跟历史里任何一条比**：「甲 → 乙 → 甲」是真实的
  三次摇摆（两次甲一次乙），跟最久远那条甲合并会把「**最后落在甲**」弄丢。
- **回执必须把两种情况说成不同的话**：`record()` 改返回 `RecordResult(is_new=...)`。
  两者都说成「记下了」就是把事实说错 —— 后者**库里并没有多出一行**。
- **顺手修掉两处同刻定序违规**：`DecisionLogRepo.list_all()` / `due_on()` 原用
  `id ASC` 定序，而 `id` 是 `new_id()` 的随机 hex —— 同刻用它定序等于**随机排序**。
  改用 `rowid`（与 `tasks` / `contacts` / `promises` 同一条先例）。
  另：`latest_on()` 用 `substr(decided_at, 1, 10)` 取**本地日历日**，
  不用 SQLite 的 `date()`（它对带时区偏移的串先折 UTC，日期可能整体挪一天，
  而 `confront_on` 是按本地日 + 7 天算的）。
- **测试**：`tests/test_decision.py` 14 → **21 条**；`tests/test_decision_mutants.py`
  8 → **12 个探针**。
- **顺手把探针 harness 的一处弱点补上**：`expect`（期望红的那条测试）原来
  **只是注释、没被验证** —— 改坏一处可能连带让别的测试红，而探针的意义在于
  「它咬的正是我们以为的那条」。现在它是一条真断言（`FAILED`/`ERROR` 都认）。
- **加固当场抓到一个真问题**：第一个探针的 `expect` 指向
  `test_record_freezes_the_facets`，而**那条测试根本没红** —— 它查的是
  「**存进去的那份**」，变异发生在 `confront()` 里「**取出来的那份**」。
  **存对了但取错了，同样是漂移**。于是补了 `test_confront_uses_the_frozen_facets`
  （对峙的当时那侧必须**逐字等于**冻结的那份），并把探针的变异代码修正成
  **真能跑**的形状（原来的 `f.text` 在 tuple 上取不到属性，直接 AttributeError ——
  那不叫「重算」，那叫「崩了」）。

---

## 上一版（V1.112 已完成 · 2026-11-04）

**决策日志 —— 记住当时那一板，到期与现况对峙
（7.6 新增规范；新增 `services/decision.py` + `/decisions` + `/weigh <序号>`；
schema v31 → v32 新增 `decision_log` 表）。**

7.5 逼主人选，但**选完就忘了** —— 下次同一件事系统还是不知道他上次怎么选的，
于是每次都重新想。19.5.4 那句「AI 的产出质量 = 模型能力 × **上下文厚度**」
里的厚度攒不起来。这一刀把**当时那一板**记下来，让「现在」能跟「当时」对质。

- **核心是「冻结」**：19.5.4 要「带**时间戳的认知快照**……当时的判断依据」，
  **「当时的」是硬要求**。若对峙时用**今天**的库重算「当时的代价对比」，
  那份对比会随事实漂移（承诺兑现了、期限过了、事务被删了），于是变成
  「拿今天的事实跟今天的事实比」，**什么也照不出来**。所以代价文本与出处
  **逐字冻结在落库那一刻**。
- **`task_id` 在快照里，却不用它重建当时的文本** —— 两者分工：它只用于
  「现在」那一侧**查现况**（查不到就如实说「已经不在库里了」），**重建即漂移**。
- **为什么是 JSON 而不是多张表 / 多列**：一个快照里的选项数是 1~3
  （`MAX_OPTIONS`），facet 数每项 2~4 —— **形状不定**。拆成子表会让
  「冻结一组选项」变成三次写入，而**快照必须一次写成**（半写进去的快照不是快照）。
- **纪律三条**：① **只摆事实不评价** —— 对峙**不说「你选对了」也不说
  「你选错了」**（那是替人下结论，7.1 / 〇.8.4）；② **不读人的审批决策** ——
  本层**只写不读** `pending_approvals.decision`（11.9.11 第 ② 条），
  **不参与任何权重、排序或偏好推断**（那是筛选层，7.5.5）；③ **事实不足就
  如实说** —— 事务被删了报「已经不在库里了」，快照读不出来**就抛**
  （静默跳过 = 让人以为没拍过板，那是本仓最坏的失效形态：看起来在工作，
  其实少了一块）。
- **入口**：**给已有命令加参数，不新增命令**（同 V1.91 `/mode-build` 的纪律）。
  `/weigh` 只摆、`/weigh <序号>` 记板、`/decisions` 看到期对峙 + 历史索引。
  **序号指向「此刻重算出来的那组」**（同 `/pending N`），超界时说清当前有几位。
- **对峙日 = 拍板日 + 7 天**：当天就拉出来对峙，看到的是「事情还没发生」，
  对峙不出任何东西；不设上限则快照永远堆着，而 19.5.4 的原话是
  「**到期**自动拉出来」。
- **顺手修掉四处 markdown 残留**（`render.py` 里的 `**事实**` / `**时段**` /
  `**星期**` / `**从你的记录里现推的事实**`）—— 呈现层走 `textContent`，
  `**` 会**原样印出星号**。这是 V1.92（飞书 plain_text）与 V1.107（Web 面板）
  各踩过一次的同一个坑，**存量漏网**；已加一条守卫把整个呈现层扫一遍。
- **测试**：`tests/test_decision.py`（14 条）+ `tests/test_decision_mutants.py`
  （8 个变异探针，默认跳过，`--run-mutants` 才跑 —— 它**会改源码**，
  与 `test_doc_contract.py` 现读磁盘的纪律冲突）。
  探针**逐个确认过失败的是期望的那条测试**（V1.109 的坑：改字段默认值全绿，
  因为那行是显式传值的）。
- **19.5.4 的状态**：**内核已落地，落在判断层这一侧**（记的是「我在 A 与 B
  之间选了 A，因为代价对比是 X」）。19.5.4 原先设想的**个人投资形态**
  （为什么买、当时什么价）**尚未实现，不得当契约用**。

---

## 上一版（V1.111 已完成 · 2026-11-04）

**代价对照 —— 把该拍板的东西整理到值得拍板
（7.5 新增规范；新增 `services/weigh.py` + `/weigh`；无 schema 变更）。**

排序答「先看哪个」，**取舍**答「只能选一个时选哪个」—— 7.2 的权重表答不了后者
（它把 14 个信号加成一个标量排成一条队，而取舍发生在**两件事之间**）。
这一刀落成：**两件以上的事争同一天时，把各自的代价摆出来供主人拍板**。

- **落在 19.2 第 2 条上**：那条壁垒是「**不替用户做取舍**」，理由是
  「通用产品倾向黑箱打分，因为那样『显得更智能』」。这一刀是它的兑现 ——
  **把取舍摆出来而不是算出来**。
- **代价必须可对账**：代价项 =「事实 + 出处（库里那列）」。推论
  （"对方会失望"）**没有出处**，所以不写进代价。这是**前提不是细则** ——
  混进推论，它就从「帮你看清」变成「让你信我」，而 19.2 第 2 条反向生效：
  **武断 → 用户自我审查 → 数据变薄 → 价值归零**。
- **20.1.7 改写**：原建议「驳回某信号 → 微调权重」是**筛选层**（决定看不看得到），
  撞 11.9.11 第 ②③ 条，且与 〇.7 直接冲突（**降权就是在制造漏掉**）。
  分界线：**筛选 = 决定看不看得到 → 有损 → 不做；呈现 = 决定怎么摆 → 无损 → 做**。
  **权重表未动**（7.2 仍是硬编码规范性）。
- **测试逮到的两个真错误**（不是实现写错，是**设计想错**）：
  ① 最初把「代价性质不同」当成取舍的**门槛** —— 于是**最常见的取舍
  （同一天两件事）恰好被过滤掉**，剩下只有「性质不同」的那种才被摆出来，
  而那种本来就更该被看见。**取舍成立看互斥（争同一天），性质差异只决定「怎么摆」。**
  ② 最初用「有 `waiting_on`」判纯等候 —— 但 4.3 的不变式**双向**：
  未结束事务里 `state == BLOCKED` **必然**有 `waiting_on`。按 waiting_on 判
  等于把所有 BLOCKED 都排除（那是「卡在流程里」的事，**明明占着你今天**）。
  正确判据是 **`kind == WAIT`**（`kind` 是形状，`waiting_on` 是对象）。
- **争用单位是「日期」不是「精力档」**：档只有三档、每档 5～7 小时宽，
  两件事都在「早晨」并不构成争用 —— **档太粗，说不硬**。已过期的那天换成今天
  （过期的事占的是此刻，不占未来某天）；没排期没期限没承诺的事**不占任何一天**。
- **纪律三条**：① 代价带出处；② **事实不足就不给**（如实说「没有需要拍板的取舍」，
  不降格成「那就先做 X」）；③ **陈述句不用祈使句**（依据 `delegate.py`：
  指令类语句是自动决策的前一步）。
- **不读人的任何决策**：零输入依赖「人点过什么」，所以**不碰 11.9.11
  自我进化边界** —— 这也是它与原 20.1.7 的分界。
- **缺口**：本章逼主人选，但**选完就忘了**。缺的那一半是 **19.5.4 决策日志
  （候选 D）** —— 只有本章没有 D，它就是一次性烟花。**D 要记的不是「我选了 A」，
  而是「我在 A 与 B 之间选了 A，因为代价对比是 X」**。
- **测试**：`tests/test_weigh.py`（25 条）+ `tests/test_weigh_mutants.py`
  （7 个变异探针，默认跳过，`--run-mutants` 才跑 —— 它**会改源码**，
  与 `test_doc_contract.py` 现读磁盘的纪律冲突）。
  探针**第一步先确认自己真改变了行为**（V1.109 的坑）。最初三个探针全绿，
  查出两个原因：① 改的分支平时走不到；② **守卫本身太弱**（WAIT 事务没占天，
  去掉那道门也看不出差别）—— 后者是真 bug，已修测试。

---

## 上一版（V1.110 已完成 · 2026-11-03）

**委派的改动账 —— 把 `ok` 的语义从「没出坏消息」翻成「有证据说明动过」
（12.10.13 新增规范；新增 `services/engine_ledger.py`；无 schema 变更）。**

`ok=True` 的五条判据全是排除法（无会话错误 / 无未答提问 / 未叫停 / 未超步数 /
未超时长）。读一圈文件、回一段话、一个字没改，正常退出也算「完成」——
人据此验收，验收的是空气。

- **第一版读错了地方**：直接读 `opencode.db` 的 `part` 表，理由是「证据已经
  在盘上」。真机实测（v2.0.29）：同一会话 `part` **0 行**、`session_message`
  **333 行** —— V2 搬了家，而按老表名读的代码**不报错**，它会安静地给每次
  委派报「零改动」。**最坏的失效形态是「看起来在工作」。**
- **改走官方接口**：`GET /api/session/{sid}/message`。实测 431 次工具调用
  （`shell` 105 / `edit` 16 / `write` 10 / `read` 40 / `grep` 23），
  `state.input.filePath` 与 `state.metadata.exit` 都在。表名没有版本承诺。
- **「服务可能关了」不成立**：调用点在 `with make_server(...) as oc` 之内，
  `close()` 在退出 `with` 时才发生。
- **三值不许合并**：零改动**不是失败**（有些委派本就不该改文件）；读不到
  **不许**落成零改动（连不上 / 404 / 非 JSON / `data` 不是列表，四种都要说）。
- **分页：空页才是终点**。实测最后一页 0 条而游标仍非空；只看游标会多翻一页，
  页数上限小时还会把自己误标成「翻满」。翻满置 `truncated` → 「至少动了 N 个」。
- **刻意不做**：不用 `session.get` 的 `outcome` 当判据（会变成「两个完成」）；
  不读 `session.diff`（实测确实改过文件的会话返回空数组）。
- **不加字段**：账只进文案。第一版在 `DispatchOutcome` 加过 `ledger` 字段，
  一查**零消费者** —— 那与 `KnowledgeService` 是同一笔欠账（「建了不用」比
  没有更坏，它让文档看起来支持了一件没人拿到的事）。
- **测试**：`tests/test_engine_ledger.py`（27 条，假服务注入）。一条设计守卫：
  源码出现 `sqlite3` 就红。两条变异探针验证会咬（加回 import / 去掉空页终止）。
  另 8 个测试文件的假服务补了 `call` —— 症状是 75 条一起 `AttributeError`，
  读起来像大范围回归，实际只是替身缺一个方法。
- **全量**：3834 passed, 1 skipped（跳过的是符号链接，缺 Windows 开发者模式，
  与本轮无关）。

---

## 上一版（V1.109 已完成 · 2026-11-03）

**执行器探针补「凭据从哪来」（11.10.2 新增规范 4；新增
`services/executor_credentials.py`；无 schema 变更）。**

起因是 V1.107 那块里「另外三条待做」的 ①。`/agents` 答得出「装了没、什么版本、
能不能派、哪三桶」，答不出一问 —— **派出去之后它拿什么身份调模型**。而 401 / 402
时人的真实路径是翻配置、翻文档、翻环境变量，最后才想起来「它到底用哪个 key」。
本机实测：`auth.json` 里有 3 个提供方、环境里没有任何 `*_API_KEY` ——
此前 `/agents` 对这件事一个字都不说。

- **报观察到的事实，不报「能不能调通」**。存放位置是执行器的内部实现，会随版本
  改；写成「有凭据 → 能调通」，下一版就成谎话。「`auth.json` 里有 3 个提供方」
  这句话永远是真的（11.9.12：标注说证据，不说结论）。
- **`unknown` 与 `none` 分开**：文件在但读不懂 ≠ 没登录。合成一个，人会去重登，
  而问题在别处（`_read_providers` 用 `None` 与 `()` 区分这两件事）。
- **不判断两处都有时谁生效**：那是执行器的内部规则。照抄别人实测的「登录优先」
  会随它改版陈旧，而这条判断会被人拿去决定「该删哪一个」。
- **环境变量按形状扫（`*_API_KEY`，结尾匹配），不列白名单**：白名单陈旧 →
  报「没有凭据」而其实有 = **错判**（引去查错方向）；认不出别的命名只是漏判。
- **`untested` 整行不打印**：hermes 没有凭据探针，就不编一句「不知道」，
  更不许写成「两处都没看到」（那是替它断言）。
- **测试全注入**（`env` / `home`），`test_executor_probe.py` 加 autouse 夹具
  把凭据探针钉成 `untested` —— 否则那个文件会去读开发者的家目录。
- **五个变异探针全红**；第一个「默认值」探针**是绿的**，因为我改的是 dataclass
  字段默认值而 `_probe_executor` 显式传 `UNTESTED` —— **探针写错了也会绿**。
- 矩阵（第五份）**这轮不动**：没碰审批 / 停止 / 提问 / 超时。那条纪律要的就是
  这个效果 —— 不必每次都被牵动。

---

## 上一版（V1.108 已完成 · 2026-11-03）

**真机验收矩阵 —— 把「人点过没有」从十来处散文收成一张可被机器检查的表
（新增 `docs/真机验收矩阵.md` + `tests/test_acceptance_matrix.py`；
`decided_via` 增 `web`；无 schema 变更）。**

起因是 V1.107 那块里「另外三条待做」的 ②，形状借自 OpenHands 的
`docs/TESTING_MATRIX.md`（组合矩阵 + 复选框 + P0/P1/P2）。它的价值不在表格，
在**未验项永远是页面上一个可见的空框** —— 而不是文档里一句早已无人复读的话。

**盘点出来最难看的数字**：审批链 17 格，真机只点过 2 格；而那 2 格取自
**V1.90 话题化之前**的形态（卡直接回在私聊主消息流里，现在一律进话题，而回写
收据靠 `open_message_id`）。此前十来句话把它说得像「基本都验过了」。

- **三种空要分开**：⬜ 没点过 / ✖ 根本没实现 / ➖ 这道闸门不管这个通道。
  混着写，「没挡」会被排进验收计划 —— 排进去也只会验出「果然没挡」。
- **每格两栏**：「怎么验」（点到什么程度算过）+「为什么不能顺带证明」。
  后一栏挡的是那句最常见的合理化「旁边那格验过了，这格应该一样」，
  而这正是「允许/拒绝验过 ≠ 停止按钮验过」的教训。
- **顺手修的真缺陷**：网页三条写路径此前 `decided_via` 留空串（= 未记），
  于是「网页上批的」与「历史遗留、不知道从哪来」在审计视图里同形 ——
  一条通道在治理信号里整体消失。已补 `web`（允许/拒绝）与 `stop`（叫停）。
- **顺手发现的**：`DirectoryAccess`（项目级目录审批，2026-09-29 真机验过）
  **已无生产调用点**，其 ✅ 收进矩阵附录并写明「不能给委派链作证」；
  它的「非白名单人点」是 **✖ 未实现**，不是待验。
- **守卫会咬**：五个变异探针（⬜ 改 ✅ 不补证据 / 删一格 / 改错摘要 /
  去掉证据日期 / 删空「为什么」栏）**全红**。
- 该文件（矩阵）上限不重要，但**编号只增不重排** —— 删格子要来测试里划掉。

---

## 上一版（V1.107 已完成 · 2026-11-03）

**委派面板读不到之后不许静默停 —— 有限退避 / 停下来要说出来 / 回前台立刻探
一次（纯前端；无 schema 变更；`js_delegate_live_poll` 重写了轮询那一半）。**

起因是用户问 `D:\GitHub\OpenHands` 有没有值得借鉴的。那个目录里现在放的是
**Agent Canvas**（驱动 OpenCode / Claude Code / Codex 的 TS 控制台），与本仓
「治理层 + 执行器」的座位最接近，但代码一行都抄不了（React 生态 vs 零依赖）。
真正值得抄的是它 `usePageVisibilityReconnect` 的那句判断：**隐藏期间被冻结的
计时器，回到前台会补发一串陈旧重试**。

- **顺着它回头看出一个真 bug**：本仓轮询的失败回调是空的 —— 一轮读不到就
  **不再装下一次**，兜底写在注释里「用户切一次页签自然就重来了」。而正盯着
  面板等授权的人不会切页签，于是**授权还在库里、界面停在旧快照**，那张卡
  看着像没有了。这是 12.7.2「沉默 ≠ 同意」的镜像：**沉默 ≠ 没有**。
- **四条处置**（12.8 新增小节）：有限退避 5/10/30s（无限重试刷日志、一次就停
  丢闸门）/ 停下来必须说清「下面那张可能已经过期」并给「重试」按钮 /
  `visibilitychange`+`pageshow` 回前台立刻探一次且监听**只绑一次** /
  `poll_seconds=0` 时**不许唤醒**（那是服务端明说别轮询）。
- **实现上改了一处形态**：自装下一次从 `setInterval` 改成 **`setTimeout` 链** ——
  一条链只有一个在飞的请求（`setInterval` + 请求在飞时，两次响应会各自再装一次
  计时器）。`stopLivePoll` 因此两清 `clearTimeout` 与 `clearInterval`，不依赖
  「两者共用 id 池」这条**换宿主就静默失效**的细节。
- **守卫要验证它会咬**：`TestLivePollRecovery` 写完做了**变异测试** —— 删掉失败
  分支那句 `armLive(box, repaint, wait)`，`test_failure_arms_another_round`
  当场报红。与 V1.106 那条教训是同一句：**没验过会红的守卫等于没有守卫**。
- **本次只落这一条**。同一份对照里另外三条**未落、不是契约**：① 执行器探针
  缺「凭据从哪来」（OpenCode 的 `~/.local/share/opencode/auth.json` 登录态 vs
  `OPENCODE_API_KEY`，「**登录优先于环境变量**」是它实测出来的，与 402 那类
  故障直接相关）② 真机验收矩阵（11.8.1 表里「停止这条委派仍未验」那一行拖了
  很久，缺的是**一个可见的空框**而不是文档里一句话 —— **V1.108 已落**）
  ③ 写入口守卫测试（11.9.4「只做只读列举，写入/删除尚未接上这套闸门」的写法）。
- 该文件已经贴着 250 行上限（`TestWebPackageStaysSmall` 守着，具体数不写在这 —— 同一事实写两处必然烂一处）：再往里加东西前先想「是不是该换一处放」。

---

## 更早（V1.106 已完成 · 2026-11-03）

**测试自身的纪律 —— 从 openwork 借「一条半」（四条守卫 + 跑完报「几条没验证」；
无 `src` 改动、无 schema 变更；新增 `tests/test_assert_discipline.py`）。**

起因是用户问 `D:\GitHub\openwork` 有没有值得借鉴的。结论：**代码与工具链不抄，
方法抄一条半** —— 抄 `evals/scripts/spec-lint/lint.ts` 的 `honest` 规则；
「A skipped journey is incomplete coverage」只抄半句，因为本仓的 skip 全是
环境缺失（缺 node / opencode），禁掉等于要求装全 SDK。

- **四条守卫**（判据全是 AST 节点与字面量，不看措辞）：`skipif` 的 `reason`
  必须是字面量且说清缺什么 / 禁无条件 `skip`·`xfail` / 禁恒真断言 /
  `time.sleep` 按文件设上限，多的要登记「在等什么」。
- **`conftest.pytest_terminal_summary`** 跑完按 reason 分类打印「本次有 N 条
  没有验证」。**不改退出码**：缺依赖的机器本来就跑不了，判红只会让人关掉它。
- **反向验证踩到的坑（记牢）**：第一版四条规则扫 `ast.Call`，
  `@pytest.mark.skip`（**不带括号**）在 AST 里是 `ast.Attribute`，探针文件
  四条只红三条。**判「某东西被用了」要按节点类型写，不能按「看起来像调用」写。**
  加规则后必须造探针验证它真的会红，否则它可能是个永远绿的摆设。
- **`time.sleep` 计数必须走 AST**：`test_feishu.py` 里 `"time.sleep(30)"` 是
  载荷字符串，grep 会误算。
- **刻意不做**：「每个 test 必须有 assert」（本仓 14 个「只调用、断言不抛异常」
  的测试是合法语义，错判 14 个换拦不住 `assert True` 伪造）、禁 mock、
  「守卫自己不可被改」（openwork 靠的是不能被 PR 改的外部审查者；单人仓库里
  加自检只是形式主义）。
- **待办，不是已完成**：openwork 的**功能注册表**值得搬 —— 本仓 8 个行为开关
  散在 5 个文件，`BYPASS_APPROVAL` 能直接关掉审批闸门却无一处能一次答清。
  它要重构 `config.py`，单独一刀。

---

## 更早（V1.105 已完成 · 2026-10-29）

**在线检索可选腿 —— 改进清单第九刀（最后一条；代码 + 测试 + 四份文档，
无 schema 变更；新增 `services/online_leg.py`）。**

九刀清单前八刀已落（V1.97 → V1.104）；本版落**第九刀**：本地检索只有
BM25 + tail 两条腿，`vector_unavailable_reason` 一直写着「要联网就破断网
可用」。这一刀不等于松口，它加的是**一条默认关闭的可选腿** —— 压它到最后
的正是那两条前提：断网可用是硬性质；没有留痕（V1.98）就不知道本地什么
时候真的不够。

- **默认关闭、没有默认端点**：`FREEAGENT_ONLINE_SEARCH_URL`（必含
  `{query}`）/ `_TIMEOUT` / `_MAX`。默认安装行为与本版之前**完全相同**；
  本仓不内置任何搜索服务（不选边、不代持 key、不留默认值）。
- **配错了要报，不能猜**：模板缺 `{query}` 就一条请求都不发，并说清
  「不知道该把查询放哪」—— 猜着拼的表现是「永远搜到不相干的东西」。
- **触发口径是可观察事实，不是感觉**：只在 `bm25_hits == 0` 时发出去。
  **降级时不发** —— 拿外部查询盖本地故障，等于把「本地坏了」显示成
  「本地没有」，那正是本仓反复避开的静默。
- **发出去就要说出来**：结果块写明「已发往 `<host>`（查询原文离开了本机）」
  —— 这是这条腿唯一真正的代价，必须在结果旁边可见。
- **单列不混排**：抬头「在线检索（外部材料，未核实）」，与本地条目分块。
  本地条目是「你记过的」，外部材料是「网上有个人这么说」，混起来就是把
  V1.104 刚分开的东西重新糊上。`reason` 原样呈现，不翻译成「未找到」。
- **只认结构化**（11.10.2 立场）：JSON + `results/items/...` 容器 +
  `title|url|snippet` 一族别名；认不出就说认不出，不把整页 HTML 当摘要。
- **留痕加 `online`**（接 3.7.7）：`hit_count` 仍**只数本地条目**（外部结果
  没有 id、不进 `retrieval_hits`），所以 `legs='online'` 的行可能同时有
  `hit_count > 0`。两个数回答不同问题，别相加。
- **刻意不做：不自动喂给模型**。只「摆到人眼前」。两条理由：外部文本进
  prompt 就是注入面（V1.93「引文是数据不是指令」只解决标记）；模型读到
  网上文字不是「查过本机」，会让 V1.104 的凭据档说谎。也不做自动重试 /
  换端点，也不因它松口「向量检索不做」（embedding 要每次检索都算）。
- **conftest 加了 autouse fixture 清那三个环境变量**：否则开发者本机配过
  之后，所有走 `/memory search` 的测试都会真的发请求 —— 与
  `FREEAGENT_HOME` 那条同一个理由：**要等真的配过才发作的坑**。
- 测试：`test_online_leg.py` +27、`test_memory.py::TestOnlineLeg` +8、
  `test_cli.py::TestOnlineLegCli` +8。

**九刀清单到此全部完成**（①`/ask` 接 core ②检索留痕 ③按相关度注入
④review 队列 ⑤框架层证据计数 ⑥冲突显影 ⑦简报协作象限 ⑧答复凭据档
⑨在线检索可选腿）。**下一件不由清单定** —— 由人按现状速读的边界重新排。

---

**答复的凭据档 —— 改进清单第八刀（代码 + 测试 + 四份文档，无 schema
变更；新增 `services/tool_evidence.py`）。**

九刀清单前七刀已落（V1.97 →V1.103）；本版落**第八刀**：`/ask` 把「它查过
没有」和「它说了什么」写在同一段话里，语气完全一样 —— 用户没法知道
「`attempt_state` 判断重派」这句是读了代码才说的，还是凭训练数据编的。

- **三档判据（全部可观察，不看口吻）**：确定 = 至少一个文件类工具的目标
  在本机存在；推理 = 调过工具但**没有一个目标可核**（没带目标 / 通配模式 /
  路径不存在）；猜想 = **一次工具都没调用**。
- **「确定」不等于「对」**（最要紧的一条）：这一档说的是**凭据**不是结论。
  CLI 抬头必须写「说的是『查没查』，不是『对不对』」—— 少了这句，「确定」
  会被读成「这句话是对的」，那比不标注**更坏**（人会把读错文件的结论当
  已核事实）。
- **不做逐句归因**：判「哪句话属于哪一档」要语义判断；让模型自评又不可信
  —— 那只是把不确定换个语气（同 11.9 拒「自我进化」）。只给整段答复的档，
  并说清它粗（宁可粗，不要假）。
- **误判方向最坏的两处堵住**：`glob` 通配模式不当路径核（`**/*.py` 拿去
  `exists()` 永远假，老实查过会被标成「推理」）；`bash` 的 target 是命令
  不是路径，压根不算文件类。
- **形状识别只有一份**：`services/tool_evidence.py`，与
  `delegate.tool_event_label`（进度卡「正在执行 read：D:/a.py」）共用。
- **认不出不进凭据，但照样计预算**：`evidence` 与 `tool_calls` 刻意分开。
- 测试：`test_ask.py::TestEvidenceCollection` +4、
  `TestKnowledgeBoundary` +8、`test_cli.py::TestAskKnowledgeBoundary` +4。

**剩余一刀**：⑨ 在线检索可选腿 —— **已完成（V1.105）**。

---

**简报标明协作象限 —— 改进清单第七刀（代码 + 测试 + 四份文档，无 schema
变更；新增 `/brief <id>`）。**

九刀清单前六刀已落（V1.97 →V1.102）；本版落**第七刀**：简报是执行器**唯一**
收到的输入，而人此前既看不到它、也不知道自己「说够没说够」——「我该说多少」
没有依据。这一刀给那个判断补一个可以被指的事实。

- **轴换了（关键设计，别再改回去）**：通行的 Johari 问「知不知道这件事」，
  那条轴在这里没有区分度 —— 需求一写进简报，双方就都「知道」了，四象限
  会永远落在同一格。本节问「**谁手里有『这件事怎么做』的一手材料**」：
  人侧 = 已采纳方案 / 约定；机器侧 = 本任务错因 / 同项目踩坑 / 上次模型与工具。
- **判空复用产出那一段的同一个函数**（`failure_history_line` /
  `project_history_line` / `conventions_line`），**入参是原始数据**而不是
  算好的字符串：`/brief` 预览与派发各判一次，让调用方自己判空必然漂移，
  而漂移的方向必然是「预览说没方案、实发带上了方案」—— 比没有预览更坏。
- **进简报的只有两句，都是「缺席声明」**：暗知「本次没有已采纳方案和约定
  —— 上面是机器的执行记录，不是人定的规则」（专治「把机器的账当人定的
  规则」）；双盲「本次没有已采纳方案、约定和历史执行记录」。**讲授/共识
  一个字不加** —— 人的材料就在上面，同 12.10.7「没踩过坑不是要说的事」。
- **只标注不决策**：象限不进 `eligible_tasks` / `dispatch_blocker` /
  `already_dispatched`（测试扫源码守着）。
- **`/brief` 预览与实发逐字相同**：「六样零件各从哪来」只在
  `freeagent.delegate.assemble_brief` 一处（两处派发路径与预览共用），
  预览只排版；抬头必须先说「实发是单行」，否则人会以为执行器收到的是
  带缩进那份。
- 测试：`test_delegate.py::TestBriefQuadrant` +14、
  `test_cli.py::TestBriefPreview` +6。

**剩余两刀**（V1.103 时点的快照）：⑧ 回答层知识边界分档（确定 / 推理 /
猜想三分）⑨ 在线检索可选腿（排最后：受「断网可用」压制）。

---

**冲突显影 —— 改进清单第六刀（代码 + 测试 + 四份文档，无 schema 变更）。**

九刀清单前五刀已落（V1.97 `/ask` 注入、V1.98 检索留痕、V1.99 相关度选注入集、
V1.100 review 队列、V1.101 框架层证据计数）；本版落**第六刀**：V1.100 亲手
把「前缀同、结论反」的对子（「回复先给结论」vs「回复先给细节」，Jaccard
0.43）登记成已知漏判，并写明「那正是最像矛盾的一类」—— 本版把它接住。

- **口径**：`MemoryMinimalPair`（`left_diff` / `right_diff` / `overlap`）。
  判定 = 公共前缀 + 公共后缀 ≥ **4 字**（`MINIMAL_DIFF_MIN_SHARED`）且占
  较短那条 ≥ **60%**（`MINIMAL_DIFF_MIN_OVERLAP`）；**与「内容相近」组互斥**
  （同一对摆两遍、贴两个标签，读的人会以为有两件事要处理）。
- **为什么是首尾不是最长公共子串**：「先把方案给我看」差异在**中段**，最长
  公共子串只有「给我看」3 字（够不上门槛），首 2 + 尾 3 = 5 字正好说明
  「同一个框架换了一个词」；且首尾是 O(n)，LCS 是 O(n·m)。`_shared_affixes`。
- **实测推翻了两个口算**（写文档前先跑数据）：以为「回复先给结论 + 再给细节」
  是 Jaccard 0.45，实际 **0.62**；「周三/周五」实际 **0.60** —— 这两类早被
  相近组接住。两组不是两个档位，是**两个视角**：Jaccard 抓「整体像」，
  首尾抓「框架像」。
- **三条纪律**：① 仍**不说「矛盾」**（改口 / 两件并列的真事 / 一条是特例，
  字面同形，分诊留给人）② 门槛 4 字（3 字能套住「我不吃 X」，只消耗信誉）
  ③ 摆「差在哪」，一边为空就说「右边多出「…」」（写成「↔」会读成换说法）。
- **降级语义收窄（契约变更）**：留痕/确认记录读不出来时只降级「好久没动静」，
  **两组字面关系照摆** —— 它们只看正文，与留痕无关；`degraded_reason`
  不再意味着整张报告为空。
- 测试：`test_memory.py::TestMinimalPairs` +13、`test_cli.py::
  TestMemoryMinimalPairs` +5。

**剩余三刀**（V1.102 时点的快照）：⑦ 委派简报象限标注 ⑧ 回答层知识边界
分档 ⑨ 在线检索可选腿（排最后：受「断网可用」压制，且要等留痕攒够才知道
本地什么时候真的不够）。

---

**框架层带证据计数 —— 改进清单第五刀（代码 + 测试 + 四份文档，无 schema
变更；`serialize.py` 拆出 `serialize_model.py`）。**

九刀清单前四刀已落（V1.97 `/ask` 注入、V1.98 检索留痕、V1.99 相关度选注入集、
V1.100 review 队列）；本版落**第五刀**：V1.96 立下「没有计数的规律不可
反驳」，而框架层（从记录现推规律的层，11.17 是完整形态）是唯一还说不出
计数的规律的地方 —— `/model` 输出「完成 37 件」下面是 24 行小时分布，
「集中在 21-23 点」这句话从来不出现在输出里，看的人得自己数。

- **口径**：`PersonalModelView` 加 `hour_peak`（起、止、计数）与
  `dow_peak`（weekday、计数），判定不出为 `None`。相邻（差 1）且各自
  ≥2 次的小时并成段（**孤例不定义段的边界**），取计数最高段；段内 ≥3 次
  且占全部 ≥ 三分之一才成峰；同数并列取起始早。星期离散，取单日 top。
- **三条纪律**：判定不出**说清判定线**（不静默跳过）；凝练句是**统计
  描述不是评价**（「37 次里 13 次在 21-23 点」可以说，「你总在深夜干活」
  不可以说，测试逐词守着「建议 / 应该 / 不如」不出现在输出里）；**每条
  结论连计数与占比一起出**。
- **刻意不做**：多峰（凝练只要最强信号，原始分布已在上面全列）、环形
  合并（23 点与 0 点算不算一段没有天然答案，第一版不猜）、加表加列
  （11.17 纯只读，凝练句从既有分布现推）。
- **Web 同源**：`serialize.py` 已顶 250 行上限，`model_payload` 拆到
  `serialize_model.py`；payload 加 `hour_peak` / `dow_peak`（null =
  判定不出），`js_model.py` 节奏区同口径。
- 测试：`test_personal_model.py::TestCompletionPeak` +13、
  `TestPeakRendering` +6、`TestModelPayloadPeaks` +2。

**剩余四刀**（V1.101 时点的快照）：⑥ 冲突显影 ⑦ 委派简报象限标注
⑧ 回答层知识边界分档 ⑨ 在线检索可选腿。

---

**记忆 review 队列 —— 改进清单第四刀（代码 + 测试 + 四份文档，schema v31；
已提交 `b85e1ed`）。**

用户问「记忆与协作要做哪些改进」→ 九刀清单。前三刀（V1.97 `/ask` 注入、
V1.98 检索留痕、V1.99 相关度选注入集）已落；本版落**第四刀：review 队列** ——
V1.98 让「好久没用」成为事实，但**事实不产生动作**：没人会主动翻
`/memory stats`，而 core「小才有效」不会自己发生。

- **候选两条口径**：好久没动静（全时段 `max(created_at, 最后留痕, 最后确认)`
  早于窗口 —— 写下来/被用过/被确认过都算「动静」，与 3.7.7「窗口只管计数、
  时间不设窗」同一条纪律）；内容相近（`search_text` bigram 集合
  Jaccard ≥ 0.5，不限类别）。
- **只摆不动**：不自动删、不自动 consolidation（3.7.5 原样成立）。动作只有
  两个且都由人发起：`/memory review confirm <id> [<id>...]`（写
  `memory_reviews`，退出**一个窗口**的候选，窗口过后还会回来）与既有
  `/memory del`（不另造删的口子）。
- **确认不碰内容**：独立表，`updated_at` 不变 ——「我看过它」≠「我改了它」。
  新写入口照例显式 `commit()`（V1.98 教训，有跨重启测试守着）。
- **相近只说相近不说冲突**：阈值 0.5 刻意偏高（宁可漏判，V1.96 同一条纪律），
  漏判代价登记在案（「回复先给结论」vs「回复先给细节」0.43 会漏 —— 最像
  矛盾的一类，但分诊留给人）；百分比写进输出，可反驳。
- **与 3.7.2 划界**：那一节的「review 层不做」指自动 consolidation 的审计面；
  本队列人发起、无模型轮，候选全部由既有事实算出。
- **读不出来时不摆候选**（留痕/确认记录降级 → 只报原因、候选为空）——
  把故障显示成「没有候选」比崩溃更糟。V1.98「读不出要自报」的第三个落点。
- 测试：`test_memory.py::TestMemoryReview` +19、`test_cli.py::TestMemoryReview`
  +12、`test_question_pending.py` +2（v31 表存在 / v30 旧库升级）；
  版本断言 30 → 31 两处。

**剩余五刀**（按可做性排序）：⑤ 框架层带证据计数 ⑥ 冲突显影 ⑦ 委派简报
象限标注 ⑧ 回答层知识边界分档 ⑨ 在线检索可选腿（排最后：受「断网可用」
压制，且要等留痕攒够才知道本地什么时候真的不够）。

---

**注入集按相关度选 —— 改进清单第三刀（代码 + 测试 + 四份文档，无 schema
变更）。**

用户问「记忆与协作要做哪些改进」→ 九刀清单。前两刀 `/ask` 注入（V1.97）、
检索留痕（V1.98）已落；本版落**第三刀：注入选择 recency → relevance** ——
「常驻层要小才有效」，小的正确选法是相关，不是新。

- `core_block` 加 `query` 形参，**返回值升级为 `CoreBlock`**（frozen
  dataclass：`text` / `truncated` / `extra` / `leg` / `selected` /
  `degraded_reason`）；CLI `_cmd_ask` 把问题原文当查询词传入。
- **注入用 or 模式、检索用 and**：FTS5 空格分隔 = AND（`knowledge` 分词
  实测），自然语言问题按 and 永远匹配不上短记忆。同一 `_retrieve` 实现、
  同一排序，只是布尔式不同（`match` 形参；token 逐个加引号防关键字/特殊字符）。
- **三条边界**：不补填（选中 2 条就 2 条，不用近期凑满预算）；落空回落
  近期且取满预算（V1.97 行为保留）；相关度不可用回落且**必须自报**
  （`degraded_reason` → CLI 明说，不装作按相关度选的）。
- **选法可复核**：`leg`（`bm25` / `tail` / `recency`）+ 查询词都进留痕
  （3.7.7 的 `legs` 列），三种 leg 三种 CLI 说法。
- `_retrieve` 内部签名从 `scope: str | None` 改为**作用域集合**（注入要
  global + 多角色一起选）；公开 `retrieve()` 签名不变。
- 测试：`test_memory.py::TestRelevanceInjection` +11、
  `test_cli.py::TestAskRelevanceInjection` +4；V1.97 的 4 项 CLI 测试
  全部保留（子串兼容）。

---

**检索留痕落库 —— 改进清单第二刀（代码 + 测试 + 四份文档，schema v30；
并修一个「`/remember` 不落盘」的真 bug，V1.98）。**

- 新表 `retrieval_log` + `retrieval_hits`（v30，走 `_DOMAIN_STEPS` keyed-30，
  与 v19 同一种占位理由）；写入用 **SAVEPOINT** 保证日志行与命中行同生共死。
- 三个消费方显式传名（`/memory search` / `/ask` / `delegate_brief`），
  **不知道就传空串，不猜**；`inject` 只记真的注入了的（拼好没发出不计）。
- 新命令 `/memory stats [天数]`（默认 90）：逐条计数 + 「近 N 天没用过的」
  单列，**只摆事实不建议删**。
- **窗口只管计数，时间不设窗**（`last_event_at` 取全时段）—— 初版把窗口
  写在 JOIN 条件上，被 `test_used_outside_window_is_not_claimed_never_used`
  当场抓出来：那会把「三个月前用过」显示成「从未用过」。
- **读不出来的要自报**：`MemoryUsageReport.degraded_reason` 有值时 CLI 明说
  「计数不可信」。这条也是测试先抓出来再改的（表改名时统计命令直接崩；
  「early-return 空 rows」的修法会把故障伪装成「还没记过任何记忆」）。
- **真 bug（P0 性质）**：`MemoryService` / `KnowledgeService` 的写方法
  **从不 `commit()`**，`App.close()` 只关连接 = 回滚。「只 `/remember` 一句
  就退出」必现丢记忆；同会话里碰巧有过别的仓储写会把它**捎带**提交，
  所以症状是「有时记得住」—— 这类 bug 单跑测试永远绿。已补两个跨重启回归。
- 测试：`test_memory.py` +19、`test_cli.py::TestMemoryStats` +7；
  版本断言 29 → 30（`test_daily.py` / `test_question_pending.py`两处）。
  全量 3596 passed, 1 skipped。

---

**`/ask` 接入 core 记忆注入 —— 改进清单第一刀（代码 + 测试 + 四份文档，
无 schema 变更，V1.97）。**

- **起因**：用户问「FreeAgent 是一个以 OpenCode 为引擎的 Personal Agent，
  看看有没有值得改进的地方？」。核对引擎侧每天在本机写下什么
  （`~/.local/share/opencode/opencode.db`：222 条会话、906 次 edit、
  1072 次 bash），发现**引擎侧几乎没被接上** —— 约束住了引擎，
  没用上引擎。据此排六条改进，本版落**第二条：引擎失败的结构化分类**。
- **真机实测（2026-10-28，OpenCode v2.0.29）**：引擎失败时给的是
  **结构化**的 —— `session.error` 带
  `{"type": "provider.quota", "status": 429, "message": "...", "response": {...}}`。
  而本仓此前用 `extract_error_message` **只挖 `message` 那一句人话**，
  把 `type` / `status` 全丢了。用户拿到的是一句**没有行动指向**的话
  （订阅？换模型？等？改需求？），而 `type` 恰恰是「下一步做什么」的
  **唯一依据**。
- **与 11.8 的分工（不重叠）**：11.8 判「**能不能重试**」（从记录形状推）；
  12.10.11 判「**为什么失败、下一步做什么**」（从引擎给的类型读）。
  配额错误在 11.8 眼里是「可重派」，但重派一百次还是 429 ——
  **「允许重派」不等于「重派有用」**。
- **三条纪律**：**不猜**（只依据引擎实际给出的 `type`/`status`；认不出就归
  `unknown` 并原样保留原话 —— 宁可不给建议，也不给错建议）；**建议必须
  可执行**（除「上游/网络抖动」外一律不说「重试」）；**已验证的与推断的
  分开**（`provider.quota` 是实测，`status` 映射是按 HTTP 语义推断，
  **推断项不得当契约用**）。
- **类别落列，不落文案**（schema **v29**，`task_records.error_kind`）：
  错因文本每次带不同细节，按整句聚类会把**同一类**错误聚成 N 条、每条
  计数都是 1 —— 「这类错我犯过 5 次」这句话根本说不出来。而从文本里正则
  解析类别等于把措辞变成契约（11.9.12 禁掉的那类事）。
  `/feedback` 新增「引擎错误的类别（按类别计次）」一段，与原「反复出现的
  错因（原样引用，不归类）」**并存**：原话给人核对，类别给人看模式。
- **顺带修掉的字段遗漏**：V1 真机形状用 `error.data.statusCode`
  （**不是** `status`）—— 该字段本仓**从未被程序读过**（402/403 只在测试
  fixture 里）。已一并认；`error.data.isRetryable` **刻意不读** ——
  那是引擎对重试的判断，而重试资格由 11.8 判，两处各有一个真源会漂。
- **真机验证的边界（诚实清单）**：本次探测确认「旧 session id 在新进程里
  被接受」（跨进程续接的 API 前提成立）与三处字段形状；但**额度耗尽导致
  模型一条都没跑起来**，所以「跨目录续接会不会串味」**尚未验证** ——
  本版**不落** P0（会话续接），等额度恢复后再验。
- **测试**：`test_engine_errors.py`（新，30 项）、`test_tool_gate.py`（+4）、
  `test_feedback.py::TestErrorKindAggregation`（+6）、
  `test_question_pending.py::TestMigration::test_error_kind_column_exists`（+1）；
  版本断言 28 → 29。**全量 3564 passed, 1 skipped。**
- 更早版本历史（V1.95 → V1.52）见设计文档头部变更块，本文件只记当前最新。

### 引擎侧待办（按可做性排序，不按重要性；与上面的九刀清单是两回事）

1. **P0 会话续接** —— 跨进程续接已验证，**跨目录边界待额度恢复后补验**
   （当时 `provider.quota` / 429 让模型一条没跑起来，两个对照组失败原因
   相同，不能用来判定）。这是引擎侧六条里唯一有明确解锁条件的一条。
2. **P1 引擎的账** —— token 走 `session.tokens` 已验证，「改了哪些文件」待验。
3. **并发派发 L1**（项目级租约 + 有界线程）—— V1.94 的租约只是前置，
   派发仍单线程串行。
4. **「停止」按钮真机验证** —— 见 `docs/真机验收矩阵.md` 的 `A7`–`A9`
   （三个通道全空）；允许/拒绝已点过，但**不能顺带证明**它。
5. **P3 项目脉络** —— `GET /api/session` 已有公开契约可走，不必读引擎 SQLite。

**刻意不做**（不是待办）：12.7.1.2 重启提示机制化、连续性个人模型自动
consolidation、hermes 执行器（判据 2 补验失败 → 停用）。

### 本机网络环境的坑（推代码时会踩）

`github.com` 经本机代理（`127.0.0.1:8678`，**沙箱自带、节点不可切换**）
**CONNECT 返 502**，而 `api.github.com` / `codeload` 通。根因是代理自己解析
`github.com` 时命中了不通的亚洲节点（20.205.243.166），**且放行是间歇性的**
（同一 IP 实测 5 次里 2 次成功，失败时带 `Retry-After: 1`）。
**绕过办法**：本地起一个 CONNECT 中继（`%TEMP%/fa-relay/relay.py`）把隧道目标
改写成可用 IP（140.82.121.4），隧道内仍是端到端 TLS、SNI 仍是 github.com；
**中继必须带重试**（上游约 40% 成功率，一次失败就放弃必然推不上去）。
用法：`git -c http.proxy=http://127.0.0.1:18770 push origin master`。

