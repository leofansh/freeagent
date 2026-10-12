# 参考：Octop 实现级对照（D:\GitHub\Octop）

> 目的：把腾讯云开源 **Octop**（自托管多用户多 Agent 平台）的真实源码，与 FreeAgent 的「Agent 之上的 Agent」架构做实现级对照，确认二者同构/异构点，提炼可借鉴细节。
> 研究时间：2026-10-11。Octop 本地提交约 2026-10-09；FreeAgent 见 `src/freeagent`。

## 0. 一句话结论

**Octop 是 FreeAgent 核心论点（委派 OpenCode + 飞书承载人工授权）的一个「超集平台」实现**：它把这套机制作为 ACP 出站 Runner 的一个特性，外层再包了完整的多用户助手平台（Harness 运行时、RAG、远程桌面、浏览器、沙箱、插件市场、JWT 多用户）。二者在「**委派 OpenCode 执行 + 把 OpenCode 原生权限提示 relay 到飞书**」这一条线上**高度同构**；差异主要在治理层落在哪、以及平台厚度。

---

## 1. Octop 核心架构（源码确认）

### 1.1 两个运行面
- **管控/运行时面 = Octop Harness**：自带 agent 运行时（基于 LangGraph，`octop_harness` 包），负责模型路由、工具、技能、记忆、`SecurityPolicy` 与 HITL 中断。OpenCode **不是**运行时，只是被委派去干「写代码」的外部 Runner。
- **委派面 = ACP（Agent Client Protocol）**：Octop 支持 ACP 双向（`docs/acp.md`）：
  - **入站**：`octop acp --agent main` 把 Octop 当 Zed/OpenCode 的 coding agent。
  - **出站**：`acp_runner` 工具把编码任务委派给 OpenCode / CodeBuddy / Claude Code / Codex（内置 Runner：`opencode` 命令 `opencode acp`）。

### 1.2 委派 OpenCode 的真实落地
源码位置：`src/octop/api/routers/acp.py`、`src/octop/infra/agents/settings/acp.py`、`src/octop/infra/agents/providers/opencode_session.py`。
- Runner 是「命令 + 参数 + 环境变量」配置，按**用户全局**存于 `settings` 表（`acp_runners:user:{id}`），每个 Agent 只开关 `acp.tool_enabled`（`config_json.acp.tool_enabled`）。
- 内置 Runner 含 `opencode`（命令 `opencode`，参数 `acp`）、`codebuddy`、`claude_code`、`codex`、`kimi_code`、`cursor_cli`、`pi`。
- `trusted: bool` 字段决定该 Runner 是否可信；`tool_parse_mode`（`call_title` / `update_detail` / `call_detail`）控制工具调用在 UI 里展示多少细节。
- **Octop 不写 opencode 配置覆盖层**，而是 `subprocess` 起 `opencode acp`（stdio JSON-RPC），以 ACP client 身份驱动。

### 1.3 权限闸 / 人在环（HITL）
源码位置：`src/octop/infra/agents/security/`（policy_store.py、hitl_session.py、tool_guard_rules.py）、`src/octop/infra/gateway/hitl/`（coordinator.py、format.py、store.py）。
- **两层闸门**：
  1. **Harness 级 `SecurityPolicy`**（存 `settings` 表键 `security_policy`，模型来自 `octop_harness.security.models`）：含 `hitl.enabled` 与 `tool_guard`（`enabled` + `mode` ∈ {`warn`, `require_approval`}）。这是 Octop **自身工具**的治理。
  2. **OpenCode 原生权限 relay**：`docs/acp.md` 第 64 行明确——「Permission prompts from the external agent appear in chat… `action=respond` with the option id」。即 OpenCode 跑 `opencode acp` 时自身权限系统触发 `[permission_required]`，Octop 捕获并 **relay 进聊天（飞书等）**，人类选选项后回灌 `acp_runner action=respond`。
- **逐次授权旁路**：`hitl_session.py` 的 `HitlSessionPolicy`（每线程，存 `threads.hitl_policy`）支持 `ask` / `allow_all` / `allow_tools` 三种模式；`apply_session_bypass` 把该旁路包进 LangGraph 中断的 `when` 谓词——即对话中可临时「全部放行 / 仅放行指定工具」。`ask_user_question` **永不**被跳过。
- **HITL 中断实现**：基于 LangGraph interrupt + `when` 谓词；`src/octop/infra/gateway/hitl/` 负责把中断格式化成卡片并经各 channel（飞书/钉钉/QQ/微信/企微）下发。

### 1.4 飞书承载
源码位置：`src/octop/infra/connectors/gateway/`（adapters/feishu_cli.py、feishu_creds.py、feishu_user_auth.py）、`src/octop/infra/gateway/bot_creators/feishu_bot_creator.py`、`src/octop/infra/auth/sso/providers/feishu.py`。飞书是众多 channel 之一（与钉钉/QQ/微信/企微/Telegram/Discord 并列），通过 gateway adapter 把 HITL 中断与 `permission_required` relay 成卡片。

---

## 2. FreeAgent 核心架构（已确认，对照项）

- **无运行时**：FreeAgent 自己不执行任务，是纯「委派治理层」。
- **委派 OpenCode**：`services/opencode_server.py` 以 `isolate=False`（系统 OpenCode）拉起 `opencode serve`；通过**项目级 `opencode.jsonc` 的 `permission` 字段**注入审批闸门（`_write_overlay`），`close()` 原子还原/删除。
- **权限闸**：OpenCode 原生 `permission` 配置（`allow`/`ask`/`deny` 逐工具，如 `edit:ask`、`bash:ask`、`task:deny`、`external_directory:deny`），由 `delegation_permission_config()` 产出，经飞书桥接逐次授权。
- **飞书逐次授权**：`feishu/bridge.py` + `services/approval.py` + `delegate.py` 把 OpenCode 的权限提示转成飞书卡片，人类逐次放行工具调用；并叠加「项目白名单 + 人类确认 + 步数/时长硬上限」三道闸。
- **审计**：`TaskRecord` 记录谁触发、访问了什么、改了什么、花了多少。

---

## 3. 逐点对照表

| 维度 | Octop | FreeAgent | 同构/差异 |
|---|---|---|---|
| 是否自带运行时 | 是（Octop Harness / LangGraph） | 否（纯治理层） | 差异：FreeAgent 更薄 |
| 委派 OpenCode 方式 | `opencode acp` 子进程，**ACP（stdio JSON-RPC）** 驱动 | `opencode serve` 无头 HTTP 服务，**HTTP（REST/OpenAPI）** 驱动 | **差异：传输协议 ACP vs HTTP** |
| 权限闸来源 | OpenCode 原生权限（经 ACP relay）+ Harness `SecurityPolicy` | OpenCode 原生 `permission`（覆盖层注入） | **同构**：都信任 OpenCode 原生权限 |
| 人工授权承载 | 飞书/钉钉/… 多 channel relay `[permission_required]` | 飞书桥接逐次授权卡 | **同构**：飞书人在环 |
| 逐次放行粒度 | 整个 `permission_required` 选项 + 线程级 `allow_tools` 旁路 | 逐工具 `ask`/`deny` + 飞书逐次卡 | 近似同构 |
| 项目白名单 | 无（靠 sandbox/runner 边界） | 有（FreeAgent 三道闸之一） | FreeAgent 更聚焦安全边界 |
| 多用户 | 完整（JWT、管理员/家庭成员隔离、专家市场） | 偏单人/操作员 | 差异：平台厚度 |
| 可委派目标 | 多（OpenCode/Claude/Codex/CodeBuddy/Cursor…） | 单一（OpenCode） | Octop 更广 |
| 审计 | 中断/线程策略落库 | `TaskRecord` 落库 | 同构：都落库可审计 |

---

## 3.1 传输协议：ACP vs HTTP（关键修正）

用户指正：Octop 用 **ACP**，FreeAgent 用 **HTTP**——这不是细枝末节，而是两条委派路线的根本分叉。依据 OpenCode 官方文档（`opencode.ai/docs/zh-cn/acp/` 与 `/zh-cn/server/`）及 FreeAgent 源码确认：

- **ACP（`opencode acp`）**：把 OpenCode 作为 ACP 兼容**子进程**启动，通过 **stdio 上的 JSON-RPC** 与「编辑器/外部 Agent」通信。官方明确「与在终端中使用的效果完全一致……代理和权限系统」均受支持。即 ACP 会原样携带 OpenCode 的**权限提示**。Octop 即把 `opencode acp` 当黑盒子进程，自己扮演「编辑器」角色去驱动它。
- **HTTP（`opencode serve`）**：运行一个**无头 HTTP 服务器**（默认 `127.0.0.1:4096`，OpenAPI 3.1），外部控制器用 REST 与之交互，靠 `OPENCODE_SERVER_PASSWORD` 做 Basic Auth。暴露的端点直接覆盖「委派治理」所需全链路：`PATCH /config`（改配置）、`POST /session`（建会话/发消息）、**`POST /session/:id/permissions/:permissionID`**（应答权限请求）、`POST /tui/control/response` + `GET /tui/control/next`（控制/权限循环）、SSE `/event`（权限事件流）。

**FreeAgent 是明确的 HTTP 客户端**（源码证据）：`OpenCodeServer` 用 `urllib.request` 连 `base_url`；`call("/config")`、`list_agents()`→`GET /agent`、`pending_permissions()`→`GET /permission`、`reply_permission()`→`POST /permission/{id}/reply`、`permission_from_event()` 解析 `permission.asked` 事件；启动 `opencode serve --hostname 127.0.0.1`，注入 `OPENCODE_SERVER_USERNAME/PASSWORD`。

### 两条路线的治理差异（同源不同形）
- **FreeAgent（HTTP + 策略注入）**：① 启动前把闸门策略写成项目级 `opencode.jsonc` 的 `permission` 覆盖层（预置 allow/ask/deny）；② 运行时轮询 `GET /permission`、用 `POST /permission/{id}/reply` 应答，飞书桥接把这两步变成逐次授权卡。即**既注入策略、又经 HTTP 应答单次提示**。
- **Octop（ACP + 提示 relay）**：不写 opencode 权限覆盖层，依赖 OpenCode 自身配置 + Runner 的 `trusted` 标志；OpenCode 经 ACP 发 `[permission_required]`，Octop relay 进聊天，人类选选项后 `acp_runner action=respond`（选项 id）回灌。即**不注入策略、只 relay 原生提示**。

### 为什么各自选了不同协议
- **FreeAgent 选 HTTP**：`opencode serve` 提供的是**完整可编程控制面**——改配置、管会话、应答权限、订阅事件全在一个 TCP 通道里；且天然无头、可无人值守，契合「Agent 之上的 Agent」做**控制器**而非单纯驱动器。其 `pending_permissions`/`reply_permission` + 飞书桥接，本质上就是 OpenCode `/tui/control` 循环的人工化替代。
- **Octop 选 ACP**：ACP 是**标准「编辑器↔编码 Agent」协议**，Octop 自居「编辑器」把 OpenCode 当外部编码 Agent 驱动，保持 OpenCode 为黑盒、不管理其生命周期/配置，最省事地实现「派个编码任务 + relay 它的权限提示」。

### 重要注记
- ACP 与 HTTP **并非完全互斥**：OpenCode 运行时内部**总是**起一个 server；`opencode acp` 只是在该 server 之上包了一层 stdio JSON-RPC 客户端接口。所以两者底层都建立在 OpenCode server 架构上，差别只在**客户端传输（stdio vs TCP-HTTP）**与**策略由谁配置（FreeAgent 注入 vs Octop 依赖原生）**。
- FreeAgent 用到的端点（`/permission`、`/permission/{id}/reply`）与最新文档写法（`/session/:id/permissions/:permissionID`）略有出入，属 OpenCode HTTP API 跨版本的演进漂移，FreeAgent 锁定了其钉版版本的端点，不影响结论。

## 4. 谁更贴近 FreeAgent 的本质

FreeAgent 本质 = **不做执行、只做委派治理（项目白名单 + 人类确认 + 逐次授权 + 审计），执行全交 OpenCode**。

- Octop 把「委派 OpenCode + 飞书承载权限提示」**同构**地实现了，且 `docs/acp.md` 那条 relay 路径（`[permission_required]` → 聊天选项 → `action=respond`）几乎就是 FreeAgent 飞书桥接的「协议版」。
- 但 Octop 把治理放在 **Harness 层**（LangGraph 中断），并包了完整平台；FreeAgent 把治理**翻译成 OpenCode 原生 `permission` 配置**、薄薄一层。两条路都验证了「信任 substrate 原生权限 + 飞书人在环」是可行且正确的。

---

## 5. 可借鉴点（落到 FreeAgent 的具体建议）

1. **`trusted` / `tool_parse_mode` 思路**：FreeAgent 目前对所有委派一律注入同一份 `delegation_permission_config()`。可借鉴 Octop runner 的 `trusted` 标志——对「只读/低风险」执行器放宽 `permission`（如 `read:allow`），对高风险保持 `ask`/`deny`，在 `executors.py` 里按执行器声明策略。
2. **线程级 `allow_tools` 旁路**：Octop 允许对话中临时「仅放行指定工具」且 `ask_user_question` 永不跳过。FreeAgent 的飞书卡目前是逐次确认，可加一个「本任务内对 X 工具免确认」的快捷按钮（对应 `allow_tools`），减少重复点击，但**必须保留 `ask_user_question`/危险操作不可跳过**这条纪律。
3. **`tool_guard` 的 `warn` vs `require_approval` 双模**：FreeAgent 的 `permission` 是硬 `ask`/`deny`，缺「仅告警不阻断」模式。可参考加一个 `warn` 档（审计但放行），用于内部可信环境。
4. **ACP 而非子进程配置注入**：长期看，若 FreeAgent 想支持「多执行器（Claude/Codex…）」，应学 Octop 走 **ACP 协议驱动**（`opencode acp` 等），而非为每个执行器写一份专属覆盖层——这正好对应 FreeAgent 文档里 `executors.py`「协议知识只住一个文件」的设计原则。当前单 OpenCode 下，覆盖层方案更轻、更直接，无需立刻改。
5. **中断格式化与多 channel**：FreeAgent 目前只飞书；若未来要企微/钉钉，可参考 Octop `infra/gateway/hitl/` 的「中断 → 格式化 → 多 adapter 下发」分层，把 `feishu/bridge.py` 抽成 `gateway` + `adapters` 结构。

---

## 6. 关键源码索引

**Octop（D:\GitHub\Octop）**
- 委派/ACP：`src/octop/api/routers/acp.py`、`src/octop/infra/agents/settings/acp.py`、`src/octop/cli/commands/acp.py`、`docs/acp.md`
- OpenCode 集成：`src/octop/infra/agents/providers/opencode_session.py`
- 权限闸：`src/octop/infra/agents/security/{policy_store,hitl_session,tool_guard_rules}.py`
- HITL 中断：`src/octop/infra/gateway/hitl/{coordinator,format,store}.py`
- 飞书：`src/octop/infra/connectors/gateway/adapters/feishu_cli.py`、`src/octop/infra/gateway/bot_creators/feishu_bot_creator.py`、`src/octop/infra/auth/sso/providers/feishu.py`

**FreeAgent（D:\PycharmProjects\freeagent）**
- 委派/覆盖层：`src/freeagent/services/opencode_server.py`（`_write_overlay` / `delegation_permission_config` / `apply_gate`）
- 飞书桥接与授权：`src/freeagent/feishu/bridge.py`、`src/freeagent/services/approval.py`、`src/freeagent/delegate.py`
- 执行器协议抽象：`src/freeagent/services/executors.py`
- 设计文档：`docs/个人事务助手设计方案.md`（V1.31：系统 OpenCode + 项目级权限覆盖层）
