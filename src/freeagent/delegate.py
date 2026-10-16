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
import datetime
import functools
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
    detect_adapter,
    permission_from_event,
    question_from_event,
    stale_gate_note,
)
from .feishu.sender import ProgressCard

__all__ = ["Runner", "subprocess_runner", "run_once", "main",
           "ApprovalGate", "Gate", "run_with_tool_gate",
           "ToolCardSender", "ServerFactory",
           "SUPPORTED_OPENCODE_MAJORS", "SUPPORTED_OPENCODE_MAJOR",
           "KNOWN_OPENCODE_MAJORS",
           "parse_major_version",
           "detect_opencode_version", "detect_hermes_version",
           "version_mismatch_reason"]

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


#: 本仓**可派发**的 opencode 主版本集合 —— 由注册表推导，不硬编码。
#:
#: V1 与 V2 的 permission 模型字段名完全不同（``permission`` 对象 /
#: ``bash`` / ``task`` vs ``permissions`` 数组 / ``shell`` / ``subagent``），
#: 而 :mod:`freeagent.services.opencode_server` 要写配置。
#: **字段名对不上时旧配置会被静默忽略 —— 不报错，但闸门随之失效，
#: 失效方向恰好是最危险的那侧**（默认回到 allow）。
#:
#: 所以放行集合就是**注册表里所有 ``verified=True`` 的适配器**：验证过的
#: 才允许派发，而不是「认识就照跑」。现在 V1 与 V2 都对着真机验过
#: （V2 的实测记录见设计文档 11.8.1），所以**两个都放行**；未验证的
#: （hermes）与不认识的版本仍然 fail-closed（见
#: :func:`version_mismatch_reason`）。
#:
#: 判定见 :func:`version_mismatch_reason`。
SUPPORTED_OPENCODE_MAJORS: tuple[int, ...] = dispatchable_majors("opencode")

#: 可派发集合里**最高**的主版本。**只用于给人看的话**（如探不到版本时的
#: 提示、执行器探针里那句「期望形如 x.x.x」）——放行判定一律用上面的集合。
#: 若这里被拿去当放行依据，「V1 与 V2 都放行」就会退化成「只认最大那个」。
SUPPORTED_OPENCODE_MAJOR: int = max(SUPPORTED_OPENCODE_MAJORS, default=1)

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


def _detect_major_by_version_flag(command: str) -> int | None:
    """跑一次 ``<command> --version``，返回主版本号。

    刻意与 :func:`parse_major_version` 分开：**解析**是纯函数（可测），
    **调用**才有 IO。版本探测要能被测，就得先拆开。
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


def detect_opencode_version(command: str = "opencode") -> int | None:
    """跑一次 ``<command> --version``，返回主版本号。

    刻意**不在热路径上调用** —— 每次派发都多一个进程不合算，
    所以只在 :func:`main` 启动时探一次，探不到就拒绝（见下）。
    """
    return _detect_major_by_version_flag(command)


def detect_hermes_version(command: str = "hermes") -> int | None:
    """探测 hermes 的主版本。

    ⚠️ **探到版本不等于能派发。** hermes 目前只**登记**在注册表里
    （``verified=False``），它的传输层与 opencode 不同
    （stdio JSON-RPC vs HTTP+SSE），协议形状一个都还没验。
    探版本只是为了让 :mod:`freeagent.services.executor_probe` 能把
    「没装 / 装了但探不到 / 装了但没验过」这三种状态**分开说**。

    实测输出形如 ``Hermes Agent v0.21.3 (2026.9.14)``，
    :func:`parse_major_version` 会取到 ``0``。
    """
    return _detect_major_by_version_flag(command)


def version_mismatch_reason(detected: int | None) -> str | None:
    """版本可派发则返回 ``None``，否则给一句**能照着做**的原因。

    探不到版本（``None``）与版本不对同等对待 —— 两者都意味着
    「我们不知道自己在跟什么东西说话」，按设计文档的默认朝关开。

    放行集合是**注册表里所有 ``verified=True`` 的主版本**
    （:data:`SUPPORTED_OPENCODE_MAJORS`）——V1 与 V2 都对着真机验过，
    所以两个都放行。四种情形各给**不同**的话，因为该做的事不同：

    - **可派发的版本**（V1 / V2） → 放行
    - **探不到版本** → 查装没装 / 输出格式
    - **不认识的版本** → 只能升级本仓（或降级执行器）
    - **认识但未验证**（hermes） → 先对着真实实例验完适配器
    """
    if detected is None:
        accepted = "、".join(str(n) for n in SUPPORTED_OPENCODE_MAJORS) or "无"
        return (
            f"探不到 opencode 版本（本仓可派发的主版本：{accepted}）。"
            "可能没装，或 `opencode --version` 输出变了格式。"
            "本仓不猜 —— 说不上来在跟哪个版本说话时，宁可不派发。"
        )
    if detected in SUPPORTED_OPENCODE_MAJORS:
        return None

    adapter = adapter_for(detected, "opencode")
    if adapter is None:
        return (
            f"opencode 主版本是 {detected}，本仓**不认识**它"
            f"（已知的只有 {list(KNOWN_OPENCODE_MAJORS)}，可派发的有 "
            f"{list(SUPPORTED_OPENCODE_MAJORS)}，见设计文档 11.8.1 的"
            "版本陷阱表）。"
            "本仓拒绝派发而不是照跑 —— 未知版本的 permission 字段名会被"
            "**静默忽略**，闸门随之失效，且失效方向是回到 allow。"
            "请升级本仓，或把执行器降回已知版本。"
        )
    # 认识但 verified=False —— 当前只有 hermes 会走到这里
    return (
        f"opencode 主版本是 {detected}，本仓**认识**它但**未验证**接线"
        f"（见设计文档 11.8.1 的版本陷阱表）。"
        "本仓拒绝派发而不是照跑 —— 未验证版本的 permission 字段名可能"
        "**静默忽略**，闸门随之失效，且失效方向是回到 allow。\n"
        "要接它：对着一个真实实例逐项实测，填 "
        "`freeagent/services/executors.py` 里对应适配器的形状函数，"
        "再把 `verified` 改成 True，并同步设计文档 11.8.1。"
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
    if policy.agent:
        argv += ["--agent", policy.agent]
    # ``--variant`` **实测存在**（``opencode run --help``，1.18.34）：
    # ``--variant`` "model variant (provider-specific reasoning effort,
    # e.g., high, max, minimal)"。
    #
    # 刻意**不加** ``--thinking``：它是「显示思考过程」的 UI 开关，不是
    # 推理强度开关。把它当推理用会得到「看起来在思考、实际强度没变」。
    if policy.variant:
        argv += ["--variant", policy.variant]
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
    progress_sender: "ProgressCardSender | None" = None,
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
                    # 上次委派被强杀会留下闸门覆盖层（见 ``stale_gate_note``）。
                    # **只说、不自动删** —— 那份文件是项目级配置、真的在生效，
                    # 用户多半不知道它在；静默替他删是替他做了没要求的决定。
                    # 同一个项目只报一次，别在多轮委派里刷屏。
                    leftover = stale_gate_note(project)
                    if leftover and leftover not in notes:
                        notes.append(leftover)
                    outcome = run_with_tool_gate(
                        task, project,
                        build_brief(task, task.intent, task.definition_of_done),
                        policy=the_policy, store=store, sender=sender,
                        progress_sender=progress_sender,
                        # 同理：执行期的每一次授权也得知道**谁发起的这条委派**。
                        approver=task_requester(task, approver),
                        server_factory=server_factory,
                        # 落库进度投影（12.8）：WEB 面板读的就是这张表。
                        # 传的是适配器而不是 TaskRepo 本身，因为
                        # ``write_delegate_progress`` 还要把 ``now`` 换成一个
                        # 墙上时钟 —— 而那条时钟属于存储层，不属于这里。
                        progress_repo=_TaskRepoProgress(app.task_repo),
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
            project=str(project), brief=brief, context=ctx,
            # 归属（12.8）：这道闸门的 `task_id` 本来就是入参，顺手透传，
            # 不必再从 task 对象上取 —— `check` 拿得到的就是它。
            task_id=task_id or None,
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


def _as_dict(value: Any) -> dict[str, Any]:
    """取出 dict，**不是 dict 就当空的**。

    ## 为什么不写 ``x.get("k") if isinstance(x.get("k"), dict) else {}``

    那种写法把 ``get`` 调了两次，而**类型收窄不跨两次调用** —— 检查器在
    真值那一侧收窄了，取值那一侧拿到的还是 ``X | None``，于是每一个
    ``.get()`` 都报「可能为 None 的属性」。

    那是**运行绝不会错、类型却过不了**的报错，而本项目禁止
    ``# type: ignore``。所以这里收窄和取值是同一次调用 ——
    既过了类型检查，也少一次字典查找。
    """
    return value if isinstance(value, dict) else {}


def tool_event_label(kind: str, props: Any) -> str:
    """从一条 SSE 事件里挖出「正在做什么」，挖不出就返回 ``""``。

    ## 为什么宁可显示不出来，也不要显示错

    进度卡片是给人做判断用的。一张写着「正在 read」实际在 write 的卡片，
    比没有卡片更坏 —— 用户会据此判断「它还在读文件，没在乱改」。所以这里
    **只接受认得出的形状**，绝不按事件名猜工具。

    ## 为什么认形状而不是认事件名

    OpenCode 的事件类型名随版本变（``tool.execute.before`` /
    ``message.part.updated`` / ``tool_call`` 都见过），而「载荷里有个
    ``part.type == "tool"``」这类特征比事件名稳定。所以判据是**载荷形状**，
    事件名只当辅助。

    :return: 一句话说明（如 ``read D:/a.py``）；不是工具事件则空串。
    """
    if not isinstance(props, dict):
        return ""
    part = _as_dict(props.get("part"))
    name = (props.get("tool") or props.get("toolName")
            or part.get("tool") or part.get("name") or "")
    if not isinstance(name, str) or not name:
        return ""
    # 事件名里带 tool、或工具名就摆在顶层，才算工具事件。两者至少命中
    # 一个 —— 否则一条 `message.part.updated`（纯文本增量）会被当成
    # 工具调用报出去，而「显示错」比「不显示」更坏。
    looks_like_tool = (
        part.get("type") == "tool"
        or "tool" in kind.lower()
        or props.get("tool") is not None
        or props.get("toolName") is not None
    )
    if not looks_like_tool:
        return ""
    # 参数在 ``part.state.input`` 里（实测形状），但也有版本放顶层
    # ``state``。**两个都找** —— 找错了就退化成「正在执行 read」，
    # 丢掉文件名，而那正是用户唯一想看的部分。
    state = _as_dict(part.get("state")) or _as_dict(props.get("state"))
    target = _as_dict(state.get("input")) or _as_dict(state.get("args"))
    arg = (target.get("filePath") or target.get("path")
           or target.get("pattern") or target.get("command") or "")
    if not isinstance(arg, str) or not arg:
        return f"正在执行 {name}"
    return f"正在执行 {name}：{arg[:80]}"


class ToolCardSender(Protocol):
    """执行期发卡需要的**那一个方法**。

    刻意只声明用到的那一个：声明整个 sender 协议就得把
    :class:`~freeagent.services.localops.CardSender` 一起搬进来，
    而这里用不到它的别的能力。多声明一个方法就多一处
    「两个 sender 的签名会漂移」的机会。
    """

    def send_tool_card(
        self, *, open_id: str, subject: str, detail: str,
        credential: str, ttl_seconds: int, reply_to: str = "",
    ) -> str: ...


class QuestionCardSender(Protocol):
    """提问卡需要的**那一个方法**。

    与 :class:`ToolCardSender` 分开声明，刻意不合并成一个 sender 协议：
    两者的按钮语义不同（一个有 allow/deny，一个**没有按钮**），合并会
    让「调用方到底需不需要实现发卡」这个问题变得要看实现才知道。
    """

    def send_question_card(
        self, *, open_id: str, subject: str, detail: str,
        credential: str, ttl_seconds: int, reply_to: str = "",
    ) -> str: ...


class ProgressCardSender(Protocol):
    """进度卡片需要的三个方法。

    与 ToolCardSender / QuestionCardSender 分开声明：进度卡片的语义是
    「持续更新同一个文本组件」，与前两者「发一张等回答的卡」不同。
    合并会让「调用方到底需不需要实现进度更新」这个问题变得要看实现才知道。

    返回 :class:`~freeagent.feishu.sender.ProgressCard` 而不是裸
    ``card_id``：授权卡要挂到进度卡下面需要的是 ``message_id``，而两个
    都是 ``str``，传错不报错（详见该类文档）。
    """

    def send_progress_card(
        self, *, open_id: str, title: str, project: str, chat_id: str,
    ) -> ProgressCard: ...

    def update_progress_text(
        self, *, card_id: str, text: str, sequence: int,
    ) -> None: ...

    def close_progress_stream(
        self, *, card_id: str, final_text: str, sequence: int,
    ) -> None: ...


class ProgressRepo(Protocol):
    """进度**落库投影**需要的两个方法（设计文档 12.8 / V1.35）。

    刻意是协议而不是直接吃 :class:`~freeagent.storage.repos.TaskRepo`：
    这条路上有两类调用方 —— 真执行器（要跨进程落库）与
    :func:`run_with_tool_gate` 的测试（只想断言文本，不想有库）。
    写成具体类的话，测试就得为「不做落库」这件事造一个假库，
    而那正好是「不传」本来要表达的。

    与 ProgressCardSender **分成两个协议**是同一个理由：飞书那条路是
    「推给用户看」，落库这条是「给 WEB 面板读」。把它们合成一个，
    就会出现「为了在测试里不做落库而必须实现一个假飞书 sender」的荒唐。
    """

    def write_delegate_progress(
        self, *, task_id: str, status: str, recent: str,
        tool_calls: int, allowed: int, project: str,
    ) -> None: ...

    def clear_delegate_progress(self, task_id: str) -> None: ...


class ServerFactory(Protocol):
    """造一个**隔离的** opencode 服务。

    返回类型刻意是 ``ContextManager[Any]`` 而不是 ``ContextManager[OpenCodeServer]``：
    要保证的安全属性是**能被 ``with`` 收口**（进程组、临时目录），
    而**不是**「必须是真 opencode」。写成后者，测试里的假服务就进不来 ——
    那逼着测试去继承真类，或者加 ``# type: ignore``，两条都不好。
    保持「必须可 with」，放开「里面是什么」。
    """

    def __call__(self, project: Path, command: str) -> ContextManager[Any]: ...


def plugin_extra_env(plugins: str) -> dict[str, str]:
    """插件模式 → 子进程环境变量。**纯函数**（无 IO、可直接测）。

    ``off`` 返回 ``{"OPENCODE_PURE": "1"}``：opencode 在**加载插件之前**
    就把 ``cfg.plugin_origins`` 整丢掉（``packages/opencode/src/plugin/index.ts``
    的 ``Flag.OPENCODE_PURE ? [] : …``）。实测有效：带这个变量起实例时
    ``GET /mcp`` 返回空 —— 插件注入的 5 个 MCP 服务器全部消失。

    ⚠️ 别名 ``OPENCODE_PURE`` 不是笔误，opencode 自己就把它叫 pure
    （``flag.ts`` 的 ``OPENCODE_PURE``，TUI 侧同一个变量）。

    ``inherit`` 返回空 dict —— **刻意不传任何变量**。传一个「什么都不做」
    的变量看起来更对称，但那样就分不清「忘了配」与「配了等于没配」。

    未知取值**报错而不是当 inherit**：``true`` / ``none`` / ``""`` 这类值一旦
    被当成「继承」，用户以为自己关了插件、实际插件全在，而且**界面上看不出
    任何异常** —— 那是最难发现的一类失效。配置层已经校验过一遍，这里再挡
    一次是因为这个函数是纯函数、可以零成本地失败关闭。
    """
    if plugins == "off":
        return {"OPENCODE_PURE": "1"}
    if plugins == "inherit":
        return {}
    raise ValueError(
        f"未知的插件模式：{plugins!r}（只认 'inherit' 或 'off'）"
    )


def _default_server_factory(
    project: Path, command: str, *, plugins: str = "inherit"
) -> "OpenCodeServer":
    """生产用的工厂。

    跑**系统 opencode**（真实全局配置 → 用户原生的全局插件 / MCP / 技能，
    离线、零重装），并用项目级覆盖层强制本项目的审批闸门。不再另起隔离
    世界去复制用户配置（那样会重装、且离了网就坏）。

    ``plugins`` 只决定**要不要加载用户的插件**（``delegate.plugins`` 配置项）：

    - ``inherit``（默认）—— 与 V1.31 以来完全一致，一个字节都不多传。
    - ``off`` —— 加 ``OPENCODE_PURE=1``，插件与其注入的 MCP / 技能全部不加载。

    闸门（``apply_gate=True``）**两种模式下一律开着**：这个开关调节的是
    「agent 能用什么」，不是「用之前问不问」。把它和闸门绑在一起会让人
    以为关插件能顺便提高安全 —— 那是两件事。
    """
    return OpenCodeServer(project=project, executable=command,
                          isolate=False, apply_gate=True,
                          extra_env=plugin_extra_env(plugins))


class _TaskRepoProgress:
    """:class:`ProgressRepo` 的生产实现：把投影写进 ``delegate_progress``。

    刻意是**一个类**而不是两段 lambda：``run_with_tool_gate`` 拿到的是
    协议，而这一层负责**换时钟** —— 存储层要的是 ``datetime``（它自己
    决定 ISO 格式），而执行器这一侧的时间是一个 ``Callable[[], float]``
    的 monotonic 计数器（见 ``now=`` 参数，那是给**超时判定**用的，
    拿它写时间戳会写出一个从开机起算的数）。

    ⚠️ 所以这里用的是 ``datetime.now()``，**不是** ``now()``。两者混用
    的症状很隐蔽：进度行的时间戳会变成「开机后多少秒」，
    而 WEB 面板拿它跟真实时间比陈旧，永远算出「刚刚才动过」——
    于是一条三天前的僵尸进度会永远显示「正在进行」。
    """

    __slots__ = ("_repo",)

    def __init__(self, repo) -> None:
        self._repo = repo

    def write_delegate_progress(
        self, *, task_id: str, status: str, recent: str,
        tool_calls: int, allowed: int, project: str,
    ) -> None:
        self._repo.set_delegate_progress(
            task_id=task_id, status=status, recent=recent,
            tool_calls=tool_calls, allowed=allowed, project=project,
            now=datetime.datetime.now(),
        )

    def clear_delegate_progress(self, task_id: str) -> None:
        self._repo.clear_delegate_progress(task_id)


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

#: 进度卡片的**最小更新间隔**（秒），默认 5 秒。
#:
#: 为什么需要节流：飞书 CardKit 流式更新 API 有频率限制（50 次/秒），
#: 但每条消息更新一次就是一次 HTTP 请求。一个跑 30 分钟的 agent 可能
#: 问 40 次授权，每次都更新卡片的话就是 40 次请求 —— 不算多，但
#: 大部分更新用户根本看不出来区别（"正在改第 3 个文件" 和 "正在改
#: 第 4 个文件" 对人来说差不多）。5 秒间隔足够让用户感知到"它在动"，
#: 又不会浪费请求配额。
#:
#: 关键节点（第一次工具调用、session.idle、session.error、step_capped、
#: time_capped）立即更新，不受此间隔限制 —— 这些节点用户需要立刻看到。
MIN_PROGRESS_INTERVAL = 5.0

#: 单条委派的**总时长**上限（秒），默认 30 分钟。
#:
#: 为什么步数上限还不够：它限的是**次数**，而一个持续吐事件的 agent
#: （一直报进度、反复读写但不问授权，或干脆在无关的工具上打转）总时长
#: **无上界**。``events()`` 那个 ``timeout=600`` 是 **socket 空闲超时** ——
#: 它只在「600 秒没动静」时抛异常，而「一直有动静」正是它管不到的那种。
#:
#: 两者是互补的，不是重复：
#: - 步数上限管「做了太多件事」
#: - 时长上限管「这件事花了太久」
#:
#: 两者都在**代码**里数，不靠请 agent 自觉。
DEFAULT_MAX_DISPATCH_SECONDS = 30 * 60


def run_with_tool_gate(
    task,
    project: Path,
    brief: str,
    *,
    policy: DelegationPolicy,
    store: "ApprovalStore",
    sender: "ToolCardSender | QuestionCardSender | None",
    progress_sender: "ProgressCardSender | None" = None,
    approver: str,
    server_factory: "ServerFactory | None" = None,
    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
    max_seconds: float = DEFAULT_MAX_DISPATCH_SECONDS,
    now: Callable[[], float] = time.monotonic,
    progress_repo: "ProgressRepo | None" = None,
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

    ## 进度卡片（设计文档 11.16）

    ``progress_sender`` 非 None 时，在执行前发一张 CardKit 2.0 流式卡片，
    每次 ``permission.asked`` 更新内容（节流），结束关流式设最终内容。

    为 None 时完全不发卡 —— 向后兼容，不配进度卡片的部署不受影响。
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

    # ``partial`` 而不是给工厂加位置参数：:class:`ServerFactory` 协议是
    # ``(project, command)``，测试注入的假工厂就按这个签名写。加一个**带默认值的
    # 关键字参数**仍然满足该协议，而 ``partial`` 让「插件模式」这件事在
    # 生产路径上可见 —— 不用把配置读两遍，也不用模块级全局。
    make_server = server_factory or functools.partial(
        _default_server_factory, plugins=policy.plugins
    )
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
    #: 用户主动叫停。**与 step_capped / time_capped 分开报**，因为给用户的
    #: 建议完全不同：那两条是「系统保护你，这件事太大/卡住了」，这一条是
    #: 「你自己叫停的」—— 不该被说成失败，更不该被说成完成。
    user_stopped = False
    #: 总时长超限。与 :data:`DEFAULT_MAX_TOOL_CALLS` **互补**：那个限次数，
    #: 这个限时长。一个一直有动静的 agent 次数上不去，但时间能拖很久。
    time_capped = False
    #: 会话以 ``session.error`` 收场时的错误对象（``properties`` 原样）。
    #:
    #: 真机踩过（2026-10-10）：模型调用被拒（403 FreeTierError）时 opencode
    #: 发 ``session.error``，而这里原先把 idle / error 无差别 break，循环后
    #: 的统计标志一个都没命中，于是落到「完成」—— 一次失败的委派在库里
    #: 长得和成功一模一样，用户被通知「待你验收」而界面上什么都没有。
    #: 所以 error 必须单独记、单独报。
    session_error: dict[str, Any] | None = None
    #: 默认 ``monotonic`` 而不是墙钟：墙钟会被 NTP 校时或用户改时间影响，
    #: 那样「已经跑了 30 分钟」可能突然变成负数，于是上限失效。
    #:
    #: **刻意做成可注入**：靠「传一个很小的 max_seconds 然后等它过期」来测
    #: 是靠运气的 —— 我第一版就写了个 ``0.001``，而假 server 建会话只要微秒，
    #: 循环第一次转起来时期限**还没到**，于是测试红成了一个不存在的 bug。
    #: 注入时钟让这个上限**确定性**可测，不用 sleep、不 flaky。
    deadline = (now() + max_seconds) if max_seconds > 0 else None

    def _abort_quietly(oc) -> None:
        """中止会话。**中止失败不许改变结论**。

        会话随后会被 ``with`` 关掉，而我们要报的是「超限停下」，
        不是「中止失败」—— 反过来会把一次**成功的保护**说成故障。
        """
        try:
            oc.abort(session_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("中止会话失败（仍将按上限报出）：%s", exc)

    # ── 进度卡片状态（设计文档 11.16）────────────────────────────────
    #
    # **刻意提到 try 之外**。这不是为了好看：ServerError / OSError 的
    # 收口路径也要关闭流式卡片，而那时 `with` 内部已经抛过错。若这些
    # 变量在 `with` 里才初始化，收口代码读到的就是「未赋值的局部变量」，
    # 「关卡片」自己先抛 UnboundLocalError —— 把真正的错误盖掉，
    # 于是「飞书抖了一下」变成一条谁也看不懂的失败。
    progress_msg_id: str | None = None
    #: 进度卡在**聊天里**那条消息的 id。授权卡挂到它下面靠这个
    #: （设计文档 11.16）。与 ``progress_msg_id``（CardKit 实体 id）**不是
    #: 同一个东西**，而两者都是 str —— 所以必须分成两个变量名，
    #: 拿错不会报错，只会让授权卡挂不上（症状看起来像「功能没做」）。
    progress_anchor_id: str | None = None
    progress_seq = 0
    last_progress_update: float | None = None
    progress_closed = False
    #: 见过第一个工具事件。第一个**强制**更新（不吃节流）—— 卡片刚发
    #: 出去时正文只写着「正在启动」，用户需要马上看到它真的动起来了。
    seen_tool_event = False

    def _note_tool_event(kind: str, props: Any) -> None:
        """把一条工具事件投影成卡片上的一行「最近操作」。

        **只投影,不判定**。判定「这一步要不要问人」是
        :func:`permission_from_event` 的事；这里只是「让人看见它在动」。
        两者混在一起就会出现「为了显示进度而必须理解载荷」—— 而载荷形状
        随 opencode 版本变，于是每次升级都可能把进度显示弄坏。

        形状认不出就**返回空串**（不显示），而不是猜一个工具名显示错的东西
        —— 一张写着「正在 read」实际在 write 的卡片，比没有卡片更坏。
        """
        nonlocal seen_tool_event
        label = tool_event_label(kind, props)
        if not label:
            return
        first = not seen_tool_event
        seen_tool_event = True
        _update_progress(label, force=first)

    def _card_body(status: str, recent: str) -> str:
        """进度卡片的正文。**一处成形** —— 形状散在多处就会漂。"""
        return (
            f"**项目**：{project}\n\n"
            f"**状态**：{status}\n\n"
            f"**最近操作**：{recent}\n\n"
            f"**工具调用**：{tool_calls} 次（允许 {granted} 次）"
        )

    def _write_progress_row(status: str, recent: str) -> bool:
        """**只**落库进度投影，不碰飞书卡。返回**有没有写成**。

        与 :func:`_update_progress` 分开是因为**两者触发的时机不同**：
        飞书卡的更新必须挤在节流窗后面（省 API 配额），而落库那行是
        「起跑线」——它在 prompt_async 之后立刻要写，好让 WEB 面板
        在第一个工具事件到来之前（可能要十几秒）就显示得出来。

        早期版本把起跑线也走 :func:`_update_progress`，于是多推了**一张**
        卡更新 —— 而那张卡的内容与建卡时的初始正文「正在启动」完全相同。
        症状是 sequence 从 2 变成 3 而内容没变：飞书不报错，
        只是白花一次配额，而「sequence 为什么跳了一格」会让人以为
        有一次更新被吞了。**展示层不该为数据库的存在多花一次调用。**

        返回值给节流用：**失败也算「这一格用掉了」**。理由是写失败
        多半是数据库被占（另一个进程正在读），那种情况下继续写下去
        只会把同一个错重复几十次；而「这一格丢了」与「卡住不动」
        是同一类可接受的后果（有陈旧判定兜底）。
        """
        if progress_repo is None:
            return False
        try:
            progress_repo.write_delegate_progress(
                task_id=getattr(task, "id", "") or "",
                status=status, recent=recent,
                tool_calls=tool_calls, allowed=granted,
                project=str(project),
            )
        except Exception:  # noqa: BLE001
            log.warning(
                "【执行期】进度落库失败（task=%s，仅影响 WEB 面板）",
                getattr(task, "id", "?"), exc_info=True,
            )
        return True

    def _update_progress(recent: str, *, status: str = "正在执行",
                         force: bool = False) -> None:
        """更新进度：**节流 + 两个去处**（飞书卡、落库投影）。**一个收口点**。

        ## 失败不许影响执行

        两边都是**展示层**。飞书抖动、sequence 冲突、卡片被删、
        数据库被另一个进程锁住 —— 任何一种都不该把一次能跑完的委派
        报成失败。所以这里吞掉一切异常只留日志：**这条进度的命，
        比不上这次委派的命**。

        ## 节流按时间，不按次数

        OpenCode 的工具调用密度不固定（连着读三个文件，也可能静默
        30 秒）。按次数节流会漏掉「长时间没动静之后的那一次」，
        而那恰恰是最该让人看到的一次（「它卡住了？」）。

        ## 落库**独立于**飞书卡（V1.35 的唯一偏差点）

        早先这里是 ``if progress_sender is None or not progress_msg_id:
        return`` —— 那一行在 V1.35 之前是对的（唯一的去处是飞书卡）。
        现在有两个去处了，而「没配飞书」不该等于「WEB 面板也瞎着」：
        纯终端部署没有飞书通道，而它照样开着 Web 界面。

        所以落库挪到了那个门槛**之外**：``progress_sender`` 为 None 时
        卡片那条路照旧不动，但库里照写。飞书的门槛与 sequence 规则
        一个字没改。

        :param force: 关键节点强制更新（第一个工具事件、提问中、首次授权）。
        """
        nonlocal progress_seq, last_progress_update
        stamp = now()
        if (not force and last_progress_update is not None
                and stamp - last_progress_update < MIN_PROGRESS_INTERVAL):
            return
        # ⚠️ 顺序**不是**随意的 —— 先落库意味着「WEB 先看到、飞书后跟上」，
        # 而倒过来会让飞书显示着一个 WEB 还没记下的进度。
        #
        # 落库那一格用掉了就推进节流，不等飞书卡那条路 ——
        # 否则纯终端部署（没有 progress_sender）下节流永远不生效，
        # 于是每一次工具事件都写一次库，而设计文档 12.8 明确禁止
        # 「把 SQLite 写放大到工具调用密度」。
        wrote = _write_progress_row(status, recent)
        if progress_sender is None or not progress_msg_id:
            if wrote:
                last_progress_update = stamp
            return
        try:
            progress_sender.update_progress_text(
                card_id=progress_msg_id,
                text=_card_body(status, recent),
                sequence=progress_seq,
            )
        except Exception:  # noqa: BLE001
            log.warning(
                "【执行期】进度卡片更新失败（task=%s）",
                getattr(task, "id", "?"), exc_info=True,
            )
            return
        progress_seq += 1
        last_progress_update = stamp

    def _clear_progress() -> None:
        """清掉这条委派的落库进度行。**幂等、不抛**。

        与 :meth:`_close_progress_stream` 并列但**不是**它的附属：
        飞书卡发没发过与库里该不该留行是两件事 —— 没配飞书的那条路
        没有卡，却同样需要清行。

        「不抛」的理由：四个收口点都会调它（正常结束 / error / 超限 /
        用户叫停），而重复清理是常态。让它抛会把「已经清干净了」
        说成故障 —— 那正是本函数要防的那类误导。
        """
        if progress_repo is None:
            return
        try:
            progress_repo.clear_delegate_progress(getattr(task, "id", "") or "")
        except Exception:  # noqa: BLE001
            log.warning(
                "【执行期】清进度行失败（task=%s，残留行会被按陈旧忽略）",
                getattr(task, "id", "?"), exc_info=True,
            )

    def _close_progress_stream(final_text: str) -> None:
        """关闭流式模式并设置最终内容。**幂等**。

        幂等靠 ``progress_closed``：正常收口、异常收口、以及 return 级联
        都可能走到这里，而重复关闭会让飞书报「sequence 不递增」。
        """
        nonlocal progress_seq, progress_closed
        if progress_sender is None or not progress_msg_id or progress_closed:
            return
        progress_closed = True
        try:
            progress_sender.close_progress_stream(
                card_id=progress_msg_id,
                final_text=final_text,
                sequence=progress_seq,
            )
            progress_seq += 1
        except Exception:  # noqa: BLE001
            log.warning(
                "【执行期】进度卡片关闭失败（task=%s）",
                getattr(task, "id", "?"), exc_info=True,
            )

    try:
        with make_server(project, policy.command) as oc:
            session_id = oc.create_session()
            # 工作模式失效守卫（设计文档 11.13.8 / 11.14）。
            # OpenCode 对不存在的 agent 是「查表取不到就静默用默认」，
            # 所以必须在这里显式判：失效就**响亮失败**，而不是派出去却
            # 用了别的模式（那正是「看起来成功、实则退化」）。
            if policy.agent:
                try:
                    _live_agents = oc.list_agents()
                except Exception as exc:  # noqa: BLE001
                    # 取不到列表 ≠ agent 失效。连不上时不误杀，只告警。
                    log.warning("【执行期】取 /agent 失败，跳过工作模式校验：%s", exc)
                else:
                    from .services.oc_selection import agent_is_executable
                    if not agent_is_executable(_live_agents, policy.agent):
                        oc.abort(session_id)
                        # 这一路径上还没有写过任何进度行（起跑线那行在
                        # prompt_async 之后），但仍调一次清理：它幂等，
                        # 而「万一以后把起跑线挪到前面」这件事不该让
                        # 这里变成一个必须记住的隐含约束。
                        _clear_progress()
                        return DispatchOutcome(
                            ok=False,
                            summary=(
                                f"工作模式「{policy.agent}」已失效：该项目可能没有它，"
                                "或 OpenCode 已更新。请到「编程」页签重选工作模式后再派发。"
                            ),
                        )
            oc.prompt_async(session_id, brief, model=policy.model,
                            agent=policy.agent, variant=policy.variant)
            # ── 进度卡片（设计文档 11.16）────────────────────────────────
            #
            # 发卡时机：prompt_async 之后、事件循环之前。此时 opencode 已经
            # 开始跑，但还没产生任何事件 —— 卡片发出去时显示"正在启动..."，
            # 后续事件来了再更新内容。
            #
            # 为什么不在 prompt_async 之前发卡：那时 opencode 还没开始跑，
            # 卡片发出去会显示"正在启动..."，但用户可能等很久才看到第一次
            # 更新。放在 prompt_async 之后，卡片发出去时 opencode 已经在跑了，
            # 第一次更新会很快到来。
            #
            # 无 progress_sender 时不发卡 —— 向后兼容，不配进度卡片的部署
            # 不受影响。
            # **有 approver 就发**，不再要求 delegate_chat_id。
            #
            # 原先的条件是 ``and task.delegate_chat_id``，理由是「没有飞书
            # 会话就别发卡」。真机暴露了这条是错的：Web 端派的委派
            # delegate_chat_id **本来就是 None**（那张截图里的
            # 「了解项目进展」就是这样），于是 Web 派的活**永远没有进度卡**，
            # 而「飞书只有权限审批、没有进展」正是这么来的 ——
            # 权限卡走 open_id 私聊，进度卡却要求会话，两者条件不一致。
            #
            # 现在：有会话发会话（群/话题里能看到），没会话就私聊发给
            # approver。**发卡的落点由「谁在等这条结果」决定**，
            # 而不是由「这条委派从哪个入口建」决定 —— 后者与收卡人无关。
            if progress_sender is not None:
                try:
                    progress_card = progress_sender.send_progress_card(
                        open_id=approver,
                        title=task.title,
                        project=str(project),
                        # 有会话发会话，没有就退化成私聊（见上面的判据）。
                        # **传 ``or ""`` 而不是 None** —— ``Task.delegate_chat_id``
                        # 的类型就是 ``str | None``，而协议里收的是 ``str``；
                        # 原样传 None 会让类型收窄在 sender 里失配。
                        chat_id=task.delegate_chat_id or "",
                    )
                except Exception:  # noqa: BLE001
                    # **发卡失败不许变成委派失败。** 展示层挂了就降级成
                    # 「没卡片」，委派照跑 —— 反过来一次飞书抖动会让代码
                    # 改动没跑成，而用户完全看不出这两件事的因果关系。
                    # （FeishuError 不属于 ServerError/OSError，不接住的话
                    # 它会直接冲出本函数，谁也拦不住。）
                    log.warning(
                        "【执行期】发进度卡片失败，降级为无卡片（task=%s）",
                        getattr(task, "id", "?"), exc_info=True,
                    )
                    progress_msg_id = None
                    progress_anchor_id = None
                else:
                    progress_msg_id = progress_card.card_id
                    progress_anchor_id = progress_card.message_id
                    # **发卡用掉了 sequence=1**（见 FeishuSender 的创建载荷），
                    # 所以第一次更新必须从 2 起。写成 1 的话第一次更新会被
                    # 飞书判成时序倒流，而表现是「内容静默不更新」——
                    # 卡停在那句「正在启动」上，没有任何报错。
                    progress_seq = 2
                    log.info(
                        "【执行期】已发进度卡片：task=%s card_id=%s anchor=%s",
                        getattr(task, "id", "?"), progress_card.card_id[:12],
                        progress_card.message_id[:12],
                    )
            # ── 起跑线：落库投影的第一行（12.8 / V1.35）────────────────────
            #
            # 刻意**紧接发卡之后**、事件循环之前 —— 与卡片的初始内容
            # （"正在启动..."）同一时刻、同一份数据。
            #
            # 为什么要这一行而不是等第一个工具事件：那一来可能要十几秒，
            # 而这段时间里 WEB 面板会**显示不出这条委派** —— 用户刚点
            # 「开始做」就看到空面板，与「没派出去」长得一模一样。
            #
            # 走 `_write_progress_row`（只落库、不推卡）而不是
            # `_update_progress`：卡片的初始正文本来就是「正在启动」，
            # 再推一次同样的内容只会白花一次 sequence（见那函数的说明）。
            _write_progress_row("正在启动", "")
            for kind, props in oc.events():
                if deadline is not None and now() > deadline:
                    handled.append(
                        f"超过单条委派的时长上限（{max_seconds:g}s）—— 已中止"
                    )
                    time_capped = True
                    log.warning(
                        "【执行期】超过时长上限 %ss，主动中止会话：task=%s",
                        max_seconds, getattr(task, "id", "?"),
                    )
                    _abort_quietly(oc)
                    break
                if kind == "session.idle":
                    break
                if kind == "session.error":
                    # 错误收场：只记不 abort —— 会话已经结束了（这是收场事件，
                    # 不是挂起请求），abort 一个已死的会话没有意义。错误对象
                    # 留给循环后统一报（见下面的 error 分支）。
                    session_error = props if isinstance(props, dict) else {}
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
                                         approver=approver, context=context,
                                         reply_to=progress_anchor_id or "",
                                         task_id=getattr(task, "id", "") or "")
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
                    # 其它事件（进度、消息）不处理 —— **但工具事件要投影到卡片**
                    # （设计文档 11.16）。`permission.asked` 只在「要人授权」
                    # 时才来，而 OpenCode 的多数工具（读文件、检索、glob）是
                    # 直接放的 —— 只看授权事件，用户看到的就是一段长时间空白，
                    # 分不清「在干活」和「卡住」。这正是 11.16 要解决的问题。
                    _note_tool_event(kind, props)
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
                    _abort_quietly(oc)
                    break
                decision, credential = _ask_one_tool(
                    oc, req, store=store, sender=sender,
                    approver=approver, context=context,
                    # 授权卡挂到进度卡下面（设计文档 11.16）。空串时 sender
                    # 走原路径，所以「没进度卡」不影响授权流程。
                    reply_to=progress_anchor_id or "",
                    # 归属（12.8）：让 WEB 面板能把这张卡指回是哪条委派。
                    task_id=getattr(task, "id", "") or "",
                )
                handled.append(f"{req.summary} → {'允许' if decision == 'allow' else '拒绝'}")
                tool_calls += 1
                if decision == "allow":
                    granted += 1
                # **先把答案回给 opencode，再考虑中止** —— 顺序反了会让
                # 那个挂起的请求没人回，opencode 会一直等着。
                oc.reply_permission(req.request_id, decision)
                # ── 进度卡片更新（设计文档 11.16）──────────────────────────
                #
                # 每次 permission.asked 都意味着 agent 做了一件工具调用。
                # 更新卡片内容，但节流 —— 不是每次都更新。
                #
                # 关键节点立即更新：第一次工具调用（tool_calls == 1）、
                # 用户叫停了（下面 if 里）。其它情况按间隔节流。
                #
                # 为什么不在事件循环里单独处理 tool_call 事件：
                # OpenCode SSE 事件流里，permission.asked 就是工具调用的
                # 信号。虽然事件流里可能有更细的 tool_call / text 事件，
                # 但那些是"agent 正在思考"的中间状态，不是"agent 做了什么"
                # 的有效信号。permission.asked 才是用户能感知的进展。
                _update_progress(
                    f"{req.summary} → {'允许' if decision == 'allow' else '拒绝'}",
                    force=tool_calls == 1,
                )
                # ── 用户叫停了整条委派 ────────────────────────────────────
                #
                # 判定放在**回完答案之后**：这一次的授权已经有结论了（拒绝），
                # 而「停止」是关于**整条委派**的另一个决定，混在一起会让
                # 「已拒绝」被记成「已停止」。
                #
                # 必须 ``abort``：光拒绝这一步，agent 只会换个方向继续，
                # 而用户点的是「停止这条委派」。挂着不 abort 的话，那
                # 就是「点了没反应」。
                if credential and store.is_stop_requested(credential):
                    handled.append("你叫停了这条委派")
                    user_stopped = True
                    log.info(
                        "【执行期】用户叫停（task=%s），中止会话",
                        getattr(task, "id", "?"),
                    )
                    _abort_quietly(oc)
                    break
    except (ServerError, OSError) as exc:
        # ── 关闭进度卡片流式模式（设计文档 11.16）────────────────────
        #
        # 无论成功还是失败，流式模式都必须关闭 —— 不关的话飞书会在 10 分钟后
        # 自动关闭，那时卡片内容可能还没写完，用户看到的是半截内容。
        #
        # 走**同一个** :func:`_close_progress_stream` 收口点，不在这里另抄一份
        # （抄一份就多一处「两边的 sequence 规则会漂」的地方，而且异常路径
        # 那一处曾经因为变量在 `with` 内初始化而读不到值）。
        _close_progress_stream(
            f"**项目**：{project}\n\n"
            f"**状态**：授权链路失败\n\n"
            f"**原因**：{type(exc).__name__}: {exc}"
        )
        # 异常路径**同样要清落库进度**：不清的话 WEB 面板会一直显示
        # 「正在执行」配一个再也不动的时间戳，而那与「卡住了」无法区分。
        _clear_progress()
        return DispatchOutcome(
            ok=False,
            summary=f"执行期授权链路失败：{type(exc).__name__}: {exc}",
            session_id=session_id or None,
            tool_calls=tuple(handled),
        )

    # ── 收尾：关掉进度卡片（设计文档 11.16）────────────────────────────
    #
    # 正常走出循环的统一收口。用与下面 return 级联**同一套判据**决定状态词，
    # 顺序也一致 —— 用户看到的状态必须与库里落的结论是同一件事。
    #
    # 幂等：异常路径已在 except 里关过，这里是 no-op。
    _final_status = (
        "会话出错" if session_error is not None
        else "提问未答" if unanswered
        else "已叫停" if user_stopped
        else "步数超限" if step_capped
        else "时长超限" if time_capped
        else "已完成"
    )
    _close_progress_stream(
        f"**项目**：{project}\n\n**状态**：{_final_status}\n\n"
        f"**工具调用**：{tool_calls} 次（允许 {granted} 次）"
    )
    # 收尾清行。放在**状态词算完之后、统一收口那里**（而不是每个 return
    # 分支各清一次）：分开放的必然漏一条，而漏掉的那条症状是
    # 「面板上永远挂着一条僵尸进度」。
    _clear_progress()

    # 会话级失败**优先于一切统计性结论**。理由：unanswered / user_stopped /
    # step_capped / time_capped 描述的是「这一轮里发生了什么事」，而 error
    # 描述的是「这一轮根本没能正常进行」。error 出现时去报「问了 3 次授权」
    # 是把事故原因藏进了统计数字里 —— 用户在界面上看到「完成（执行期问了
    # 3 次）」而实际模型一次都没跑成（真机 403 实测）。
    if session_error is not None:
        raw = json.dumps(session_error, ensure_ascii=False)
        reason = (extract_error_message(raw)
                  or first_meaningful_line(raw)
                  or "（错误里挖不出可读原因）")
        return DispatchOutcome(
            ok=False,
            summary=f"执行器会话出错：{reason}",
            detail=raw[:4000],
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
    if user_stopped:
        # **不算失败，也不算完成。** 用户自己叫停的，措辞必须如实：
        # 说成「失败」会让他以为代码坏了而去排查；说成「完成」会让他
        # 以为改动做完了而去验收 —— 后者更糟。
        return DispatchOutcome(
            ok=False,
            summary=(
                "你叫停了这条委派 —— 会话已中止，agent 不会继续动手。\n"
                "  已经完成的那几步改动**仍然有效**，剩下的没做。\n"
                f"  想接着做的话重新发一条委派就行；"
                f"已做的部分可以先看一眼（{session_id or '会话已关'}）。"
            ),
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
    if time_capped:
        # 与 step_capped 同样报成失败，但**原因不同、给的路也不同**：
        # 步数超限是「要做的事太多」，时长超限是「这件事卡住了」。
        # 混成同一句话会让人往错的方向拆需求。
        return DispatchOutcome(
            ok=False,
            summary=(
                f"超过单条委派的时长上限（{max_seconds:g} 秒）—— 已主动中止会话。\n"
                f"  这不是「要做的事太多」，是**这件事卡住了**："
                f"它一直有动静，所以次数没超，但时间拖到了上限。\n"
                f"  已完成的改动还在，剩下的没做。可以看一眼它卡在哪，"
                f"或者调高上限（当前 {max_seconds:g} 秒）。"
            ),
            session_id=session_id or None, tool_calls=tuple(handled),
        )
    note = (f"执行期问了 {len(handled)} 次，允许 {granted} 次"
            if handled else "未触发授权请求")
    return DispatchOutcome(
        ok=True, summary=f"完成（{note}）",
        session_id=session_id or None, tool_calls=tuple(handled),
    )


def pick_approver(users: frozenset[str] | set[str]) -> str:
    """从白名单里选出**唯一审批人**。

    ## 为什么不是 ``sorted()[0]``

    白名单可能同时装着同一个人的两种 id：``open_id``（``ou_`` 开头，
    卡片点击回传的身份）与租户级 ``user_id``（裸串）。两者是**互相独立
    的串**，靠后缀比对认不出是同一人（:func:`_same_person` 只能做后缀
    匹配，而实测 user_id 并不是 open_id 的后缀）。

    实测踩坑（2026-10-09）：Web 建的委派没有飞书发起人，审批人兜底取
    ``sorted()[0]`` —— 数字开头的 ``user_id`` 排在 ``ou_`` 前面被选中，
    发出去的卡主人**自己点不动**，被「只有发起人能批」误拒。

    所以：**优先选 ``ou_`` 开头的条目**（它与点击身份同一类型，必然能
    比对）；没有才退回字典序第一个。发卡侧（``_receive_id_type``）两种
    id 都能发，但**比对**只有 open_id 这一头是可靠的。
    """
    open_ids = sorted(u for u in users if u.startswith("ou_"))
    if open_ids:
        return open_ids[0]
    return next(iter(sorted(users)), "")


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
    reply_to: str = "",
    task_id: str = "",
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
        task_id=task_id or None,
    )

    # **一问一卡，发完立刻等那一问。**
    #
    # 卡必须在循环**里**发。我第一版把「等」挪进循环却把「发」留在循环外，
    # 于是第 2 张卡永远发不出去 —— 死锁原因没变，只是挪了一层。
    collected: list[list[str]] = []
    for slot in range(len(req.questions)):
        try:
            message_id = _send_card_threaded(
                sender.send_question_card,
                open_id=approver,
                subject=req.questions[slot],
                detail=_question_detail(
                    req, slot, ttl_seconds=policy.ttl_seconds),
                credential=pending.credential,
                ttl_seconds=policy.ttl_seconds,
                # 同授权卡：挂到进度卡下面，失败则退回（见 _send_card_threaded）
                reply_to=reply_to,
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


def _send_card_threaded(
    send: Callable[..., str], *, reply_to: str, **kwargs: Any,
) -> str:
    """发一张卡，**优先挂到锚点下，失败则退回独立发送**。返回 ``message_id``。

    ## 为什么必须有这次退回（真机踩过，2026-10-12）

    「reply 到进度卡下」本身引入了一个**新的失效模式**：锚点那条消息一旦被
    撤回或过期，之后每一次 reply 都会被飞书拒（实测 ``code=99992354
    The request ... is not a valid open_message_id``）。

    而上游 :func:`_ask_one_tool` 对「发卡失败」的处置是**降级成拒绝** ——
    这是有意的（卡片送不到就没人能批准，等着必然超时）。两者叠起来的后果是：
    **整条委派静默停在「已拒绝」，而实际上没有任何人拒绝过任何东西。**

    一次没用的拒绝比一次难看的卡糟糕得多：用户会以为是自己点错了，
    而真因（锚点没了）没有任何提示。

    ## 为什么退回而不是直接拒绝

    锚点失效只影响**挂哪儿**，不影响**能不能发**。退回独立发送后：
    卡片照样到人手里、闸门照样工作，只是没聚拢 —— 而没聚拢是**外观损失**，
    停掉整条委派是**功能损失**。两害相权取轻。

    只在 ``reply_to`` 非空时才重试（那时才可能有锚点）；
    两次都失败就照旧抛出去，由上游降级成拒绝。

    :param send: 绑定好的发卡函数（``send_tool_card`` / ``send_question_card``）。
        收成参数是为了让两张卡**共用同一条退回规则** —— 复制两份的话，
        改了一处就会出现「授权卡会退回、提问卡不会」。
    """
    if not reply_to:
        return send(reply_to="", **kwargs)
    try:
        return send(reply_to=reply_to, **kwargs)
    except Exception as exc:  # noqa: BLE001 - 退回路径要兜住一切
        log.warning(
            "【执行期】挂到进度卡下失败（%s）—— 退回独立发送（不再聚拢）", exc,
        )
        return send(reply_to="", **kwargs)


def _ask_one_tool(
    oc,
    req,
    *,
    store: "ApprovalStore",
    sender: "ToolCardSender",
    approver: str,
    context: "ApprovalContext",
    reply_to: str = "",
    task_id: str = "",
) -> tuple[str, str]:
    """问一次，回 ``(结论, 凭据)``。**只回结论，不解释** —— 解释是卡片的事。

    **凭据为什么要带出来**：停止与拒绝在 :meth:`ApprovalStore.wait` 那里
    **都表现为 ``deny``** —— 那次动作确实没被批准，这一点两者是一样的。
    但「你拒绝了这一步」和「你叫停了整条委派」**给用户的话完全不同**，
    而只有凭据能让调用方去查 :meth:`ApprovalStore.is_stop_requested`。

    没有凭据的话，调用方只能把两者混成同一句「已拒绝」—— 而用户点的是
    「停止」，却看到「拒绝」，会以为还能继续等下去。

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
        task_id=task_id or None,
    )
    try:
        message_id = _send_card_threaded(
            sender.send_tool_card,
            open_id=approver, subject=pending.subject,
            detail=pending.detail or "", credential=pending.credential,
            ttl_seconds=policy.ttl_seconds, reply_to=reply_to,
        )
    except Exception as exc:  # noqa: BLE001 - 发卡失败必须变成拒绝
        log.warning("【执行期】发卡失败（%s）—— 按拒绝处理", exc)
        # 凭据给空串：没走到 wait，也就没有「被叫停」可言。
        return "deny", ""
    store.record_card(pending.credential, message_id)
    # 结论一律回库读（同 11.9.4 的纪律），不采信任何返回值。
    # ``timeout_seconds`` 必须**显式**给 policy 的 TTL：默认那个是
    # DEFAULT_TTL_SECONDS，与本场景的 TTL 不是一回事（实测 1800 vs 600）。
    decision = store.wait(
        pending.credential,
        timeout_seconds=policy.ttl_seconds,
        # 让人能**中途叫停**：用户点「停止这条委派」之后，这里最多一个
        # poll_seconds 就返回，而不是干等到 TTL（委派档 1800s = 半小时）。
        should_stop=lambda: store.is_stop_requested(pending.credential),
    )
    return decision, pending.credential


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
    # 这句承诺必须指向**真能重置**的入口。原文写「可以 /note 记下原因后重派」
    # 是个空头承诺：note 不在 terminal 集合里，对幂等判定完全不可见，
    # 什么也重置不了 —— 于是「一次失败变死任务」而用户拿到一句「可以重派」。
    # 现在指向 /redispatch，那条真的会落一条 DELEGATION_RESET。
    #
    # 刻意不提「执行期失败才重派」这种区分：那句话要求这里知道「本次失败
    # 属于哪一类」，而判定在 services/delegate.py 里、这里只看得到 ok 与
    # summary —— **断言一个本函数无权确认的全局状态就是撒谎**（与设计文档
    # 8.2 那条「文本必须能从本函数持有的事实推出」同源）。
    # 真正分「派发前 / 执行期」的地方是 :func:`attempt_state`，它才是权威。
    app.tasks.note(
        task.id,
        ("执行器已产出结果，待你验收"
         if outcome.ok else "执行失败；改好原因后 /redispatch <id> 可以重新派发"),
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
    # ``--agent`` / ``--variant`` 与 ``--model`` 同族：三者都是「用哪个」。
    # 合成一个 ``--use`` 看着更整齐，但 OpenCode 的载荷本来就是三个独立字段
    # （OpenAPI ``UserMessage``），拆一次就多一处会漂移的地方。
    parser.add_argument(
        "--agent", default=None, metavar="工作模式",
        help="覆盖 opencode 的工作模式（agent 名，如 "
             "'Sisyphus - ultraworker'）。留空用它的默认。",
    )
    parser.add_argument(
        "--variant", default=None, metavar="档位",
        help="推理档位，如 high。留空=不施加 variant，用模型基线。"
             "只有部分模型有档（实测 4300/8475），且 opencode **不校验**"
             "错档也会返回 204，所以只给模型真有的档。",
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
    detected = detect_opencode_version(probe_policy.command)
    mismatch = version_mismatch_reason(detected)
    if mismatch is not None:
        print(f"⚠ opencode 版本不可用 —— 不派发任何委派\n  {mismatch}")
        return 2
    # 版本可用 ⇒ **把当前适配器切成这个版本**。协议知识（端点、字段名、
    # 事件形状）全在适配器里；用错版本不报错，只会让闸门**静默失效**。
    # ``detect_adapter`` 取不到就 raise（不静默回退到 V1），而上面已确认
    # ``detected`` 在可派发集合里，所以这里不会抛。
    detect_adapter(detected)

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
        approver = pick_approver(cfg.allowed_users)
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
        """唯一有资格批准的人。**经 :func:`pick_approver` 选取**（优先 ``ou_``
        条目，理由见彼处；与 :class:`ApprovalGate` 同源）。

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
        return pick_approver(cfg.allowed_users)

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
        if args.model is not None or args.agent is not None \
                or args.variant is not None:
            base = _delegate_policy(build_app(args.db))
            # 只覆盖**真的给了**的那些 —— ``--agent A`` 不该顺手把配置里的
            # model 抹成默认。逐个判而不是 ``if any(...)`` 之后一起 replace。
            overrides = {}
            if args.model is not None:
                overrides["model"] = args.model
            if args.agent is not None:
                overrides["agent"] = args.agent
            if args.variant is not None:
                overrides["variant"] = args.variant
            policy = replace(base, **overrides)
        # ``--dry-run`` 不碰闸门：预演的意义是「看看会派什么」，
        # 而闸门要发卡、要人点 —— 在这里发卡会让预演变成一次真请求。
        gate = None if args.dry_run else build_real_gate()
        if args.tool_gate:
            # 同一个 FeishuSender 实例 —— 它同时实现 ToolCardSender 与
            # ProgressCardSender 两套方法。刻意只造一个：调两次会各自拿一份
            # token 缓存、各刷新一次，而它们是同一个东西。
            _sender = build_feishu_sender()
            report = run_once_with_tool_gate(
                db_path=args.db, dry_run=args.dry_run, policy=policy,
                sender=_sender, approver=current_approver(),
                progress_sender=_sender,
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
