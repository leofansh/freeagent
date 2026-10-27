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
  当前最新是 **V1.95**（委派带上历史：默认派发路径补 `DELEGATION_DISPATCHED`
  —— 执行期失败曾全被误判成「派发前被拒」永不重试；项目级踩坑史、
  已采纳方案、一次委派的账（`artifacts.model`/`tool_calls`，schema v27/v28
  **一列一版**——域通道脚本必须自身幂等）、人记的「约定」进简报 +
  `/accept` 验收后提示沉淀（只提示不代记））。更早版本（V1.94 → V1.52）
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

## 当前进行中的工作（V1.95 · 2026-10-28）

让委派带上历史 ——「Agent 之上的 Agent」从「管得住」补向「记得住」。

- **P0（先修）**：默认派发路径（执行期逐次授权）只占租约、不写
  `DELEGATION_DISPATCHED` → 执行期失败（模型 402、执行器崩溃）全被
  11.8 的形状判据误判成「派发前被拒」，**永不自动重试**。修：闸门通过后、
  占租约前补写，顺序与旧路径对齐（先记派出再查白名单的话
  「派发前被拒」类别就不存在了）。
- **① 项目级踩坑史**：`sibling_history` + `project_history_line`
  （同项目其它任务、只取执行期失败、默认三条/单条 160 字）进简报。
- **② 已采纳方案进简报**：`_accepted_plan_text` 取已采纳产物作
  `accepted_plan`（「你已经定下的方案，按它实施」，1500 字截断留标记，
  排在错因之前）。规划线与委派线由此接通。
- **③ 一次委派的账**：`artifacts.model`/`tool_calls`（schema **v27/v28**），
  `last_attempt_line` 读进重派简报。认执行器产出靠两列非空，不靠标题。
  **教训**：两条 ALTER 不能塞域通道（`executescript` 裸跑要求脚本自身幂等，
  `ADD COLUMN` 没有 IF NOT EXISTS，`test_rerun_is_idempotent` 当场抓住）——
  一列一版走单语句通道，先例是 v9/v10/v11。
- **④ 约定进简报 + 验收提示**：`MemoryService.conventions()`
  （global+本事务角色、类别过滤在 SQL）→ `conventions_line`
  （必须带「可能过期，以当前为准」定性，三条/120 字）进简报；
  终端 `/accept` 与 Web 验收在采纳执行器产出（`model` 非空）时提示
  「`/memory add 约定` 可沉淀」——**只提示不代记**（EXPLICIT_ONLY），
  用户自己的草稿不提示。
- **测试**：`test_delegate.py` +16（回归 2 / 项目历史 4 / 方案 4 / 约定 4 / 账 2）、
  `test_memory.py::TestConventions`（4）、`test_cli.py`（2）、
  `test_artifact_accept.py`（2）；版本断言 26 → 28。
- 更早版本历史（V1.94 → V1.52）见设计文档头部变更块，本文件只记当前最新。

仍未做（重写版附录 B 的诚实清单）：12.7.1.2 重启提示机制化（刻意不做）、
连续性个人模型自动派生（V1.47 只做了**用户手写**的 core 层，自动 consolidation
仍刻意不做）、「停止」按钮真机验证。hermes 判据 2/4 补验已结（判据 2 = 失败，
处置为停用执行器）。并发派发 L1（项目级租约 + 有界线程）：V1.94 的租约只是
前置，派发仍单线程串行。

