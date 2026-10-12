# 协议选型：ACP 还是 HTTP

> 适用场景：FreeAgent 作为「Agent 之上的 Agent」，委派外部执行器（OpenCode 等）时如何选通信协议。
> 关联文档：[参考-Octop实现对照.md](./参考-Octop实现对照.md)（第 3.1 节「传输协议：ACP vs HTTP」）

## 0. 一句话结论

ACP 与 HTTP **不是技术代差**，而是**两种客户端传输 + 两种控制哲学**：

- **ACP** = 宿主视角，把执行器当黑盒子进程，用 stdio 驱动，信任其原生权限；
- **HTTP** = 控制器视角，把执行器当无头服务，用 REST 管配置/会话/逐次放行，自己拥有策略。

真正要回答的问题不是「哪个更先进」，而是「**你的角色是宿主还是控制器**」。

## 1. 两种协议的本质

### 1.1 ACP（Agent Client Protocol）

- **命令**：`opencode acp`，把 OpenCode 作为子进程拉起。
- **传输**：**stdio JSON-RPC**（进程管道），无端口、单进程。
- **角色**：客户端（编辑器 / IDE / 终端宿主）驱动「外部编码 Agent」做事。
- **权限**：OpenCode 原生权限系统原样生效；权限提示经协议带出（类 `[permission_required]` 事件），宿主 relay 进聊天，人类选择后 `action=respond` 回灌。
- **来源**：标准「编辑器 ↔ 编码 Agent」协议，Zed / JetBrains / Neovim / VS Code 通用。Claude Code、Codex、Cursor、OpenCode 都讲 ACP——**一套协议可驱动多个底层 Agent，换执行器不换代码**。

### 1.2 HTTP（opencode serve）

- **命令**：`opencode serve`，起一个无头 HTTP 服务。
- **传输**：**TCP**，默认 `127.0.0.1:4096`，OpenAPI 3.1 + Basic Auth；REST 端点 + SSE 事件流。
- **角色**：客户端是**完整控制器**——可改配置、管会话、应答权限、订阅事件。
- **权限**：可预置策略（如项目级 `opencode.jsonc` 的 `permission` 覆盖层）；运行时用 `GET /permission` 轮询、`POST /permission/{id}/reply` 应答（**注**：OpenCode 跨版本端点命名有漂移，新文档写法为 `/session/:id/permissions/:permissionID`，以实际运行版本为准）。
- **适合**：无头 / 无人值守、多客户端、远程控制、自建人在环桥。

## 2. 架构对照

| 维度 | ACP（stdio JSON-RPC） | HTTP（REST + SSE） |
|---|---|---|
| 拉起方式 | `opencode acp` 子进程 | `opencode serve` 无头服务 |
| 传输 | stdin/stdout 管道，无端口 | TCP 127.0.0.1:4096，Basic Auth |
| 角色 | 宿主（编辑器 / IDE / 终端） | 控制器（Agent 之上的 Agent） |
| 权限策略 | 原生，不注入 | 注入覆盖层 / 预置 |
| 单次提示应答 | 协议带出 → relay 进聊天 → `action=respond` | `GET /permission` → `POST /reply` |
| 典型代表 | Octop | FreeAgent |

文字版链路：

- **ACP**：宿主 → stdin/stdout 管道 → `opencode acp` 子进程（黑盒外部 Agent）→ 权限提示经协议 relay 进聊天 → `action=respond` 回灌。
- **HTTP**：控制器 → TCP 127.0.0.1:4096 → `opencode serve` 无头服务（可编程控制面）→ `GET/POST /permission` 逐次授权。

## 3. 决策判据

### 选 ACP 当…

1. 你是 IDE / 编辑器 / 终端宿主，想嵌入编码能力；
2. 想用一套协议驱动多个编码 Agent（换底层不换代码）；
3. 把 Agent 当黑盒，只把它的权限提示 relay 进 UI；
4. 离线、单进程、不想管端口；
5. 不想自己拥有 / 注入策略；部署最省事，spawn 子进程即可。

> 例：编辑器插件、终端 TUI 内嵌。**Octop 即走此路**（`opencode acp` + 飞书 relay 权限提示）。

### 选 HTTP 当…

1. 要完整控制面：改配置、管生命周期、列 Agent、管多会话；
2. 要预置权限策略（注入覆盖层）；
3. 无头 / 无人值守，多个外部客户端连同一服务；
4. 自己用 REST 应答权限（自建人在环桥）；
5. 要 SSE 实时事件流做 UI；
6. 要跨网络远程控制（带鉴权，注意默认只绑 `127.0.0.1`）；
7. 「Agent 之上的 Agent」治理层。

> 例：**FreeAgent 委派治理层**（`opencode serve` + 覆盖层 + 飞书逐次授权）。

## 4. 关键注记

- **底层同源**：OpenCode 运行时内部总起一个 server；`opencode acp` 只是该 server 之上的 **stdio JSON-RPC 客户端壳**。差异只在两点——① 客户端传输（stdio vs TCP）；② 策略由谁配（原生 vs 注入）。
- **两者不互斥**：同一份 OpenCode 既能用 ACP 接入编辑器，也能同时用 HTTP 被治理层控制。不要把它当成非此即彼的选型。
- **版本漂移**：HTTP 端点路径随 OpenCode 版本演进（如 `/permission` ↔ `/session/:id/permissions/:permissionID`）。FreeAgent 实现须以目标版本实际暴露的路由为准，并做好跨版本兼容（见 `src/freeagent/services/opencode_server.py` 中 `pending_permissions` / `reply_permission` 的适配层）。

## 5. 落到 FreeAgent 的判断

FreeAgent 选 **HTTP 是正确的**：治理层需要①预置 `permission` 覆盖层②飞书逐次应答权限③无人值守长驻。若未来要加「在编辑器里直接呼出 OpenCode」的旁路入口，那一块才用 ACP；**主治理链路保持 HTTP 不变**。

## 6. 参考

- OpenCode 官方文档：ACP（<https://opencode.ai/docs/zh-cn/acp/>）、HTTP Server（<https://opencode.ai/docs/zh-cn/server/>）
- 本项目对照：[参考-Octop实现对照.md](./参考-Octop实现对照.md)
