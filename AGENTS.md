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
  当前最新是 **V1.84**（飞书实测三摩擦点改善：today 空态报「排在未来」——
  `TodayView` 加 `future_count`/`earliest_future`，chat/render/Web 空态照报条数、
  不改 6.2 视图定义；建事务**默认安静记下**——删掉 `_create` 的「意图还没说清楚」
  自动追问；建事务**提及已登记关系提示可挂账**——`PromiseService.mentioned_contacts`
  只认已登记名、零误判、守 11.19「只提示不静默挂」）。更早版本（V1.83 → V1.52）
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

## 当前进行中的工作（V1.84 · 2026-10-25）

飞书实测三摩擦点改善 —— today 空态报「排在未来」+ 建事务默认安静记下 + 提及已登记关系提示可挂账。

- **断点定位**：飞书实机测试跑通五条链路（引导卡 / 自然语言建事务 / today / all / contact 关系对象），确认「暂不接微信」；用户拍板改善三摩擦点。
- **摩擦点 1（today 空态报「排在未来」）**：实测建一条排周四的事、回 /today 得「0 件事」——事务在库里（/all 看得到）却像没记上。`unscheduled_count` 已报「没排期」，**排在未来**是同构的静默。修：`TodayView` 加 `future_count` + `earliest_future`（`scheduled_for > today 且 open`）；`view()` 填充；`chat.py::_answer_today` 空态、`cli/render.py::render_today`、Web `serialize.py` + `js_core.py::renderList` 空态都报「还有 N 条排在后面（最早 X）」——**不改 6.2 视图定义**，只照报条数。
- **摩擦点 2（建事务默认安静记下）**：建事务回「意图还没说清楚」追问，快速记一笔被盘问。修：删掉 `cli/app.py::_create` 自动追问行（intent 可选项、缺了有 [TODO] 兜底）。**默认安静记下**。
- **摩擦点 3（提及已登记关系提示可挂账）**：实测「和张总确认合同」建了事务、/contact 张总 建了关系，但事务没挂——用户不知道能挂账。修：`PromiseService.mentioned_contacts(text)` **只认已登记**关系名（零误判，守 11.19「只显式触发绝不静默推断」）；`_create` 与 Web `create_task` 提到已登记关系加一行「提到 X，可挂关系账（/contact X）」——**只提示不静默挂**。
- **测试**：`test_unscheduled_visibility.py`（+`TestFutureIsCountedNotListed` 与 payload/chat future 断言）、`test_promise.py`（+`mentioned_contacts` 三测）、`test_cli.py`（+关系提示两测）、`test_web.py::TestTaskEndpoint`（+`relation_hint`）。全量 pytest 绿。
- 更早版本历史（V1.83 → V1.52）见设计文档头部变更块，本文件只记当前最新。

仍未做（重写版附录 B 的诚实清单）：12.7.1.2 重启提示机制化（刻意不做）、
连续性个人模型自动派生（V1.47 只做了**用户手写**的 core 层，自动 consolidation
仍刻意不做）、「停止」按钮真机验证。hermes 判据 2/4 补验已结（判据 2 = 失败，
处置为停用执行器）。

