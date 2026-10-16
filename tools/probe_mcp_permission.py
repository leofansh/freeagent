"""实测：opencode V1 下，**插件注入的 MCP 工具**能否被 ``permission`` 拦住。

## 为什么需要这个探针

FreeAgent 的闸门（:func:`delegation_permission_config`）只登记**内置工具名**
（``edit`` / ``bash`` / ``webfetch`` / ``websearch``）。而用户的 opencode 全局
配置里装着插件（``oh-my-openagent``），它在**运行时注入** MCP 服务器
（``codegraph`` / ``context7`` / ``grep_app`` / ``websearch`` / ``lsp``，
见该插件 README「injected at runtime by the plugin」）。这些工具**不在任何
permission 键下**。

复盘 0001 的教训是「不登记 = 默认 allow」（opencode 默认 ``allow``）。
所以在给委派接浏览器 MCP **之前**，必须先知道：opencode V1 对插件注入的
MCP 工具会不会弹 ``permission.asked``。会弹 → B 路线成立；不弹 →
今天的委派已经带着一群无人审批的工具在跑，那是比浏览器更大的缺口，
得先补闸门。

## 判据（三态，不许有第四种含糊）

- ``ASK`` —— 调用 MCP 工具前挂起了 ``permission.asked``（**可拦**）
- ``SILENT`` —— MCP 工具静默执行完，全程没有任何 permission 记录（**拦不住**）
- ``INCONCLUSIVE`` —— agent 压根没调那个工具（这次没测到，**不许当成结论**）

## 探针纪律

抄 :mod:`tools.probe_view_routing` 那条：**先证明被测的东西真的跑了，再报
数字**。一个从没被调用过的工具和一个「静默通过了闸门」的工具，表面上都是
「什么都没发生」。所以必须先从事件流里拿到「MCP 工具真的被调用了」的证据
（tool part），否则一律 INCONCLUSIVE。

用法（仓库规矩）：``set PYTHONIOENCODING=utf-8`` + ``python tools\probe_mcp_permission.py``
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.stdout.reconfigure(encoding="utf-8")

from freeagent.delegate import detect_opencode_version  # noqa: E402
from freeagent.services.executors import dispatchable_majors  # noqa: E402
from freeagent.services.opencode_server import (  # noqa: E402
    OpenCodeServer,
    delegation_permission_config,
    permission_from_event,
    read_only_permission_config,
)

#: 被测的 MCP 工具。选 codegraph 的三个理由：插件注入（与 websearch/context7
#: 同一条注入路径）、**只读**（探针不该有副作用）、**离线**（不依赖外网，
#: 断网也能测——「断网照常能用」是这个仓库的硬性质）。
#: ⚠️ 换工具只改这一处；判据逻辑与工具名无关。
TARGET_TOOL = "codegraph_explore"

#: 第二阶段的额外闸门规则。
#:
#: 来源刻意是 **argv**（``probe_mcp_permission.py codegraph_xxx=ask``）而不是环境变量：
#: cmd 的 ``set X=Y && …`` 在后台链里根本传不进子进程（实测 ``FREEAGENT_PROBE_PURE=1``
#: 读出来是 0），而 ``set "X={""a"":""b""}"`` 的引号转义又会把整串吃掉 ——
#: 两种写法都**静默少测一半还不报错**。argv 没有这些坑。
#: 环境变量仍认（给交互式调试用），但那条路要自己确认真的传进去了。
#:
#: 用来验「登记了就会问」——第一阶段只能证「没登记=静默放行」，
#: 证不了「登记了真的拦得住」，那才是接浏览器 MCP 要依赖的那一半。
def _extra_rules() -> dict[str, str]:
    raw_items = list(sys.argv[1:])
    env_raw = os.environ.get("FREEAGENT_PROBE_EXTRA_RULES", "") or ""
    raw_items += [item for item in env_raw.split(",") if item.strip()]
    out: dict[str, str] = {}
    for item in raw_items:
        item = item.strip()
        if not item:
            continue
        # ``--pure`` / ``--agent=x`` 是**开关**，不是规则 —— 必须先跳过。
        # 否则 ``--pure`` 会被当成「删掉名叫 -pure 的规则」，而 ``--agent=x``
        # 会被当成「删掉名叫 agent 的规则」：今天都不撞名所以无害，但那是
        # 巧合，不是设计。
        if item.startswith("--"):
            continue
        # ``-*`` / ``-*=`` 都表示「删掉这条规则」；删的写法**不带值**。
        drop, sep, value = item.partition("=")
        drop = drop.strip()
        if drop.startswith("-"):
            DROPPED_RULES.add(drop[1:])
            continue
        if not sep:
            continue
        out[drop] = value.strip()
    return out


#: argv 里以 ``-`` 开头的键会进这里，用来**删掉**生产配置里的某条规则 ——
#: 验「opencode 未命中规则时到底默认什么」只能这么做，因为默认行为只在
#: **没有兜底规则**时才会露出来（2026-10-14 实测）。
DROPPED_RULES: set[str] = set()


EXTRA_RULES = _extra_rules()

#: ``--agent=<名字>`` → 发指令时**显式指定 agent**（opencode ``prompt.ts:916``
#: 是 ``input.agent || defaultAgent()``，显式值优先）。
#: 用途：量「委派跑在用户插件的人格下」这件事的**实际代价** —— 默认情况下
#: 本机 steering 插件会把 agent 换成 ``Sisyphus - ultraworker``，
#: 一次回「OK」就烧 2 万 input tokens（实测）。
PROBE_AGENT = ""
#: ``--pure`` → 给子进程加 ``OPENCODE_PURE=1``，等价于 ``delegate.plugins: "off"``。
#: 用来**验那个开关真的有效**，而不只是相信 opencode 的源码。
#: 走 argv 而不是环境变量：实测 cmd 的 ``set X=Y && …`` 传不进子进程
#: （``FREEAGENT_PROBE_PURE=1`` 读出来是 0），argv 没这个坑。
PURE = False
PROBE_AGENT = ""
PROBE_MODEL = "opencode/big-pickle"
for _item in sys.argv[1:]:
    if _item.startswith("--agent="):
        PROBE_AGENT = _item.split("=", 1)[1].strip()
    elif _item == "--pure":
        PURE = True
    elif _item.startswith("--model="):
        # 需要它的原因：big-pickle 的免费额度会用完（403 FreeTierError），
        # 而那让轮次**在模型侧**就失败（空 parts、0 tokens、session.error）——
        # 与闸门无关，但会把判据变成 INCONCLUSIVE。换模型才能把变量分开。
        PROBE_MODEL = _item.split("=", 1)[1].strip()

PURE = PURE or os.environ.get("FREEAGENT_PROBE_PURE", "").strip() == "1"

#: ``--env-gate=readonly`` → **不写覆盖层**，改用 ``OPENCODE_CONFIG_CONTENT``
#: 把 ``read_only_permission_config()`` 投递给实例。
#:
#: 验的是「环境变量能不能当闸门的投递通道」—— 这是把 `/ask` 从 jail 搬到
#: 统一治理路的前提：`/ask` 的 project 是 ``Path.cwd()``，走覆盖层等于在任意
#: 目录留文件（那正是今天在修的残留问题），所以它必须换一种投递。
#:
#: 判据因此多一态：read-only 那份的兜底是 ``*: deny``，所以未登记的 MCP 工具
#: 应当**被拒绝**，而不是被问（`/ask` 没有审批人，问等于挂死），更不该被放行。
ENV_GATE = ""
for _item in sys.argv[1:]:
    if _item.startswith("--env-gate="):
        ENV_GATE = _item.split("=", 1)[1].strip()

#: 必须**单行**：brief 里有换行，opencode 会判定为「复杂任务」并升级到主
#: agent 的强模型、无视 ``--model``（README 已记这个坑）。
#: 可用 ``FREEAGENT_PROBE_BRIEF`` 覆盖——冒烟（不含工具调用的纯文本轮次）
#: 与正式探针（逼一次 MCP 调用）靠它区分，只改输入不改判据逻辑。
BRIEF = os.environ.get(
    "FREEAGENT_PROBE_BRIEF",
    "你必须先调用名为 codegraph_explore 的 MCP 工具，"
    "query 填 'ApprovalStore pending_for_web'，"
    "然后只回复它返回的文件路径列表，不要做任何别的操作。",
)

#: 整体预算：启动 + 一轮真模型调用。到点没 idle 就按手头证据收口。
DEADLINE_SECONDS = 150.0


def _walk_tools(node: object, out: set[str]) -> None:
    """递归收集事件里出现过的工具名。

    不假设载荷形状：``permission.asked`` 的 ``permission`` 字段、tool part
    的 ``tool`` 字段都在里面，各版本字段名有漂移（适配器的存在就是证明），
    按 key 名收比按 schema 收稳。
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "tool" and isinstance(value, str) and value:
                out.add(value)
            else:
                _walk_tools(value, out)
    elif isinstance(node, list):
        for item in node:
            _walk_tools(item, out)


def _is_target(name: str) -> bool:
    """认得出 opencode 给 MCP 工具加的**命名空间前缀**。

    实测（2026-10-14）：MCP 工具在事件流与 permission 键里叫
    ``<服务器名>_<工具名>``（``codegraph`` 服务器的 ``codegraph_explore``
    → ``codegraph_codegraph_explore``）。所以只等全等会**永远判不出来**——
    第一版探针就死在这儿，把「静默放行」误报成 INCONCLUSIVE。
    """
    return name == TARGET_TOOL or name.endswith("_" + TARGET_TOOL)


def main() -> int:
    # -- 0. 版本闸门：与生产派发同一个判据，不是 V1 就拒绝 ------------------ #
    major = detect_opencode_version("opencode")
    print(f"=== opencode --version 主版本：{major}（可派发：{dispatchable_majors()}）")
    if major not in dispatchable_majors():
        print("!! 不是 V1。本仓库按 V1 的字段名接线，拒绝继续（与派发同一理由）")
        return 2

    # -- 1. 起与生产派发同款的真实例 ---------------------------------------- #
    # delegate.py 的派发参数就是这四个：isolate=False（系统 opencode，吃用户
    # 真实全局配置）+ apply_gate=True（项目级覆盖层压闸门）+ 生产那份
    # permission 映射。少一个都不算「同款」。
    # ⚠️ ``delegation_permission_config()`` 返回的是**整份配置**
    # （``{"$schema":…, "permission": {…}}``），不是光一个规则 dict。
    # 第一版探针把额外规则 update 到**顶层**，而 ``_write_overlay`` 取的是
    # ``full["permission"]`` —— 键被丢掉，闸门压根没变，于是「登记了也不问」
    # 这个结论是假的（实测踩过：多跑一轮才发现）。
    rules = dict(delegation_permission_config())
    perm = dict(rules.get("permission", {}))
    if DROPPED_RULES:
        for key in DROPPED_RULES:
            perm.pop(key, None)
        print(f"=== 已删掉规则：{sorted(DROPPED_RULES)}")
    if EXTRA_RULES:
        perm.update(EXTRA_RULES)
        print(f"=== 第二阶段：额外闸门规则 {json.dumps(EXTRA_RULES, ensure_ascii=False)}")
    if perm:
        rules["permission"] = perm

    # ``--env-gate=readonly`` 时**故意不写覆盖层**：这一轮验的就是
    # 「环境变量单独当投递通道」能不能成立。两路一起上就分不清是谁起的作用。
    env_gate_readonly = ENV_GATE == "readonly"
    extra_env: dict[str, str] = {"OPENCODE_PURE": "1"} if PURE else {}
    if env_gate_readonly:
        extra_env["OPENCODE_CONFIG_CONTENT"] = json.dumps(
            read_only_permission_config(), ensure_ascii=False
        )
        print("=== 投递通道：OPENCODE_CONFIG_CONTENT（不写覆盖层）")

    server = OpenCodeServer(
        project=ROOT,
        executable="opencode",
        isolate=False,
        apply_gate=not env_gate_readonly,
        permission_config=rules,
        extra_env=extra_env or None,
    )
    started = time.monotonic()
    with server:
        print(f"=== 实例就绪（{time.monotonic() - started:.1f}s）"
              f" base={server.base_url}"
              f" OPENCODE_PURE={'1' if PURE else '0'}")

        # 自证闸门真的压上去了：覆盖层文件与内容打出来。没有这一步，
        # 「什么都没拦住」可能只是因为闸门压根没写进去。
        overlay = getattr(server, "_overlay_path", None)
        print(f"=== 覆盖层：{overlay}")
        if overlay is not None and pathlib.Path(overlay).exists():
            print(pathlib.Path(overlay).read_text(encoding="utf-8"))
        elif not env_gate_readonly:
            print("!! 没找到覆盖层文件 —— 闸门可能没压上，结论无效")
            return 2
        else:
            # 环境变量投递的路**本来就没有覆盖层**。这一轮的自证换成下面
            # 的 GET /config + 行为判据（工具到底被拒了没有）。
            print("（环境变量投递：无覆盖层是预期，自证看下面的 /config 与判据）")

        # 这个实例**实际在跑**的 permission 规则。文件是我们写的，实例报出来的
        # 才是「真在生效」—— 两者不一致时（比如双层 permission 让 opencode 整份
        # 丢弃），只有后者算数。
        code, raw = server.call("/config")
        if code == 200:
            try:
                live = json.loads(raw).get("permission", {})
                print(f"=== 实例生效的 permission：{json.dumps(live, ensure_ascii=False)}")
            except ValueError:
                print(f"=== 实例 /config 非 JSON：{raw[:400]!r}")
        else:
            print(f"=== GET /config -> {code}（跳过；不影响判据）")

        # 这个实例眼里的 MCP 有哪些（best effort：端点不在就跳过，不影响判据）
        code, raw = server.call("/mcp")
        if code == 200:
            try:
                print(f"=== GET /mcp：{json.dumps(json.loads(raw), ensure_ascii=False)[:800]}")
            except ValueError:
                print(f"=== GET /mcp（非 JSON）：{raw[:400]!r}")
        else:
            print(f"=== GET /mcp -> {code}（端点不可用，跳过；不影响判据）")

        # -- 2. 发指令 ------------------------------------------------------- #
        sid = server.create_session()
        print(f"=== 会话 {sid}，发指令（目标工具 {TARGET_TOOL}"
              f"{f'，指定 agent={PROBE_AGENT}' if PROBE_AGENT else ''}）")
        server.prompt_async(sid, BRIEF, model=PROBE_MODEL,
                            agent=PROBE_AGENT)

        # -- 3. 盯事件流 + 轮询挂起列表 -------------------------------------- #
        permission_events: list[tuple[str, str, str]] = []   # (id, permission, pattern-ish)
        pending_seen: list[str] = []                          # GET /permission 见过的
        tools_seen: set[str] = set()
        event_types: dict[str, int] = {}
        toasts: list[str] = []                                # tui.toast.show 的文案
        idle = False

        for etype, props in server.events(timeout=30.0):
            event_types[etype] = event_types.get(etype, 0) + 1
            _walk_tools(props, tools_seen)
            if etype == "tui.toast.show":
                # toast 常带 agent 的失败原因（「unknown tool」之类），是
                # INCONCLUSIVE 时最有价值的诊断。
                body = props.get("message") if isinstance(props, dict) else None
                if isinstance(body, dict):
                    body = body.get("message") or body.get("title")
                if isinstance(body, str) and body:
                    toasts.append(body)
            if etype == "session.error":
                # 这一条**必须原样打出来**：空 parts + 0 tokens 的轮次有兩種完全
                # 不同的成因（模型侧失败 vs 闸门把工具全禁了），而处置方式相反。
                # 不打印它就分不清，只能瞎猜（2026-10-14 在这上面卡过一轮）。
                print(f"  [session.error] {json.dumps(props, ensure_ascii=False)[:1200]}")
            if etype == "permission.asked":
                perm = permission_from_event(props)
                if perm is not None:
                    permission_events.append(
                        (perm.request_id, perm.permission,
                         "|".join(perm.paths) or "(无路径)")
                    )
                    print(f"  [permission.asked] {perm.permission}"
                          f" → {perm.paths or '(无路径)'}")
            if etype == "session.idle":
                idle = True
                break
            for item in server.pending_permissions():
                rid = str(item.get("id", "")) if isinstance(item, dict) else str(item)
                if rid and rid not in pending_seen:
                    pending_seen.append(rid)
                    print(f"  [GET /permission] 挂起 {rid}: {item}")
            if time.monotonic() - started > DEADLINE_SECONDS:
                print("!! 到总预算，按手头证据收口")
                break

        # -- 4. 证据与判据 ---------------------------------------------------- #
        target_ran = any(_is_target(name) for name in tools_seen)
        asked = any(_is_target(p) for _, p, _ in permission_events)
        pending_hit = bool(pending_seen)

        print("\n=== 证据 ===")
        print(f"会话 idle：{idle}")
        print(f"事件类型计数：{event_types}")
        print(f"出现过工具名：{sorted(tools_seen)}")
        print(f"permission.asked 记录：{permission_events}")
        print(f"GET /permission 挂起过：{pending_seen}")
        print(f"toast 文案（{len(toasts)} 条）：{toasts[:10]}")

        # 会话实况：agent 到底回了什么、有没有尝试调工具。
        # INCONCLUSIVE 时这是唯一能区分「没调」与「调了但失败」的东西。
        denied_text = ""      # 工具被 permission 拒绝时，错误文本落在这里
        try:
            msgs = server.call(f"/session/{sid}/message")
            if msgs[0] == 200 and msgs[1]:
                rows = json.loads(msgs[1])
                print(f"\n=== 会话消息（{len(rows)} 条，原始 JSON 前 2500 字符）===")
                print(msgs[1].decode("utf-8", "replace")[:2500])
                for row in rows:
                    info = row.get("info") if isinstance(row, dict) else None
                    role = (info or {}).get("role", "?") if isinstance(info, dict) else "?"
                    err = (info or {}).get("error") if isinstance(info, dict) else None
                    parts = row.get("parts") if isinstance(row, dict) else []
                    tools = []
                    if isinstance(parts, list):
                        for part in parts:
                            if isinstance(part, dict) and part.get("type") == "tool":
                                state = part.get("state") or {}
                                name = part.get("tool", "?")
                                status = state.get("status", "?")
                                tools.append(f"{name}[{status}]")
                                # read-only 的兜底是 *: deny —— 拒绝时 opencode
                                # 让工具以 error 收场（PermissionDeniedError），
                                # 文本里带 "denied"/"PermissionDenied"。这是
                                # 「被拒」与「跑完」唯一分得开的地方。
                                if status in ("error", "rejected"):
                                    body = (state.get("error")
                                            or state.get("output") or "")
                                    if isinstance(body, dict):
                                        body = json.dumps(body, ensure_ascii=False)
                                    if "denied" in str(body).lower():
                                        denied_text = f"{name} → {body}"
                    print(f"  [role={role}] error={err} tools={tools}")
        except Exception as exc:  # noqa: BLE001 - 诊断失败只报告，不改判据
            print(f"(拉会话消息失败，忽略：{exc})")

        print("\n=== 判据 ===")
        if not target_ran:
            print(f"INCONCLUSIVE：{TARGET_TOOL} 没被调用。")
            print("  agent 没用这个工具，「没弹权限」与「拦不住」外表相同，")
            print("  不许当结论。先看上面的工具名列表诊断（MCP 没注入？模型挑了别的工具？）。")
            verdict = "INCONCLUSIVE"
        elif denied_text:
            # read-only 那份的兜底是 *: deny —— 期望的就是这一态：
            # 不问（/ask 没有审批人，问等于挂死），也不放行。
            print(f"DENIED：{TARGET_TOOL} 被**拒绝**执行。")
            print(f"  证据：{denied_text[:300]}")
            print("  结论：OPENCODE_CONFIG_CONTENT 能把 read-only 闸门压进去，")
            print("  环境变量可以当投递通道 —— /ask 不必靠 jail 也能「改不了」。")
            verdict = "DENIED"
        elif asked or pending_hit:
            print(f"ASK：{TARGET_TOOL} 被调用，且调用前有 permission 拦截。")
            print("  结论：插件注入的 MCP 工具**可以**被 permission 拦 ——")
            print("  B 路线成立，把浏览器 MCP 工具名登记进 permission 即可。")
            verdict = "ASK"
        else:
            print(f"SILENT：{TARGET_TOOL} 静默执行完，全程没有任何 permission 记录。")
            print("  结论：插件注入的 MCP 工具**不在**闸门覆盖内 ——")
            print("  今天的委派已经带着一群无审批工具在跑；接浏览器前必须先补闸门。")
            verdict = "SILENT"

        # 诊断尾部：插件加载失败、MCP 注入失败都写在这里
        log = server.child_log(max_chars=6000)
        if log.strip():
            print("\n=== 子进程日志尾部（诊断用）===")
            print(log)

        try:
            server.abort(sid)
        except Exception as exc:  # noqa: BLE001 - 收尾失败只报告，不改判据
            print(f"(abort 失败，忽略：{exc})")

    print(f"\n最终判据：{verdict}")
    return 0 if verdict == "ASK" else 1


if __name__ == "__main__":
    raise SystemExit(main())
