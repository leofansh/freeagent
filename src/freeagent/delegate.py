"""委派执行器（**独立进程**）：``python -m freeagent.delegate``。

为什么是独立进程，而不是 Web 服务器里的一个线程
------------------------------------------------
委派链路的终点是「在你的机器上执行代码」。所以这条链路必须**独立于**
任何接收远程输入的组件：

* Web UI 在 ``127.0.0.1``，但仍是 HTTP —— 万一将来加了远程通道，
  那个通道就等于代码执行入口
* 飞书消息是**远程输入**。谁拿到 App Secret 谁就能发消息

把它做成独立进程 + 只读数据库，就形成一道物理隔离：
这个进程**只从库里取已经被人 ``/start`` 过的事务**，不接受任何外部消息。
消息要变成执行，得先经过人。

三道闸门，缺一不可
------------------
1. **项目白名单** —— :func:`~freeagent.services.delegate.check_project_allowed`
2. **必须 ``active``** —— 你 ``/start`` 过。``inbox`` 的一律不碰
3. **不传 ``--auto``** —— opencode 自己标了 ``dangerous!``，它该停下问就停下问

离线可测
--------
:func:`run_once` 接受注入的 ``runner``，测试全程不启动 opencode、不发网络请求
（和 ``DeepSeekProvider`` 用 ``transport`` 注入同一个手法）。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, ContextManager, Protocol, Sequence

from .app import build_app
from .domain import FreeAgentError, RecordType, TaskState
from .services.approval import (
    BYPASS_FLAGS,
    DEFAULT_DENIED_COMMANDS,
    ApprovalContext,
    ApprovalPolicy,
    ApprovalStore,
    PendingApproval,
    check_command,
    unattended_deny,
)
from .services.delegate import (
    DelegationPolicy,
    DispatchOutcome,
    build_brief,
    check_project_allowed,
    eligible_tasks,
    parse_opencode_output,
)
from .services.opencode_projects import authorized_names
from .services.executors import (
    V2_FIELD_RENAMES,
    adapter_for,
    dispatchable_majors,
    known_majors,
)
from .services.localops import CardSender
from .services.opencode_server import (
    OpenCodeServer,
    ServerError,
    ToolPermission,
    ToolQuestion,
    permission_from_event,
    question_from_event,
)

__all__ = ["Runner", "subprocess_runner", "run_once", "main",
           "ApprovalGate", "Gate", "run_with_tool_gate",
           "ToolCardSender", "ServerFactory",
           "SUPPORTED_OPENCODE_MAJOR", "KNOWN_OPENCODE_MAJORS",
           "parse_major_version",
           "detect_opencode_version", "version_mismatch_reason"]

#: 回推飞书失败之类的问题走日志，不走 stdout —— stdout 是给用户看的报告，
#: 混进 traceback 只会让人以为委派本身炸了。
log = logging.getLogger("freeagent.delegate")

#: ``(argv, cwd, timeout) -> (returncode, stdout, stderr)``
Runner = Callable[[Sequence[str], Path, float], tuple[int, str, str]]


@dataclass(frozen=True, slots=True)
class DispatchReport:
    """一次 ``run_once`` 的结果。给 CLI 打印用。"""

    considered: int
    dispatched: int
    succeeded: int
    failed: int
    notes: tuple[str, ...] = ()


def resolve_command(command: str) -> str:
    """把命令名解析成**可执行的完整路径**。

    踩过的坑：Windows 上 ``opencode`` 实际是 npm 装的 ``opencode.CMD``，
    而 ``subprocess.run(["opencode", …], shell=False)`` 会
    ``FileNotFoundError`` —— ``CreateProcess`` 不会去补 ``PATHEXT``。
    ``shutil.which`` 会。这条在 Linux/macOS 上无害，所以统一走它。

    解析后仍然用 ``shell=False``：brief 里可能有引号、``&&``、换行，
    经 shell 解析会出事。参数以数组传给 ``CreateProcess`` 才安全。
    """
    import shutil

    return shutil.which(command) or command


#: 本仓**可派发**的 opencode 主版本 —— 由注册表推导，不硬编码。
#:
#: V1 与 V2 的 permission 模型字段名完全不同（``permission`` 对象 /
#: ``bash`` / ``task`` vs ``permissions`` 数组 / ``shell`` / ``subagent``），
#: 而 :mod:`freeagent.services.opencode_server` 要写配置。
#: **升到 V2 后旧字段会被静默忽略 —— 不报错，但闸门随之失效，
#: 失效方向恰好是最危险的那侧**（默认回到 allow）。
#:
#: 所以这里朝**关**开：只放行注册表里 ``verified=True`` 的适配器，
#: 而不是警告后照跑。V2 的适配器**存在但未验证**（数组元素形状官方未给出
# 可据以接线的定义），所以它拿不到这个值 —— 也就是说**本次改动不改变
# 任何运行时行为**，只是把「支持哪个版本」从常量变成注册表的一个查询。
#:
#: 判定见 :func:`parse_major_version` 与 :func:`version_mismatch_reason`。
#:
#: 用「取第一个」而不是「取全部」是刻意的：一旦某天 V2 也验证过了，
#: 这里会变成 2，而闸门仍然只放行 1 —— 那时要**显式**改这一行，
#: 而不是让「多了一个已验证适配器」自动改变闸门行为。
SUPPORTED_OPENCODE_MAJOR: int = next(iter(dispatchable_majors("opencode")), 1)

#: 本仓**知道**的主版本（不是可派发的）。用于给「不认识的版本」与
#: 「知道但没验证」两种拒绝各一句**不同且能照着做**的话。
KNOWN_OPENCODE_MAJORS: tuple[int, ...] = known_majors("opencode")


def parse_major_version(text: str) -> int | None:
    """从 ``opencode --version`` 的输出里取主版本号。

    纯函数，不做 IO —— 版本探测要能被测，就得先把解析和调用拆开。

    解析不了返回 ``None``（**不放行**）。宁可因为格式变化停下来问，
    也不要猜一个大版本出来然后照着一份**可能已经过时**的文档接线。
    """
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    return int(m.group(1)) if m else None


def detect_opencode_version(command: str = "opencode") -> int | None:
    """跑一次 ``<command> --version``，返回主版本号。

    刻意**不在热路径上调用** —— 每次派发都多一个进程不合算，
    所以只在 :func:`main` 启动时探一次，探不到就拒绝（见下）。
    """
    import subprocess as _sp

    try:
        proc = _sp.run(  # noqa: S603 - argv 固定，无用户输入
            [resolve_command(command), "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            shell=False,
        )
    except (OSError, _sp.TimeoutExpired):
        return None
    return parse_major_version(f"{proc.stdout or ''}\n{proc.stderr or ''}")


def version_mismatch_reason(
    detected: int | None, expected: int = SUPPORTED_OPENCODE_MAJOR
) -> str | None:
    """版本可用则返回 ``None``，否则给一句**能照着做**的原因。

    探不到版本（``None``）与版本不对同等对待 —— 两者都意味着
    「我们不知道自己在跟什么东西说话」，按设计文档的默认朝关开。

    三种拒绝各给**不同**的话，因为该做的事不同：

    - **不认识的版本** → 只能升级本仓（或降级执行器）
    - **认识但未验证**（V2） → 先对着真实实例验完适配器
    - **就是期望版本** → 放行
    """
    if detected is None:
        return (
            f"探不到 opencode 版本（期望主版本 {expected}）。"
            "可能没装，或 `opencode --version` 输出变了格式。"
        )
    if detected == expected:
        return None

    adapter = adapter_for(detected, "opencode")
    if adapter is None:
        return (
            f"opencode 主版本是 {detected}，本仓**不认识**它"
            f"（已知的只有 {list(KNOWN_OPENCODE_MAJORS)}，见设计文档 11.8.1 "
            "的版本陷阱表）。"
            "本仓拒绝派发而不是照跑 —— 未知版本的 permission 字段名会被"
            "**静默忽略**，闸门随之失效，且失效方向是回到 allow。"
            "请升级本仓，或把执行器降回已知版本。"
        )
    # 认识但 verified=False —— 这就是 V2 当前的状态
    return (
        f"opencode 主版本是 {detected}，本仓**认识**它但**未验证**接线"
        f"（见设计文档 11.8.1 的版本陷阱表）。"
        "本仓拒绝派发而不是照跑 —— V1→V2 的 permission 字段名全变，"
        "旧配置会被**静默忽略**，闸门随之失效，且失效方向是回到 allow。\n"
        f"已知改名：{V2_FIELD_RENAMES}。\n"
        "未确认的是 `permissions` **数组元素的形状** —— 官方文档未给出"
        "可据以接线的定义，猜一个『看起来很像对的』形状比拒绝更危险。\n"
        "要接 V2：对着一个真实 V2 实例实测，改 "
        "`freeagent/services/executors.py` 里那三个 `_v2_unverified`，"
        "把 `verified` 改成 True，并同步设计文档 11.8.1。"
    )


def subprocess_runner(
    argv: Sequence[str], cwd: Path, timeout: float
) -> tuple[int, str, str]:
    """真的去跑。**刻意不 shell=True** —— 参数以数组给，不经 shell 解析。"""
    try:
        proc = subprocess.run(  # noqa: S603 - argv 是我们自己组装的固定列表
            [resolve_command(argv[0]), *argv[1:]],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
        )
    except FileNotFoundError as exc:
        return 127, "", f"找不到执行器：{exc}"
    except subprocess.TimeoutExpired:
        return 124, "", f"超时（{timeout:g} 秒）"
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _argv(policy: DelegationPolicy, brief: str, title: str) -> list[str]:
    """组装 opencode 命令行。

    **刻意不加 ``--auto``**：opencode 自己把这个开关标成 ``dangerous!``。
    委派场景下我们宁可它中途停下来，也不要一个没人盯着的 agent 改代码。
    """
    argv = [policy.command, "run", brief, "--format", "json", "--title", title]
    if policy.model:
        argv += ["--model", policy.model]
    return argv


def run_once(
    *,
    db_path: Path | None = None,
    runner: Runner | None = None,
    policy: DelegationPolicy | None = None,
    dry_run: bool = False,
    gate: "Gate | None" = None,
) -> DispatchReport:
    """扫一遍库，把够格的事务派出去。返回统计。

    ``dry_run=True`` 时**只列出**将要派什么，一条都不跑 —— 第一次用应该先跑它。
    它也**不碰闸门**：dry run 的意义是「看看会派什么」，而闸门要发卡、
    要人点；在这里发卡会让「预演」变成一次真请求。

    ``gate=None`` 时**一条都不派**（每条都记成「没有闸门，拒绝」）。
    委派会执行代码，没闸门就不能派 —— 这不是保守，这是这条链路唯一的安全底。
    """
    app = build_app(db_path)
    the_runner = runner or subprocess_runner
    try:
        with app.lock:
            the_policy = policy or _delegate_policy(app)
            candidates = eligible_tasks(app.task_repo, app.record_repo)
            if not the_policy.enabled:
                return DispatchReport(
                    considered=len(candidates), dispatched=0, succeeded=0,
                    failed=0,
                    notes=("委派未启用（白名单为空），什么都没派。",),
                )

            notes: list[str] = []
            done = failed = 0
            for task in candidates:
                if dry_run:
                    notes.append(f"将派发 {task.id[:8]} → {task.project_path}")
                    continue
                outcome = _dispatch_one(app, the_policy, task, the_runner, gate=gate)
                (done, failed) = (
                    (done + 1, failed) if outcome.ok else (done, failed + 1)
                )
                # **失败必须带上原因。** 踩过的坑：这里原本只拼 ``summary``，
                # 而真正的原因（stderr）在 ``detail`` 里 —— 终端于是只显示
                # 「执行器退出码 1」，用户完全无从下手，尽管原因就存在
                # 产物链里。报错信息不进 stderr 就等于没报。
                head = outcome.summary.splitlines()[0][:80] if outcome.summary else ""
                why = first_meaningful_line(outcome.detail or "")
                note = f"{task.id[:8]} {'成功' if outcome.ok else '失败'}：{head}"
                if not outcome.ok and why:
                    note += f"（{why}）"
                notes.append(note)
            return DispatchReport(
                considered=len(candidates),
                dispatched=0 if dry_run else len(candidates),
                succeeded=done, failed=failed, notes=tuple(notes),
            )
    finally:
        app.close()


def run_once_with_tool_gate(
    *,
    db_path: Path | None = None,
    policy: DelegationPolicy | None = None,
    dry_run: bool = False,
    sender: "ToolCardSender | None",
    approver: str,
    server_factory: "ServerFactory | None" = None,
) -> DispatchReport:
    """执行期逐次授权版的 :func:`run_once`。

    **刻意复用**同一套资格判定（:func:`eligible_tasks`）、同一套白名单
    （:func:`check_project_allowed`）、同一套落库
    （:func:`_record_result`）。只有「怎么跑」这一步不同。

    抄一份资格判定就会漂移，而漂移的方向必然是「多派」——
    所以这里宁可多写几行调用，也不重写规则。

    与 :func:`run_once` 的**关键**区别：它**不**要求传 ``gate``。
    因为这条路的闸门在**执行期**，不在派发前；派发前那道由调用方
    照旧走 :class:`ApprovalGate`（两条路可以叠加，也可以只走一条）。
    """
    app = build_app(db_path)
    try:
        with app.lock:
            the_policy = policy or _delegate_policy(app)
            candidates = eligible_tasks(app.task_repo, app.record_repo)
            if not the_policy.enabled:
                return DispatchReport(
                    considered=len(candidates), dispatched=0, succeeded=0, failed=0,
                    notes=("委派未启用（白名单为空），什么都没派。",),
                )
            store = ApprovalStore(app.conn)
            notes: list[str] = []
            done = failed = 0
            for task in candidates:
                if dry_run:
                    notes.append(f"将派发 {task.id[:8]} → {task.project_path}"
                                 "（执行期逐次授权）")
                    continue
                try:
                    project = check_project_allowed(
                        the_policy, task.project_path or "",
                        known_names=authorized_names(the_policy.projects),
                    )
                    outcome = run_with_tool_gate(
                        task, project,
                        build_brief(task, task.intent, task.definition_of_done),
                        policy=the_policy, store=store, sender=sender,
                        # 同理：执行期的每一次授权也得知道**谁发起的这条委派**。
                        approver=task_requester(task, approver),
                        server_factory=server_factory,
                    )
                except FreeAgentError as exc:
                    outcome = DispatchOutcome(
                        ok=False, summary=f"白名单拒绝：{exc}"
                    )
                _record_result(app, task, outcome)
                (done, failed) = (
                    (done + 1, failed) if outcome.ok else (done, failed + 1)
                )
                notes.append(
                    f"{task.id[:8]} {'成功' if outcome.ok else '失败'}："
                    f"{(outcome.summary or '').splitlines()[0][:90] if outcome.summary else ''}"
                )
            return DispatchReport(
                considered=len(candidates),
                dispatched=0 if dry_run else len(candidates),
                succeeded=done, failed=failed, notes=tuple(notes),
            )
    finally:
        app.close()


#: opencode 的 stderr 带 ANSI 颜色转义，不剥掉的话取到的「第一行」
#: 往往是 ``ESC[0m`` 这种空行色标，而不是真正的报错 —— 报错信息等于丢了。
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _delegate_policy(app) -> DelegationPolicy:
    """取委派策略。``app.config`` **可能真是 None**。

    抄 :func:`freeagent.app.build_app` 里那条已经写好的教训（那里明确说
    「``the_config`` 可能真是 None，直接访问属性会炸」）。同一个坑在这里
    也等着：``App.config`` 的类型就是 ``Config | None``，
    而 ``delegate_policy()`` 挂在 ``Config`` 上。

    读不出来时返回**停用**策略（白名单为空）—— 没配置就没有白名单，
    没白名单就不许委派。往严的方向走，与 11.8「留空 = 关闭委派」一致。
    """
    if app.config is None:
        return DelegationPolicy(projects=())
    return app.config.delegate_policy()


class Gate(Protocol):
    """闸门的**契约**。刻意是 Protocol 而不是 ``ApprovalGate`` 那个类。

    为什么：闸门只有一个方法，而 :class:`ApprovalGate` 之外还有两种正当实现
    —— 测试用的替身（不落库、不发卡），以及将来可能的「按项目配不同审批人」
    的实现。把类型写成具体类，就等于**禁止**这些，而它们是正当的。
    设计文档 12.1.1 的「一份能力一份实现」说的是行为不许分叉，
    不是说实现只能有一个类。

    ``check`` 返回 ``None`` = 放行；返回**字符串** = 拒绝原因。
    返回原因而不是 bool，是为了让用户看到「是自己没点，还是被安全规则挡了」。
    """

    def check(self, *, task_id: str, project: str, brief: str,
               requester: str = "") -> str | None: ...


class ApprovalGate:
    """委派闸门（设计文档 11.9.7）。

    **它回答一个问题**：这条委派**允许执行**吗？只有 ``True`` 才允许派发。

    为什么单独一个类而不是散在 :func:`_dispatch_one` 里：判定点必须**唯一**。
    散开就必然出现「本地操作那条路拦住了、委派那条路忘了」的分叉，
    而这条链路的失败模式是「远程消息在本机执行了代码」。

    它的三个来源**层层收窄**，任一层不通过都不放行：

    1. :meth:`check` 里先查 :func:`~freeagent.services.approval.is_bypass_active`
       —— 有人传了 ``--auto`` 就**拒绝**（不是「容忍」）。
       绕过态必须在**这里**查，而不能只靠 :func:`_argv` 记得别加。
    2. 再查命令地板（deny 优先于白名单 + 拦 shell 运算符拼接）。
    3. 最后才是人：落 pending → 发卡 → 等。**结论一律回库读**
       （:meth:`_store.decide`）—— ``wait`` 只是阻塞点，不持有结论。
       踩过的坑：原先采信 ``wait`` 的返回值，于是「跑了但库里未决定」，
       审计记录与实际执行不一致。

    传 ``sender=None`` 时**永远拒绝**：委派必然要发卡，没有通道就没人能批准，
    那是「拒绝」而不是「跳过闸门」。
    """

    def __init__(
        self,
        store: "ApprovalStore | None",
        sender: "CardSender | None" = None,
        *,
        approver: str = "",
        argv: list[str] | None = None,
        env: dict[str, str] | None = None,
        # ``wait`` **只负责等**：它返回什么都不影响结论。
        #
        # 标注成 ``Callable[[str], object]`` 而不是 ``-> None``，是**如实描述**
        # 运行时契约 —— 「返回值被忽略」。先前标成 ``-> None`` 看着更严，实际
        # 逼着反向测试去写 ``# type: ignore``（而本仓库的检查器不认那个注释），
        # 反而更糟。结论一律 :meth:`_store.decide` 读库，见 :meth:`check`。
        wait: Callable[[str], object] | None = None,        context: str = "remote",
        denied: set[str] | None = None,
        allowlist: set[str] | None = None,
    ) -> None:
        self._store = store
        self._sender = sender
        self._approver = approver
        # argv/env 是**快照**，构造时读一次：闸门判定必须基于派发那一刻的状态，
        # 而不是「判定时恰好读到什么」。
        self._argv = list(argv or [])
        self._env = dict(env or {})
        self._wait = wait
        self._context = context
        self._denied = set(denied or DEFAULT_DENIED_COMMANDS)
        self._allowlist = set(allowlist or ())

    @property
    def context(self) -> ApprovalContext:
        return ApprovalContext(
            self._context, who=self._approver, argv=self._argv, env=self._env
        )

    @property
    def _context_ttl(self) -> int:
        """发卡用的 TTL。与 :meth:`check` 里落 pending 时用的**同一个来源**。

        两处各算一次的话，改了一处就会出现「卡上写着 30 分钟、
        库里实际 10 分钟」—— 而用户是按卡上那个时间做决定的。
        """
        return ApprovalPolicy.for_context(self._context).ttl_seconds

    def check(
        self, *, task_id: str, project: str, brief: str,
        requester: str = "",
    ) -> str | None:
        """返回 ``None`` = 放行；返回**字符串** = 拒绝原因。

        刻意返回「原因」而不是 bool：拒绝时必须能说清是哪一层拦的，
        否则用户只会看到「失败了」而无法判断是自己没点、还是被安全规则挡了。
        """
        # 「谁发起的」是**本次调用**的参数，不是闸门的状态。
        #
        # 原因：闸门是**一次构造、多条事务共用**的，而不同事务的发起人不同——
        # 一个共用的 approver 装不下。它被用在**三处**处：
        # pending 行的 requested_by、卡片的收件人、
        # 以及 context.who。
        #
        # 优先级：有身份（飞书发起）用它；没有（终端发起）
        # 落回 ``self._approver``，**行为与今天完全一致**。
        who = requester or self._approver
        ctx = ApprovalContext(
            self._context, who=who, argv=self._argv, env=self._env
        )

        # 1) 绕过态。**有就是拒绝**，不设「容忍绕过」这档。
        if ctx.is_bypassing:
            return f"检测到绕过批准的开关（argv/env）—— 见 {BYPASS_FLAGS or 'BYPASS_FLAGS'}"

        # 2) 命令地板。检查的是**将要执行的那条命令**的形状。
        #    project 必须是不含运算符的绝对路径：它在白名单里，
        #    但如果它自己带着 ``;``，就说明有人把整条命令塞进了「项目路径」。
        #
        #    ``str()`` 不是修辞 —— :func:`check_project_allowed` 返回的是
        #    ``Path``，而 check_command 收 ``str``。踩过的坑（实测）：
        #    漏了这个转换会在真实链路上抛 ``'WindowsPath' object has no
        #    attribute 'strip'``，而**攻击测试本来是用来抓这种崩的**，
        #    结果它自己先崩了。
        ok, why = check_command(
            str(project),
            allowlist=self._allowlist or None,
            denied=self._denied or None,
        )
        if not ok:
            return f"项目路径未通过闸门（{why}）"

        # 3) 人。没有 store 就没人能批准 ⇒ 拒绝（不是跳过）。
        if self._store is None or self._sender is None:
            return "没有审批通道（store/sender 缺失）—— 无人能批准，故拒绝"

        pending = self._store.request_delegation(
            project=str(project), brief=brief, context=ctx
        )
        try:
            card = self._send_card(pending, to=who)
        except Exception as exc:  # noqa: BLE001 - 发卡失败必须变成拒绝
            # 发卡失败**绝不**降级成「那就直接跑吧」—— 那正是闸门形同虚设
            # 的样子：通道坏了 = 无人监督 = 拒绝。
            return f"发卡失败（{type(exc).__name__}）—— 无人能批准，故拒绝"

        if self._wait is not None:
            # ``wait`` 只负责**等**，不负责「答」。
            #
            # 踩过的坑（实测，攻击测试抓到的）：原先是
            # ``decision = self._wait(cred)`` —— 直接采信 ``wait`` 的返回值。
            # 于是只要 ``wait`` 返回 "allow"，代码就跑了，而**库里那行
            # ``decision`` 仍然是 None**。后果是审计记录说「未决定」、
            # 代码却执行了：出事后按记录查，会得出「没批准过，可它确实跑了」。
            #
            # 所以 ``wait`` 降级为纯粹的**阻塞点**（测试用它避免真睡），
            # 结论一律回库读 —— SQLite 是唯一真源（11.9.4 的同一条纪律）。
            self._wait(pending.credential)
        else:
            # **生产路径必须真的等。**
            #
            # 踩过的坑（真机测出来的，16 条单测全绿也没抓到）：上一版把
            # ``self._store.wait(...)`` 整个删掉了，理由是「结论要回库读」。
            # 但 ``main()`` **不传** ``wait``，于是走 else 分支时**根本没等**
            # —— 立刻读库、读到空、当场拒绝。用户点了「允许一次」，
            # 系统却说「未获批准（deny）」，而库里明明白白是 allow。
            #
            # 单测抓不到的原因：它们**全都**传了 ``wait`` 替身，
            # 于是那条「不传 wait」的分支一次都没被走到。
            # 这类 bug 只有真机跑才会暴露 —— 而它恰恰是最容易写错的一个。
            self._store.wait(pending.credential)

        decision = self._store.decide(pending.credential)
        if decision is None:
            # 等完了但库里还没人答 → 未应答 = 拒绝（不是「那就当允许」）
            decision = unattended_deny("等完仍无人应答")

        if decision != "allow":
            return f"未获批准（{decision}）"

        self._record_card(pending, card)
        return None

    def _send_card(self, pending: PendingApproval, *, to: str = "") -> str | None:
        """发确认卡，返回 message_id。

        走的是 :class:`~freeagent.services.localops.CardSender` **协议**
        （``sender.send_approval_card(...)``），不是 ``sender`` 模块里的函数 ——
        踩过的坑：写成 ``from .feishu.sender import send_approval_card``，
        而那个名字**根本不存在**（它是 sender 对象上的方法），
        于是 ``ImportError`` 发生在真正的发卡之前。
        与 11.9.4 复用**同一个协议**，不新造第二个发卡接口 ——
        两个接口必然漂移，而漂移的表现是「只读操作能发卡、委派发不出去」。

        这里**自己再判一次 None**，不靠 :meth:`check` 里的判空 ——
        判空在调用方、真正的解引用在本方法，两者一旦被拆开（或者将来有人
        直接调 ``_send_card``）就会变成 ``None.send_...``。
        """
        if self._sender is None:
            raise RuntimeError("没有发卡通道（sender=None）—— 无法请求批准")
        return self._sender.send_approval_card(
            # 收件人用 ``to``（本次的发起人）而不是 ``self._approver``。
            # 否则卡发给了一个人、库里记的发起人却是另一个，
            # 两边不一致又变成另一种错。
            open_id=to or self._approver,
            subject=pending.subject,
            detail=pending.detail or "",
            credential=pending.credential,
            ttl_seconds=self._context_ttl,
        )

    def _record_card(self, pending: PendingApproval, message_id: str | None) -> None:
        if message_id and self._store is not None:
            self._store.record_card(pending.credential, message_id)


def first_meaningful_line(text: str, limit: int = 160) -> str:
    """取第一行**有内容**的文本（剥 ANSI、跳过空行）。

    踩过的坑：直接 ``splitlines()[0]`` 会拿到 ``\\x1b[0m`` —— 一行纯转义码，
    于是报告里显示「失败：执行器退出码 1（[0m）」，等于什么都没说。
    """
    for raw in text.splitlines():
        line = _ANSI.sub("", raw).strip()
        if line:
            return line[:limit]
    return ""


class ToolCardSender(Protocol):
    """执行期发卡需要的**那一个方法**。

    刻意只声明用到的那一个：声明整个 sender 协议就得把
    :class:`~freeagent.services.localops.CardSender` 一起搬进来，
    而这里用不到它的别的能力。多声明一个方法就多一处
    「两个 sender 的签名会漂移」的机会。
    """

    def send_tool_card(
        self, *, open_id: str, subject: str, detail: str,
        credential: str, ttl_seconds: int,
    ) -> str: ...


class QuestionCardSender(Protocol):
    """提问卡需要的**那一个方法**。

    与 :class:`ToolCardSender` 分开声明，刻意不合并成一个 sender 协议：
    两者的按钮语义不同（一个有 allow/deny，一个**没有按钮**），合并会
    让「调用方到底需不需要实现发卡」这个问题变得要看实现才知道。
    """

    def send_question_card(
        self, *, open_id: str, subject: str, detail: str,
        credential: str, ttl_seconds: int,
    ) -> str: ...


class ServerFactory(Protocol):
    """造一个**隔离的** opencode 服务。

    返回类型刻意是 ``ContextManager[Any]`` 而不是 ``ContextManager[OpenCodeServer]``：
    要保证的安全属性是**能被 ``with`` 收口**（进程组、临时目录），
    而**不是**「必须是真 opencode」。写成后者，测试里的假服务就进不来 ——
    那逼着测试去继承真类，或者加 ``# type: ignore``，两条都不好。
    保持「必须可 with」，放开「里面是什么」。
    """

    def __call__(self, project: Path, command: str) -> ContextManager[Any]: ...


def _default_server_factory(project: Path, command: str) -> "OpenCodeServer":
    """生产用的工厂。**没有配置覆盖** —— 隔离是安全属性，不给开关。"""
    return OpenCodeServer(project=project, executable=command)


#: 单条委派允许的**工具调用次数**上限（设计文档 12.7.2）。
#:
#: 为什么需要它：审批本身**不能**防循环。一次失败的重试、一个跑偏的任务、
#: 或者一个执意反复重试的 agent，都会反复来问授权 —— 而每一次都可能拿到
#: 「允许」。于是**人是被同意淹没了，不是被拦住了**。
#:
#: 为什么这里必须是**代码**而不是 prompt：请 agent「别无限重试」是把安全
#: 押在一个可以忽略它的东西上���而这个上限由我们自己数、由我们自己停。
#:
#: 默认值 40：一条真实的多文件改动会用到几十次工具调用，而一个正常任务
#: 不该超过这个数。超了**不是失败**，是「这件事该拆开」——所以报错里
#: 会直接这么说。
DEFAULT_MAX_TOOL_CALLS = 40


def run_with_tool_gate(
    task,
    project: Path,
    brief: str,
    *,
    policy: DelegationPolicy,
    store: "ApprovalStore",
    sender: "ToolCardSender | QuestionCardSender | None",
    approver: str,
    server_factory: "ServerFactory | None" = None,
    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
) -> DispatchOutcome:
    """派一条委派，**执行期逐次授权**（设计文档 11.8.1）。
    与 :func:`_dispatch_one` 的分工：那是**无人值守**的老路（subprocess 一次
    跑到底），这一条是**人在环上**的新路。两条都保留，因为：

    - 老路已经真机验证过，删掉它等于把能用的东西弄没
    - 新路依赖飞书通道可用；通道不可用时**不能**静默退化成「无人值守跑」
      —— 那正是闸门形同虚设的样子。所以下面 ``sender is None`` 直接失败。

    循环：发指令 → 订阅事件 → 遇到 ``permission.asked`` 就发卡等回答 →
    把回答送回 opencode → 继续。遇到 ``question.asked`` 则发**无按钮**提问卡，
    等人**打字**回答（桥接把文本路由到 ``put_answer``），齐了再送回 opencode。

    提问没答就**停下会话并报失败**（无人答的提问下去就改代码，
    而那会被记成「完成」）。

    **超时即拒绝**（:meth:`ApprovalStore.wait` 的既有语义）。opencode 那侧
    会一直等下去，所以这里必须自己给期限 —— 沉默即拒绝，不是「等下去」。
    """
    if sender is None:
        # **必须给出路**，不能只说「不派发」（设计文档 12.7.2 的 R3：
        # 拒绝要看得见，且要告诉人下一步能做什么）。
        # 闸门默认开启之后，这条路径从「少数人没加旗标」变成了**没配飞书的
        # 人必经**—— 只报「不派发」会让他们以为程序坏了。
        return DispatchOutcome(
            ok=False,
            summary=(
                "没有飞书通道 —— 执行期授权需要人逐次批准，故不派发。\n"
                "  三条出路，任选其一：\n"
                "  1) 配好飞书通道（tools\\run_feishu.bat，会自检凭据）"
                "—— 推荐，闸门才能真正起作用\n"
                "  2) 确实只想无人值守跑：显式加 --no-tool-gate"
                "（此时第三道闸门不存在，agent 写文件、跑命令都无人过问）\n"
                "  3) 先看会派什么而不真跑：--dry-run"
            ),
        )

    make_server = server_factory or _default_server_factory
    context = ApprovalContext("remote", who=approver)
    handled: list[str] = []
    granted = 0
    session_id = ""
    #: 有提问没答。记下来是为了在结尾把它报成**失败**——
    #: 一次无人答的提问下去就算完成，那是最像成功的一种失败。
    unanswered = False
    #: 工具调用次数超限。**必须主动 abort**：否则 opencode 那边挂着一个
    #: 没人会回答的授权请求干等 —— 那正是「卡死」的定义。
    step_capped = False
    tool_calls = 0
    try:
        with make_server(project, policy.command) as oc:
            session_id = oc.create_session()
            oc.prompt_async(session_id, brief, model=policy.model)
            for kind, props in oc.events():
                if kind in ("session.idle", "session.error"):
                    break
                if kind == "question.asked":
                    # 提问**不计入授权统计**：它不是动作审批，
                    # 混进去会让「几次授权」这个数字不再可信。
                    q = question_from_event(props)
                    if q is None:
                        # 同样不能当成「已拒绝」悄悄跳过。
                        log.warning("【执行期】收到认不出的 question.asked：%r", props)
                        continue
                    note = _ask_one_question(oc, q, store=store, sender=sender,
                                             approver=approver, context=context)
                    if note:
                        handled.append(note)
                    else:
                        # 没答、或发卡失败。**不能继续干等**—— 无人答的
                        # 提问下去，就会在一个人根本不知道的事实上继续改代码。
                        handled.append(f"提问未答（{q.summary}）—— 已停下会话")
                        unanswered = True
                        break
                    continue
                if kind != "permission.asked":
                    # 其它事件（进度、消息）不处理。
                    continue
                req = permission_from_event(props)
                if req is None:
                    # 认不出的挂起请求：不能当成「已拒绝」悄悄跳过 ——
                    # 那会让 agent 干等，而人这边什么都不知道。
                    log.warning("【执行期】收到认不出的 permission.asked：%r", props)
                    continue
                # ── 步数硬上限：防「无限循环」的���效部件 ──────────────────
                #
                # **检查放在问人之前**，理由很实际：否则会发出一张卡，人正在
                # 点它，而我们会立刻中止会话 —— 那张卡点了没有任何后果，
                # 比不发更让人困惑。
                #
                # 审批本身**不能**防循环：每一次都可能拿到「允许」，于是人
                # 是被同意淹没了，不是被拦住了。所以这个数必须由我们自己数。
                if max_tool_calls > 0 and tool_calls >= max_tool_calls:
                    handled.append(
                        f"工具调用达到上限（{max_tool_calls} 次）—— 已中止"
                    )
                    step_capped = True
                    log.warning(
                        "【执行期】工具调用超过上限 %d 次，主动中止会话：task=%s",
                        max_tool_calls, getattr(task, "id", "?"),
                    )
                    try:
                        oc.abort(session_id)
                    except Exception as exc:  # noqa: BLE001
                        # 中止失败**不改变结论**：会话已被 `with` 关掉，
                        # 而我们要报的就是「超限停下」，不是「中止失败」。
                        log.warning("中止会话失败（仍将按超限报出）：%s", exc)
                    break
                decision = _ask_one_tool(oc, req, store=store, sender=sender,
                                         approver=approver, context=context)
                handled.append(f"{req.summary} → {'允许' if decision == 'allow' else '拒绝'}")
                tool_calls += 1
                if decision == "allow":
                    granted += 1
                oc.reply_permission(req.request_id, decision)
    except (ServerError, OSError) as exc:
        return DispatchOutcome(
            ok=False,
            summary=f"执行期授权链路失败：{type(exc).__name__}: {exc}",
            session_id=session_id or None,
            tool_calls=tuple(handled),
        )

    if unanswered:
        # 显式报失败。不把它装成「完成」—— 那会让一次
        # 无人答课的委派在记录里看起来和正常完成一样。
        return DispatchOutcome(
            ok=False,
            summary="执行期有提问没箅到答案 —— 已停下会话"
                    "（已答的部分不会被重放）",
            session_id=session_id or None, tool_calls=tuple(handled),
        )
    if step_capped:
        # 报成**失败**，且必须说清「这不是 bug，是这件事太大」。
        #
        # 刻意不装成「完成」：那会让一次被上限拦下的委派在记录里看起来和正常
        # 完成一样 —— 而实际上代码可能只改了一半。
        return DispatchOutcome(
            ok=False,
            summary=(
                f"工具调用达到上限（{max_tool_calls} 次）—— 已主动中止会话。\n"
                f"  这不是失败，是**这件事该拆开**：已完成的改动还在，"
                f"剩下的没做。\n"
                f"  下一步：把需求拆成几条更小的委派，"
                f"或者调高上限（DEFAULT_MAX_TOOL_CALLS）。"
            ),
            session_id=session_id or None, tool_calls=tuple(handled),
        )
    note = (f"执行期问了 {len(handled)} 次，允许 {granted} 次"
            if handled else "未触发授权请求")
    return DispatchOutcome(
        ok=True, summary=f"完成（{note}）",
        session_id=session_id or None, tool_calls=tuple(handled),
    )


def task_requester(task, fallback: str = "") -> str:
    """这条委派的**发起人**，没有就落回 ``fallback``。

    ## 为什么需要这个函数

    V1.16 之前，闸门拿到的是「白名单里**排序第一的人**」（
    ``sorted(cfg.allowed_users)`` 的首个，空白名单时为空串）。
    因此「只有发起人能批」的实际效果与文档描述**相反**：
    真正的发起人点不动，没发起的人反而能批。

    现在**优先**用事务上真的记着那个人（``delegate_requested_by``，
    飞书发起时由 ``_cmd_delegate`` 从 ``channel_ctx.sender_open_id`` 落下）。

    ## ``fallback`` 是兜底而不是规则

    只有**终端发起**的委派会走到它 —— 那条路径没有飞书身份，
    是**正常情况**。把「没有身份」当成「随便某个人」等于凭空造一个越权面，
    所以宁可落回旧行为，也不填一个假身份。

    另外：空串与 ``None`` 必须同等对待——空串不是「有身份」。
    同理，对象缺少这个属性时也不应撞（旧对象、别的实现）。
    """
    return (getattr(task, "delegate_requested_by", None) or "").strip() or fallback


def _ask_one_question(
    oc,
    req,
    *,
    store: "ApprovalStore",
    sender: "QuestionCardSender",
    approver: str,
    context: "ApprovalContext",
) -> str:
    """问一次（一次问一个），把答复送回 opencode。**返回一句话摘要**。

    与 :func:`_ask_one_tool` 的差别在两个地方：

    1. **答复是文本而不是 allow/deny**，因此等的是
       :meth:`~ApprovalStore.wait_complete` 而不是 :meth:`~ApprovalStore.wait`。
    2. **卡不带按钮**，因此发卡失败不能降级成「那就自己回答了」。

    第二点很重要：发卡失败必须返回失败，而不是自己给一个答案。
    一个人根本没看见的问题，若由程序自己回答，那就是一个
    **伪装成人答案**的提问单（它确实会驱动 agent 继续干活）。
    """
    policy = context.policy
    # 只登记一次、所有问题共用一个凭据，最终只发**一次**答复给 opencode。
    pending = store.request_question(
        subject=req.questions[0],
        detail=_question_detail(req, 0, ttl_seconds=policy.ttl_seconds),
        questions=req.questions,
        # 凭据**交给 store 生成**（它默认就是 q 前缀）。不自己传：
        # 一旦自己传了，就多了一个凭据的来源。
        ttl_seconds=policy.ttl_seconds,
        requested_by=context.who,
    )

    # **一问一卡，发完立刻等那一问。**
    #
    # 卡必须在循环**里**发。我第一版把「等」挪进循环却把「发」留在循环外，
    # 于是第 2 张卡永远发不出去 —— 死锁原因没变，只是挪了一层。
    collected: list[list[str]] = []
    for slot in range(len(req.questions)):
        try:
            message_id = sender.send_question_card(
                open_id=approver,
                subject=req.questions[slot],
                detail=_question_detail(
                    req, slot, ttl_seconds=policy.ttl_seconds),
                credential=pending.credential,
                ttl_seconds=policy.ttl_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - 发卡失败必须变成失败
            # 不自己回答：一个人没看见的问题若由程序代答，那是**伪装成人的
            # 答案**，而它确实会驱动 agent 继续改代码。
            log.warning("【执行期】发提问卡失败（%s）—— 不自己回答", exc)
            return ""
        store.record_card(pending.credential, message_id)
        got = store.wait_answer(
            pending.credential, slot, timeout_seconds=policy.ttl_seconds,
        )
        if got is None:
            # 这一问没答 → 整个提问算未完成。**不部分回传**：那会让 agent
            # 拿着一个欠缺的答案继续干活。
            log.info("【执行期】提问第 %d 问超时或过期", slot + 1)
            return ""
        collected.append(got)

    answers = collected
    try:
        oc.reply_question(req.request_id, answers)
    except ServerError as exc:
        return f"答案已收到，但送回 opencode 失败：{exc}"
    return f"提问 {len(answers)}/{len(req.questions)} 已答"


def _question_detail(
    req, current: int = 0, *, ttl_seconds: int = 0
) -> str:
    """提问卡的正文。**逐轮渲染**：第 ``current`` 张卡只讲当前那一问。

    ## 选项不会被写成「这题的选项」

    我们没有验明 opencode 的 ``options`` 是「全部问题的选项池」还是
    「每题各一组」—— 两者都是数组，但粒度未验。因此这里只说「可选项」。

    写成「选择一个」是一个**靠猜**的声称：若它实际按问题分组，那第 1 张卡上
    列出的可选项里就有一部分属于第 2 题。人照着选，选到的是另一个问题的
    答案 —— 而那会被 agent 当成对的答案用下去。
    """
    total = len(req.questions)
    lines = [f"**{current + 1}/{total}、{req.questions[current]}**"]
    if req.options:
        lines.append("")
        lines.append("可选项（" + "、".join(req.options) + "）")
    rest = req.questions[current + 1:]
    if rest:
        nxt = "、".join(
            f"{current + i + 2}. {q}" for i, q in enumerate(rest)
        )
        lines.append("")
        lines.append(f"_一次问一个，这一张卡下还有：{nxt}_")
    if ttl_seconds:
        lines.append("")
        lines.append(f"_{ttl_seconds}s 内不答按超时处理。_")
    return "\n".join(lines)


def _ask_one_tool(
    oc,
    req,
    *,
    store: "ApprovalStore",
    sender: "ToolCardSender",
    approver: str,
    context: "ApprovalContext",
) -> str:
    """问一次，回结论。**只回结论，不解释** —— 解释是卡片的事。

    ⚠️ **TTL 只在这里算一次**，卡片文案与等待时长**都用它**。
    两处各算一次的话，改了一处就会出现「卡上写 30 分钟、实际只等 10 分钟」——
    而用户是**按卡上那个时间**做决定的（11.9.4 同一条纪律）。

    踩过的坑（实测）：原先这里写 ``ApprovalPolicy.for_context(context)``
    —— 传进去的是 ``ApprovalContext`` **对象**而不是场景名，于是落到
    「认不出的场景一律最严」的分支，TTL 变成 **0**，卡上写着「0s 内有效」。
    而 ``store.wait()`` 又用自己默认的 600s，于是出现
    「卡上 0 秒、实际等了 10 分钟」这种自相矛盾的表现。
    """
    policy = context.policy
    pending = store.request_tool_call(
        permission=req.permission, paths=req.paths, diff=req.diff,
        suggested_always=req.suggested_always, context=context,
    )
    try:
        message_id = sender.send_tool_card(
            open_id=approver, subject=pending.subject,
            detail=pending.detail or "", credential=pending.credential,
            ttl_seconds=policy.ttl_seconds,
        )
    except Exception as exc:  # noqa: BLE001 - 发卡失败必须变成拒绝
        log.warning("【执行期】发卡失败（%s）—— 按拒绝处理", exc)
        return "deny"
    store.record_card(pending.credential, message_id)
    # 结论一律回库读（同 11.9.4 的纪律），不采信任何返回值。
    # ``timeout_seconds`` 必须**显式**给 policy 的 TTL：默认那个是
    # DEFAULT_TTL_SECONDS，与本场景的 TTL 不是一回事（实测 1800 vs 600）。
    return store.wait(pending.credential, timeout_seconds=policy.ttl_seconds)


def _dispatch_one(
    app,
    policy: DelegationPolicy,
    task,
    runner: Runner,
    *,
    gate: "Gate | None" = None,
) -> DispatchOutcome:
    """派一条。

    **任何失败都必须变成一条记录**，不能往上抛：
    * 往上抛会中断整个循环，别的够格任务再也派不到
    * 没记录的话这条事务永远停在 active，下次重跑还会再失败一次（幽灵）

    所以这里把白名单违规、**闸门拒绝**、找不到执行器、返回非零、输出不可解析
    全都收敛成同一个 :class:`DispatchOutcome`。

    ``gate is None`` 时**不派发**（除非 ``dry_run``）—— 理由见
    :class:`ApprovalGate`：委派是唯一终点在「本机执行代码」的能力，
    没有闸门就等于裸奔。测试也依赖这个默认值（``test_no_gate_means_no_run``）。
    """
    try:
        project = check_project_allowed(
            policy, task.project_path or "",
            known_names=authorized_names(policy.projects),
        )
    except FreeAgentError as exc:
        outcome = DispatchOutcome(ok=False, summary=f"白名单拒绝：{exc}")
        _record_result(app, task, outcome)
        return outcome

    brief = build_brief(task, task.intent, task.definition_of_done)
    title = task.title[:60]

    # ---- 闸门：派发**之前**必须有 allow 记录（设计文档 11.9.7 规则 1）----
    # 顺序刻意在「记派发出去了」**之前** —— 一条被拒绝的委派不该留下
    # 「已派出」的痕迹，否则日志会骗人，而且重跑时看不出它到底跑没跑。
    if gate is not None:
        # 只传事务上真的发起人；「没有就落回闸门自己的」由 check() 内部管
        # （``who = requester or self._approver``）。
        # 所以这条规则**只有一处**，调用点不需要知道兜底逻辑。
        refusal = gate.check(
            task_id=task.id, project=str(project), brief=brief,
            requester=task_requester(task),
        )
        if refusal is not None:
            outcome = DispatchOutcome(
                ok=False,
                summary=f"闸门拒绝（{refusal}）：未执行",
                detail=f"项目：{project}\n要求：{brief}",
            )
            _record_result(app, task, outcome)
            return outcome
    else:
        outcome = DispatchOutcome(
            ok=False,
            summary="没有闸门，拒绝派发：委派会执行代码，必须先有人批准",
            detail=f"项目：{project}",
        )
        _record_result(app, task, outcome)
        return outcome

    # 先记「派发出去了」再跑：崩了也不会重复派同一件事
    app.record_repo.append(
        task.id, RecordType.DELEGATION_DISPATCHED,
        f"派给 {policy.command}：{project}｜标题 {title}", app.clock.now(),
    )

    code, stdout, stderr = runner(
        _argv(policy, brief, title), project, policy.timeout
    )
    # 刻意**不加自动重试**。曾以为「全新目录首次运行会掉到主 agent 的付费
    # 模型、重跑就好」，实测两次都失败 —— 假设被证伪。真实规律是：
    # **需要写权限的任务会路由到唯一的 primary agent**（本机 Sisyphus，
    # 模型钉死 claude-opus-5），``--model`` 在那次路由里不生效，
    # 余额不足就 402。重试既治不了，又让每个真失败白跑一遍、耗时翻倍。
    if code != 0:
        # 截断放宽到 4000：**产物链才是事后能查的地方**，而 500 字符
        # 常常正好把错因切掉（实测那次 `APIError` 的 message 就在 500 字符
        # 之后）。终端那行另有 ``first_meaningful_line`` 做短显示，
        # 两边不必用同一个上限 —— 一个给人看，一个给事后查。
        detail = (stderr or stdout).strip()[:4000]
        outcome = DispatchOutcome(
            ok=False,
            summary=f"执行器退出码 {code}",
            detail=detail,
        )
    else:
        outcome = parse_opencode_output(stdout)

    _record_result(app, task, outcome)
    return outcome


def extract_error_message(text: str, limit: int = 240) -> str:
    """从 opencode 的失败输出里**挖出那句人话**。

    为什么需要专门挖：opencode 的错误是一条**单行** JSON，而
    :func:`first_meaningful_line` 对单行只能返回整行 —— 于是记录里会出现
    「执行器退出码 1（xxxxxxxxxxxx…）」这种**没有行动价值**的句子，
    真正的原因（``Upstream request failed: Insufficient account funds``）
    恰好在 300 字符之后被切掉。

    挖的顺序按 opencode 实际形状排：``error.data.message`` →
    ``error.message`` → ``message``。都挖不到就退回取首行
    （非 JSON 的失败，比如 shell 报错，仍然看得到）。

    挖不出来**不算错**：返回空串，调用方退回首行。这里不做任何猜测性解析。
    """
    raw = (text or "").strip()
    if not raw:
        return ""
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    if isinstance(error, dict):
        inner = error.get("data")
        if isinstance(inner, dict) and isinstance(inner.get("message"), str):
            return inner["message"][:limit].strip()
        if isinstance(error.get("message"), str):
            return error["message"][:limit].strip()
    if isinstance(payload.get("message"), str):
        return payload["message"][:limit].strip()
    return ""


def _record_result(app, task, outcome: DispatchOutcome) -> None:
    """把结果落成 artifact 版本 + 日志。**不自动把事务标成完成** ——
    opencode 说做完了，不等于你验收过了。留着 active，你自己看。"""
    now = app.clock.now()

    # **失败时必须把 detail 一起落进去。**
    #
    # 踩过的坑（真机测出来的）：原来是 ``summary or detail``，而 summary
    # 恒有值（「执行器退出码 1」），于是 **detail 从头到尾没进过库** ——
    # 真正的错因（「Upstream request failed: Insufficient…」）只出现在
    # 终端那一行，还被截断。下次失败时用户**没法自己查**。
    #
    # 这就是 11.8 那条教训「报错信息不进 stderr 等于没报」的残留：
    # 当时做到了「进 stdout」，但没做到「进库」。而库里才是事后能查的地方。
    body = outcome.summary or "（无输出）"
    detail = (outcome.detail or "").strip()
    if detail and detail != body:
        body += f"\n\n**失败详情**\n\n```\n{detail[:4000]}\n```"
    if outcome.session_id:
        body += f"\n\n<!-- opencode session: {outcome.session_id} -->"
    if outcome.model:
        # **实际用的模型**。刻意写进产物正文而不是只打日志 ——
        # 实测过委派从 deepseek 悄悄换成 opencode/big-pickle 而库里一个字都没有；
        # 写进产物，`/artifact` 就能一眼看见「这次到底谁干的」。
        body += f"\n<!-- 模型：{outcome.model} -->"
    if outcome.tool_calls:
        body += "\n<!-- 用到的工具：" + "、".join(outcome.tool_calls) + " -->"

    app.artifacts.create_draft(task.id, f"opencode 产出 {now:%m-%d %H:%M}", body)
    # 记录里也带上**错因**：``/note``、``/task`` 看到的应该是错因，
    # 而不是「执行器退出码 1」这种没有行动价值的句子。
    # 优先从 JSON 里挖 ``message``（见 :func:`extract_error_message` 的理由），
    # 挖不到再退回取首行（非 JSON 的失败，比如 shell 报错）。
    summary_text = outcome.summary or ""
    if not outcome.ok:
        reason = extract_error_message(outcome.detail or "") or first_meaningful_line(
            outcome.detail or ""
        )
        if reason and reason not in summary_text:
            summary_text = f"{summary_text}（{reason}）"
    app.record_repo.append(
        task.id,
        RecordType.DELEGATION_SUCCEEDED if outcome.ok
        else RecordType.DELEGATION_FAILED,
        (summary_text or outcome.detail or "")[:300],
        now,
    )
    app.tasks.note(
        task.id,
        ("执行器已产出结果，待你验收"
         if outcome.ok else "执行失败，可以 /note 记下原因后重派"),
    )
    # 本地已经落好了，**这时才**往飞书推。顺序不能反：飞书挂了也不能
    # 让本地少一条记录 —— 本地是权威，飞书只是通知渠道。
    _notify_chat(task, outcome)


def _notify_chat(task, outcome: DispatchOutcome) -> None:
    """把结果推回发起这条委派的飞书会话。

    刻意做到**完全不影响委派本身**：

    - 终端发起的（没有来源会话）→ 安静跳过，本该没有回推
    - 飞书没配 / 配错 / 网络不通 → **出声警告**（但绝不让执行器失败）

    理由：通知渠道挂了不等于任务没做完。**反过来也不成**：通知渠道通了
    不等于任务做完了 —— 所以文案里必须写明「待你验收」。

    **「飞书没配」要出声而不是静默**：执行器是独立进程，而手册要求
    「另开一个终端跑它」。于是 ``FEISHU_*`` 很可能只设在桥接那个终端里，
    这边读不到 —— 静默跳过的话，结果不会飞回飞书，而用户以为闭环是通的。
    """
    chat_id = getattr(task, "delegate_chat_id", None)
    if not chat_id:
        return
    try:
        from .feishu.config import load_config
        from .feishu.sender import FeishuSender

        config = load_config()
        if not config.app_id or not config.app_secret:
            # **必须出声，不能静默跳过。** 踩过的坑：这里原本直接 return，
            # 结果没飞回飞书，而用户以为闭环是通的。
            #
            # 真实原因**不是**「环境变量不跨终端」了 —— 凭据现在落在
            # ``<state_home>/feishu.env``，桥接和执行器各自 load_config()
            # 都能读到。实测过：环境里一个 FEISHU_* 都没有，照样读得到
            # app_id 与 32 位 secret。所以真正会缺凭据的只剩一种：
            # **执行器用了另一个 FREEAGENT_HOME**（或那个目录下没有 feishu.env）。
            # 提示必须指向这个，否则用户会去白费力气设环境变量。
            log.warning(
                "这条委派来自飞书会话 %s，但本进程没读到凭据，"
                "**结果没有推回飞书**（已照常落库）。"
                "凭据在 <FREEAGENT_HOME>/feishu.env —— 请确认执行器的 "
                "FREEAGENT_HOME 和桥接那边是同一个目录。", chat_id,
            )
            return
        header = "委派完成" if outcome.ok else "委派失败"
        # **失败时把错因也带上。** 踩过的坑：这里是 ``summary or detail``，
        # 而失败时 summary 恒为「执行器退出码 1」——于是飞书里收到的推送
        # 是一句**没有行动价值**的话，用户还得自己再去翻产物链。
        # 通知渠道的意义就是「你现在就知道发生了什么」。
        parts = [outcome.summary or ""]
        if not outcome.ok:
            reason = extract_error_message(
                outcome.detail or ""
            ) or first_meaningful_line(outcome.detail or "")
            if reason and reason not in (outcome.summary or ""):
                parts.append(f"原因：{reason}")
        text = f"【{header}】{task.title}\n\n" + "\n".join(p for p in parts if p)
        text += "\n\n—— 这条事务仍是「进行中」，**需要你自己验收**；"
        text += f"细节看 /task {task.id[:8]}"
        FeishuSender(config).send_text(chat_id, text[:1900])
    except Exception:
        # 通知失败**不该**变成委派失败。记一笔就好。
        log.warning("回推飞书失败（任务本身已完成）", exc_info=True)


def build_parser() -> argparse.ArgumentParser:
    """造命令行解析器。

    刻意提成模块级函数：``--tool-gate`` 的**默认值**是安全姿态的选择
    （开 = Strict，关 = Dangerous），而 ``main()`` 会去探 opencode 版本、
    真的派发 —— 直接测它就得先装 opencode、造飞书凭据，测的就不是默认值
    而是环境。默认值必须能被单独钉住。
    """
    parser = argparse.ArgumentParser(
        prog="python -m freeagent.delegate",
        description="委派执行器：把「已开始」的委派事务交给 opencode 完成。",
    )
    parser.add_argument("--db", type=Path, default=None, help="数据库文件路径")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="只列出将要派什么，一条都不跑（第一次用请先跑它）",
    )
    parser.add_argument(
        "--watch", type=float, default=0.0, metavar="秒",
        help="持续轮询，间隔指定秒数（默认只扫一遍）",
    )
    parser.add_argument(
        "--model", default=None, metavar="模型",
        help="覆盖 opencode 用的模型。留空用它的默认。"
             "实测 *-free 那批不需要账户余额。",
    )
    # 第三道闸门（执行期逐次授权）**默认开启**（设计文档 11.8.1）。
    #
    # 为什么翻过来：闸门的默认值决定的是**失效方向**。默认关 = 忘了加旗标
    # 就静默进入无人值守，而「跑起来了」比「跑不起来」危险得多
    # —— 参见 postmortem/0001：opencode 默认 allow，不传 --auto 也全放行。
    # 对照 QM 的三档姿态（Strict / Auto / Dangerous），**从前的默认值
    # 等价于 Dangerous**；现在是 Strict。
    #
    # `--tool-gate` **故意保留**：它在 README、tools\*.cmd、既有运维习惯里
    # 都写着，删掉会把这些命令行直接变成报错（而不是变安全）。
    # 它现在只是「把默认再说一遍」。
    gate_group = parser.add_mutually_exclusive_group()
    gate_group.add_argument(
        "--tool-gate", dest="tool_gate", action="store_true",
        help="（默认已开启）执行期逐次授权：agent 每要动手一次就发一张飞书卡"
             "等人批。这条只是把默认再说一遍，留着是为了兼容既有命令行。",
    )
    gate_group.add_argument(
        "--no-tool-gate", dest="tool_gate", action="store_false",
        help="**关掉**执行期逐次授权，回到无人值守的老路 —— "
             "那时第三道闸门不存在，agent 写文件、跑命令都无人过问。"
             "仅在明知风险、且没有飞书通道时用。",
    )
    parser.set_defaults(tool_gate=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # ── 版本闸门（规范性，见设计文档 11.8.1 的版本陷阱表）──────────────
    # 为什么在派发**之前**就挡：V1→V2 的 permission 字段名全变，旧配置被
    # **静默忽略**（不报错），闸门随之失效，失效方向恰好是默认 allow。
    # 那种情况下「跑起来了」比「跑不起来」危险得多。
    #
    # 刻意**放在 once() 之前**：`--dry-run` 也要过这一关 —— 预演的作用
    # 之一就是「照着接缝写的东西在真环境里成不成立」，版本都不对的话
    # 预演结果没有意义。
    probe_policy = _delegate_policy(build_app(args.db))
    mismatch = version_mismatch_reason(detect_opencode_version(probe_policy.command))
    if mismatch is not None:
        print(f"⚠ opencode 版本不可用 —— 不派发任何委派\n  {mismatch}")
        return 2

    def build_real_gate():
        """**真**闸门：落 pending → 发飞书卡 → 等人点。

        为什么必须在这儿建而不是让 :func:`run_once` 自己建：闸门要发卡，
        而发卡要 ``FeishuSender`` 与批准人 —— 那些是**进程级依赖**，
        不该被 ``run_once`` 猜。反过来如果这里不传，``run_once`` 收到
        ``gate=None`` 会**拒绝一切**（这是有意的安全底），于是
        ``python -m freeagent.delegate`` 会一条都派不出去、且只说「没有闸门」。

        这两处必须同时成立：默认拒绝 + 真实路径自带闸门。少任何一半，
        要么变成裸奔，要么变成什么都干不了。

        建不起来（没配飞书凭据 / 白名单为空）就返回 ``None`` → 全拒。
        **绝不**降级成「没闸门也先跑」：那正是闸门形同虚设的样子。
        """
        try:
            from .feishu.config import load_config
            from .feishu.sender import FeishuSender
        except ImportError as exc:  # pragma: no cover - 装了 [feishu] 就有
            print(f"⚠ 飞书通道不可用（{exc}）—— 委派闸门无法发卡，故不派发任何委派")
            return None
        try:
            cfg = load_config()
            cfg.check_ready()
        except Exception as exc:  # noqa: BLE001 - 配置错也要给出可读原因
            print(f"⚠ 飞书配置不可用（{exc}）—— 委派闸门无法发卡，故不派发任何委派")
            return None
        approver = next(iter(sorted(cfg.allowed_users)), "")
        if not approver:
            print("⚠ 飞书白名单为空，没人能批准 —— 不派发任何委派")
            return None
        store = ApprovalStore(build_app(args.db).conn)
        # argv 里带上**将要用的那条命令**：闸门要查的就是它有没有 `--auto` 之类
        # 绕过开关。用配置里的 command 拼，与 :func:`_argv` 同一个来源 ——
        # 两处各写一份的话，将来改了一处就出现「闸门查的不是真跑的那条」。
        policy_probe = _delegate_policy(build_app(args.db))
        return ApprovalGate(
            store=store, sender=FeishuSender(cfg), approver=approver,
            argv=[policy_probe.command, "run"], env=dict(os.environ), context="remote",
        )

    def current_approver() -> str:
        """唯一有资格批准的人。**取白名单第一人**（与 :class:`ApprovalGate` 同源）。

        刻意**只认一个人**而不是「白名单里谁都行」：设计文档要求
        「只有发起人能批」，而多批准人等于把「谁能指挥本机」的范围扩大。
        与既有闸门同一来源，将来换人只改一处。
        """
        try:
            from .feishu.config import load_config
            cfg = load_config()
            cfg.check_ready()
        except Exception:  # noqa: BLE001 - 取不到就空，由调用方拒绝
            return ""
        return next(iter(sorted(cfg.allowed_users)), "")

    def build_feishu_sender():
        """造发卡器。**失败返回 None**，由 :func:`run_with_tool_gate` 拒绝派发。

        绝不返回「一个不发卡的替身」—— 那会让执行期闸门静默失效。
        """
        try:
            from .feishu.config import load_config
            from .feishu.sender import FeishuSender
            cfg = load_config()
            cfg.check_ready()
            return FeishuSender(cfg)
        except Exception as exc:  # noqa: BLE001
            print(f"⚠ 飞书通道不可用（{exc}）—— 执行期授权需要人逐次批准，故不派发")
            return None

    def once() -> DispatchReport:
        policy = None
        if args.model is not None:
            base = _delegate_policy(build_app(args.db))
            policy = replace(base, model=args.model)
        # ``--dry-run`` 不碰闸门：预演的意义是「看看会派什么」，
        # 而闸门要发卡、要人点 —— 在这里发卡会让预演变成一次真请求。
        gate = None if args.dry_run else build_real_gate()
        if args.tool_gate:
            report = run_once_with_tool_gate(
                db_path=args.db, dry_run=args.dry_run, policy=policy,
                sender=build_feishu_sender(), approver=current_approver(),
            )
        else:
            report = run_once(
                db_path=args.db, dry_run=args.dry_run, policy=policy, gate=gate
            )
        print(f"扫到 {report.considered} 条，派 {report.dispatched} 条"
              f"（成功 {report.succeeded}／失败 {report.failed}）")
        for note in report.notes:
            print("  " + note)
        return report

    once()
    if args.watch > 0:
        print(f"持续轮询中（每 {args.watch:g} 秒，Ctrl+C 停止）…")
        try:
            while True:
                time.sleep(args.watch)
                once()
        except KeyboardInterrupt:
            print("\n已停止。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
