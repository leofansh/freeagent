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
  - `config.json` 绝不写 API Key（Key 只进 `~/.freeagent/llm.env`）。
  - web 包单文件有 250 行上限（`TestWebPackageStaysSmall` 守着）。
- 提交信息格式：不用管——本仓由用户手动提交，agent 不主动 commit
  （用户明确要求时代劳，如 2026-10-21 的 V1.43–V1.50 一次提交）。
- 文档版本号纪律：设计文档头部「本次变更（V1.x）」记录最新变更，
  当前最新是 **V1.52**（催办草稿：`services/nudge.py` + `/nudge` 命令 +
  `WaitingOn.contact_id`（可选，让**等候**也挂到关系对象上）；补 V1.51 的
  Web 待处理视图端点（`/api/pending` `/api/promises` `/api/contacts`）与测试；
  V1.51 对外承诺与关系对象：`contacts` + `promises` 两表 + `/promise` 与
  `/contact` + `PROMISE_OVERDUE`（+45）信号；V1.50 待处理收集器、
  V1.49 连续性个人模型 `/model`、V1.48 捕获摩擦附件收下、
  V1.47 记忆 `memory` 表 + `/memory` 命令族）。

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

## 当前进行中的工作（V1.52 · 2026-10-21）

催办草稿 + V1.51 的 Web 视图补齐 —— 设计文档新增 **11.20 规范**。

- 断点定位：`OVERDUE_WAIT` 已能说「该催张总了」，`next_actions` 也能给
  「催一下 X」，`/draft` 能起骨架 —— 但用户仍要自己从零敲那条消息。而这件事
  最难的不是措辞，是**张冠李戴**（催错人、把上周的事催成这周的、把「我等你」
  写成「你欠我」）。
- 四条纪律（11.20.2）：**起草不发送且系统没有发送能力**（不是自律而是结构 ——
  `NudgeService` **不持有任何通道对象**；同 11.10.5「不可逆动作不该只靠一条
  纪律守着」）；**只显式触发绝不静默生成**；**只铺事实、正文留白**
  （第十章 `[TODO]` 延伸，「上次说好周四」那种绝不能编，用户会照着发）；
  **不代替用户结账**（承诺仍 `open`、**不落 `TaskRecord`**）。
- **两向语气相反故模板分岔**：`wait`（我等对方 → 问进度/提醒交付）vs
  `promise`（我欠对方 → 告知延期/给新时间）。混成一句 = 该道歉的事被写成
  质问。
- **候选与信号逐字对齐**：`follow_up_at <= now` 对齐 `OVERDUE_WAIT`，
  `promised_for <= now 且 open` 对齐 `PROMISE_OVERDUE`。刻意不新增判定 ——
  漂移症状是「今天视图说该催，`/nudge` 列不出来」。测试
  `TestAgreesWithSignals` 逐条比对两集合要求相等。
- **顺带补上 20.1.5 提的那另一半**：新增可选字段 `WaitingOn.contact_id`
  （在 JSON 列内，**不另加 schema 字段**，历史行 `.get` 兜住），
  `/wait <id> <等谁> 关系=名字` 把等候挂到关系上。于是 `/contact` 与
  `/api/contacts` 并排给出 `open_promises`（我欠对方）与 `open_waitings`
  （我在等对方）——**两数才是完整的账**。
- **修一处真实排序错位**（核查发现，非需求）：待处理收集器原为
  `ORDER BY created_at ASC, id ASC`，而 `id` 是 `new_id()` 随机 hex —— 同刻
  收下时顺序随机，于是「界面第 1 条」与「`/pending 1` 澄清出来的第 1 条」
  可能不是同一条。改 `rowid`（插入序），与 tasks 仓储同一先例；
  `contacts`/`promises` 三处一并统一。**同一个坑 V1.37 在 `task_records` 上
  踩过一次**（那次加 `seq` 列根治），见 `services/feedback.py`「同刻记录的
  顺序前提」。
- 测试：`tests/test_nudge.py`（35 项）+ `tests/test_web_pending.py`（19 项，
  补上 V1.51 三个新端点的测试 —— **它们此前面世时没测**，别再犯第三次）。

上一轮（V1.51）：对外承诺与关系对象 —— 设计文档新增 **11.19 规范**。

- 断点定位：9.1 的 `WaitingOn` 只记了「我等别人」，反向那半整个缺 ——
  **我答应别人**（19.5.3 原语 C）。三重代价：违约有社会代价（自己的截止日
  改了没人受损，答应别人的没有内部宽限期）；它很可能根本不在库里（只存在
  于对方脑子里）；关系不可检索（`who_or_what` 是自由文本）。
- 四条纪律（11.19.3）：**只显式触发绝不静默推断**（同 11.10.5，宁可漏不可
  擅自动手）；**承诺必须有兑现物**（空头承诺无处可查）；**过了日期不等于违约**
  （三个终态只由用户显式动作产生，延期不算违约）；**关系与脉络正交**。
- 数据模型：两张新表 `contacts` + `promises`（schema v19，走 `_DOMAIN_STEPS`
  keyed-19）。`task_id` **一对一**（一条承诺一个兑现物）；`contact_id` **不设
  外键**（人删了历史承诺不该消失）。
- 排序：`PROMISE_OVERDUE`（+45）——**高于** `DEADLINE_RISK`（+40），19.5.3 的
  理由。信号名里**没有「违约」**：只陈述事实「已逾期 N 天还没兑现」。
- 入口：`/promise`（记/列/keep/break/withdraw/postpone）+ `/contact`。
  关系与期限用**显式标记**（`关系=` / `期限=`）不用位置猜 ——「周三」这种
  既是日期又可能是人名的东西会被误判。
- **顺带收口两处错位**（核查发现，非需求）：
  ① 11.18.7 曾把「飞书里直接 `/pending`」列为不做，而 `channel.py:359` 早就
  放行 ——**文档说不做、代码做得到**。改为承认它能做（11.18.8），回话从
  「在终端 /pending」改成「在这里敲 /pending」。
  ② 11.10 执行器注册表**早已落地**（V1.19），四处汇总表仍写「代码 ⬜」——
  19.6 阶段 1 实际已完成，下一阶段不是从阶段 1 起步。
- 测试：`tests/test_promise.py`（27 项）+ `tests/test_pending.py` 增 3 项。

上一轮（V1.50）：待处理收集器（GTD 澄清环节）—— 设计文档新增 **11.18 规范**。

- 断点定位：GTD 五环 捕获 ✅(V1.48) → **澄清 ❌** → 组织 ✅ → 回顾 ✅ → 执行 ✅。
  V1.48 只修了「数据先进来」的一半：附件落盘即终点，没有队列、没有提醒、
  没有一键澄清成事务 —— 收集器变垃圾场是 GTD 经典死法，且失血是沉默的。
- 第一性原理：复利资产 = 数据厚度（19.5.4），厚度的两道关口是「进来」与
  「澄清」；收集器也是未来所有录入通道（语音/邮件/批量倾倒，19.6 阶段 4）
  的共同接缝（17.2.2 先做接缝）。
- 三条纪律（11.18.3）：**捕获不是事务**（不建事务契约不变）；**澄清是唯一
  建事务入口**且必须用户显式 `/pending N` 触发，走 `TaskService.create`
  唯一写入口；**不猜内容**（标题用真名，飞书图片退「图片（MM-DD HH:MM）」）。
- 数据模型：新表 `captured_items`（schema v18）——**刻意不做成 Task**：
  语义不同（未澄清 ≠ 待办）、排序会污染 /today、迁移纪律（只 CREATE TABLE）。
- 入口：`/pending` 列出（FIFO）/ `/pending N [标题]` 澄清 / `/pending drop N`
  忽略；`/today` 在待处理 >0 时追加一行提示（0 条不占位，默认参数保证既有
  输出逐字不变）；飞书 `_capture_unsupported` 两条分支都入队，入队失败
  回复说实话（不许静默降级）。
- **术语占座（已核实）**：`TaskState.INBOX`（待办）早已占用「inbox」，
  新概念**不能**叫 inbox，用「待处理收集器」——同 11.10.1「执行器」占座。
- **迁移索引坑**：`_MIGRATIONS[source-1]` 按源版本位置索引，v17 槽位原本
  是空的（memory 走 `_DOMAIN_STEPS` keyed-17），直接追加会让新条目在
  16→17 步执行、17→18 空跑。照 v8 先例补 `SELECT 1` 占位条目占住索引 15。
- 测试：`tests/test_pending.py`（服务 9 + 渲染 3 + 桥接入队 5）。

上一轮（V1.49）：连续性个人模型 `/model`（`services/personal_model.py`，
纯只读聚合 TaskRecord/Task，零新依赖；20.1.1「如果只能先做三件」之首）。
V1.48 捕获摩擦（11.9.10）：非文字消息收下而非丢弃，落盘 `attachments/` +
`.meta.json` 引用，**绝不建事务**（白名单 / @ / 不建事务三前提是契约）。
V1.47 记忆（3.7）：core 手写层 + episodic 复用 `task_records`，`/memory`
命令族 + `/remember`；接线死代码 `KnowledgeService`。
更早：V1.46 Plan 恢复不再静默（12.7.2「恢复时要说清」，`_resume_notice`
在下一条回复前说一次）；V1.45 安静窗口 `quiet_hours`（`QuietHours`）；
V1.44 周回顾 `/week`（只读五分区，复用 today 的顺延）；
V1.43 委派白名单候选三源并集（`services/delegate_browse.py` +
`GET /api/delegate/browse` + `POST allow_pick` + 配置 `delegate.browse_roots`）。

仍未做（重写版附录 B 的诚实清单）：12.7.1.2 重启提示机制化（刻意不做）、
连续性个人模型自动派生（V1.47 只做了**用户手写**的 core 层，自动 consolidation
仍刻意不做）、「停止」按钮真机验证、催办草稿（20.1.4，下一项候选）。

