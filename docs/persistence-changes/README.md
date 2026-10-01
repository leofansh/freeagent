# 持久化变更记录

数据库 schema 每次结构变更都在这里留一条。

## 为什么需要这份文档

`storage/db.py` 里的 `_MIGRATIONS` 只记录**从 v1 往后**的增量。
于是「v1 长什么样」没有任何地方写着 —— 而旧库出问题时要能推理，
就必须知道它缺哪些列。

这份文档记的是**当时为什么改**，不是「现在有哪些表」。后者看 `_SCHEMA` 就行。

## 约定

- 库级版本号用 SQLite 自带的 `PRAGMA user_version`，**不另建迁移表**
  （单文件库的最省事做法，也最不容易出错）
- 迁移**必须幂等**：库是用户的文件，不能假设版本号干净
- 列变更走显式 `ALTER TABLE`（`init_schema` 只建新表，**不会**给已存在的表加列）
- 编号只增不改

## 版本史

| 版本 | 变更 | 为什么 | 记录 |
| --- | --- | --- | --- |
| 1 | 基线：roles / tasks / task_roles / artifacts / task_records / task_dependencies + 索引 | 见 [historical-formats/v1.md](historical-formats/v1.md) | — |
| 2 | `tasks.project_path` | 委派要记项目绝对路径（白名单校验的输入） | `_MIGRATIONS[0]` |
| 3 | `tasks.delegate_chat_id` | 委派跑完要把结果推回**发起那个飞书会话** | `_MIGRATIONS[1]` |
| 4 | 新表 `pending_approvals` | 逐次授权的等待与答复**必须落盘** | `_MIGRATIONS[2]` |
| 5 | `pending_approvals.requested_by` | 「只有发起人能批」此前只写在文档里、**代码没实现** | `_MIGRATIONS[3]` |
| 6 | `tasks.reminder_rule` | 重复提醒需要**显式时区 + 墙钟时间**；没有它「每天八点」会随夏令时漂 | `_MIGRATIONS[4]` |
| 7 | `tasks.revision` | 读-改-写要能发现「我读到的已经不是最新的」；`updated_at` 是时间戳，同一秒内分不出先后 | `_MIGRATIONS[5]` |
| 8 | `role_knowledge` + FTS5 索引 | 角色脉络知识要**可检索**（3.6）。`Role.note` 是自由文本，能读不能搜 | `_DOMAIN_STEPS[0]` |

| 9 | `pending_approvals.kind` | 档道要分清「等 allow/deny」与「等文本」两条每循。**不能靠凭据前缀推**（隐式契约）| `_MIGRATIONS[7]` |
| 10 | `pending_approvals.answer_text` | 提问的答复是**文本**而不是 allow/reject 枚举 | `_MIGRATIONS[8]` |
| 11 | `pending_approvals.question_spec` | agent 一次能问两问，而原来只有一个 `answer_text` → **只回答了第一个** | `_MIGRATIONS[9]` |
| 12 | `tasks.delegate_requested_by` | 「只有发起人能批」的**输入**：tasks 只有 `delegate_chat_id`（会话）而没有「人」 | `_MIGRATIONS[10]` |

`SCHEMA_VERSION = 12`（`storage/db.py`）。

> **v8 是第一个「多语句」迁移。** `_MIGRATIONS` 一步只跑一条 `conn.execute`，
> 而 v8 要建一张表 + 一个索引 + 一个**虚拟表** + 三个触发器，塞不进一条语句。
> 硬拆成四步的后果是中间态「表建了触发器没建」，一次崩溃就留下不同步的索引。
> 所以另立 `_DOMAIN_STEPS`（键是**目标**版本），两种步骤共用 `migrate()` 的
> **同一个循环**与同一次版本号推进 —— 版本号仍**一步一版**，不会跳版。
> 详见 3.6 的「索引同步」。

## 存储约定

这几条不是某次变更，是**一直如此**的约定，改动它们需要走上面的流程。

- **`datetime` 存 ISO-8601 `TEXT`**，**不引入时区换算**。
  一律 `.isoformat()` / `fromisoformat()`。
  这么定是为了测试注入 `FrozenClock` 后能**精确比对字符串** ——
  换成带时区的存储，顺延与提醒的断言就得跟着处理时区。
- **`date` 存 `YYYY-MM-DD` `TEXT`**。
- **多值字段不存 JSON 数组**，用带外键的关联表
  （`task_roles` / `task_dependencies`）。
  这样「删角色时被引用则拒绝」由**数据库自己**保证，而不是靠应用层记得检查。
  代价是查角色下的事务要 join，收益是约束不会漏。
- `PRAGMA foreign_keys = ON`、`journal_mode = WAL`、`busy_timeout = 5000`。
- `check_same_thread=False`：Web 用 `ThreadingHTTPServer`，连接会在工作线程里用。
  「同一时刻只有一个线程用这个连接」由 `App.lock` 保证，**不是**靠 SQLite。

## 踩过的坑：版本号不推进

迁移循环里原本写成 `PRAGMA user_version = target`（目标版本），
而循环变量是**源版本** —— 于是版本号永远停在原地：

- 迁移 SQL 照跑（列确实加上了）
- 但 `user_version` 不推进，**每次启动都重跑一遍全部迁移**

**只靠「列在不在」断言会漏掉这个错** —— 因为列确实在那里。
所以现在断言的是 `user_version` 本身。

## 升级到新版本

用户从旧库启动时，`migrate()` 会自动按版本号逐级迁移，**无需手工操作**。

若迁移失败，库**保持原状**（每步都在独立事务里），修掉原因后重跑即可 ——
幂等性保证重跑是安全的。

## 记录一条新变更

1. 在 `_MIGRATIONS` 末尾追加 `（当前版本 → 新版本：<一句话>，<为什么>）`
2. `SCHEMA_VERSION` +1
3. 在上面的版本史里加一行
4. 若涉及**旧数据的语义变化**（不只是加列），在
   `historical-formats/` 下补一页，说明旧数据怎么解释

> **V1.16（2026-10-01）更正**：`pending_approvals.requested_by` 这一列**已落地、判定已实现且有测试**，但执行器喂进去的值是「白名单里排序第一的人」而不是委派的发起人 —— 所以「只有发起人能批」的实际效果与描述相反。
> 本表记录的是**当时的迁移事实**，所以不改写。
>
> **V1.17（2026-10-02）已修**：新增版本 **12** 补上了缺的入口 —— `tasks.delegate_requested_by`。当前状态见设计文档 11.8.1「取值错在哪」。
