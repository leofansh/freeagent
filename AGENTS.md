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
  当前最新是 **V1.50**（待处理收集器：`captured_items` 表 + `/pending` 一键澄清，
  补上 GTD「捕获 → 澄清」断点；V1.49 连续性个人模型 `/model`、V1.48 捕获摩擦
  附件收下、V1.47 记忆 `memory` 表 + `/memory` 命令族并接线 KnowledgeService）。

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

## 当前进行中的工作（V1.50 · 2026-10-21）

待处理收集器（GTD 澄清环节）—— 设计文档新增 **11.18 规范**。

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

