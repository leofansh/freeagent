"""后台服务统一控制：状态、启、停、重启。

## 为什么单独一个模块

收敛之前的状态是「桥接的启停按钮在飞书通道页、执行器没有任何入口」
—— 恢复出厂要停服务时当场撞上（界面只能停一个）。进程生命周期收归
这里；**各服务的配置仍留在自己的页**（飞书凭据/白名单在飞书页），
控制与配置不混在一处。

## 状态以什么为准：进程命令行扫描

不是「谁启的」记录。服务有四个可能来源：开机自启（Startup 里的
.cmd）、用户自己开的终端、本页面、别处 —— 记归属必然漏一种，而漏掉
的那种表现是「明明在跑却显示已停止」，比没有状态更糟。命令行扫描对
来源一视同仁：在跑就是 in跑，pid 与命令行一并显示。

## 两条硬边界

1. **只杀命令行匹配已知服务特征的 pid**。绝不给「杀任意 pid」的入口
   —— 那等于把本机指挥权交出来。
2. **启动脱离父进程**（Windows: DETACHED_PROCESS；POSIX:
   start_new_session）。Web 挂了不该带走服务；反过来 Web 重启也不
   该「忘记」它们在跑 —— 后者由扫描天然保证。
"""

from __future__ import annotations

import contextlib
import dataclasses
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

__all__ = [
    "SERVICES",
    "ServiceRefused",
    "ServiceSpec",
    "ServiceState",
    "list_states",
    "restart",
    "start",
    "stop",
]

#: 进程命令行里的特征串。按命令行找、不靠 pid 文件 —— pid 会复用，
#: 命令行不会（与 README 的排查命令、reset.py 的检测同一思路）。
_BRIDGE_MARK = "freeagent.feishu.bridge"
_EXECUTOR_MARK = "freeagent.delegate"


@dataclasses.dataclass(frozen=True, slots=True)
class ServiceSpec:
    """一个可管理的后台服务。"""

    key: str
    title: str
    mark: str
    note: str


#: 登记表。顺序即界面顺序。**不加 hermes**：它「已登记、未接线」，
#: 给它一个启停按钮等于给一个点了必失败的东西（设计文档 11.10.8）。
SERVICES: tuple[ServiceSpec, ...] = (
    ServiceSpec(
        key="bridge",
        title="飞书桥接",
        mark=_BRIDGE_MARK,
        note="飞书长连接：收消息、发卡片、回落授权结论。",
    ),
    ServiceSpec(
        key="executor",
        title="委派执行器",
        mark=_EXECUTOR_MARK,
        note="每 5 秒扫库，把已 /start 的委派交给 opencode；执行期逐次授权。",
    ),
)

_SPECS = {s.key: s for s in SERVICES}


class ServiceRefused(RuntimeError):
    """拒绝执行。消息给用户看：说清为什么、下一步做什么。"""


@dataclasses.dataclass(frozen=True, slots=True)
class ServiceState:
    """一个服务的当前状态。"""

    key: str
    title: str
    note: str
    running: bool
    pid: int | None
    cmdline: str
    started_by_page: bool


def _repo_root() -> Path:
    """仓库根（``src/freeagent/services/svcs.py`` 向上四级）。

    为什么值得单测：写成 ``parents[2]`` 会得到 ``src/``，症状不是崩溃
    而是「日志路径指到一个不存在的目录」（Errno 2）和 PYTHONPATH 变成
    ``src/src`` —— 页面拉起的执行器起不来，而同一个文件里的桥接启动
    （走 supervisor）却是好的。这类错静态看不出来，真机一点才炸。
    """
    return Path(__file__).resolve().parents[3]


def _scan() -> dict[str, list[tuple[int, str]]]:
    """扫一遍进程，按特征分组。返回 ``{key: [(pid, cmdline), ...]}``。

    刻意一次 WMI 查询拿全部 python 进程再本地分组：起 N 个子进程查 N
    次，慢且中间状态可能不一致。扫不到平台 ⇒ 抛（fail-closed：
    不知道就等于不能停）。
    """
    if os.name == "nt":
        return _scan_windows()
    if Path("/proc").is_dir():
        return _scan_proc()
    raise ServiceRefused(
        "无法检测服务进程（未知平台）—— 为安全起见拒绝启停。"
    )


def _scan_windows() -> dict[str, list[tuple[int, str]]]:
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" "
        "| ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }"
    )
    try:
        proc = subprocess.run(  # noqa: S603 - argv 固定，无用户输入
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=30, shell=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ServiceRefused(f"进程扫描失败：{exc}") from exc
    return _group(proc.stdout or "")


def _scan_proc() -> dict[str, list[tuple[int, str]]]:
    rows: list[str] = []
    for entry in sorted(Path("/proc").glob("[0-9]*/cmdline")):
        try:
            pid = int(entry.parent.name)
            raw = entry.read_bytes().replace(b"\x00", b" ").decode(
                "utf-8", "replace").strip()
        except (OSError, ValueError):
            continue
        if raw:
            rows.append(f"{pid}\t{raw}")
    return _group("\n".join(rows))


def _group(text: str) -> dict[str, list[tuple[int, str]]]:
    found: dict[str, list[tuple[int, str]]] = {s.key: [] for s in SERVICES}
    for line in text.splitlines():
        if "\t" not in line:
            continue
        head, _, cmdline = line.partition("\t")
        try:
            pid = int(head.strip())
        except ValueError:
            continue
        for spec in SERVICES:
            if spec.mark in cmdline:
                found[spec.key].append((pid, cmdline.strip()))
    return found


#: 本进程（Web）这一会话里由本页面拉起来的服务 pid。
#: 只为了一句诚实的「本页面启的」标注，**不是**状态的来源。
_PAGE_SPAWNED: set[int] = set()


def list_states() -> list[ServiceState]:
    """全部服务的当前状态。一张表，界面直接渲染。"""
    hits = _scan()
    out: list[ServiceState] = []
    for spec in SERVICES:
        rows = hits.get(spec.key) or []
        # 多实例（不该有，但扫描不撒谎）：报第一个 + 计数，让人看见异常。
        pid, cmdline = (rows[0] if rows else (None, ""))
        out.append(ServiceState(
            key=spec.key, title=spec.title, note=spec.note,
            running=bool(rows), pid=pid, cmdline=cmdline,
            started_by_page=bool(pid and pid in _PAGE_SPAWNED),
        ))
    return out


def _state_of(key: str) -> ServiceState:
    spec = _SPECS.get(key)
    if spec is None:
        raise ServiceRefused(f"不认识的服务：{key!r}")
    return next(s for s in list_states() if s.key == key)


def _kill(pid: int) -> None:
    """停一个 pid。Windows 用 taskkill（那边没有 SIGTERM 语义可用），
    POSIX 先 TERM、等一秒、还活着才 KILL。"""
    if os.name == "nt":
        subprocess.run(  # noqa: S603 - pid 来自扫描，且调用方已校验
            ["taskkill", "/f", "/pid", str(pid), "/t"],
            capture_output=True, timeout=30, shell=False,
        )
        return
    os.kill(pid, signal.SIGTERM)
    time.sleep(1.0)
    with contextlib.suppress(OSError):
        os.kill(pid, 0)          # 还活着才补 KILL
        os.kill(pid, signal.SIGKILL)


def stop(key: str) -> ServiceState:
    """停。没在跑 ⇒ 拒（那是一次空操作，不该显示成「已停止」的成功）。"""
    state = _state_of(key)
    if not state.running:
        raise ServiceRefused(f"{state.title}本来就没在跑。")
    hits = _scan()
    rows = hits.get(key) or []
    _PAGE_SPAWNED.discard(state.pid)
    for pid, _cmdline in rows:
        _kill(pid)               # 命令行已匹配过特征，杀的是自己人
    return _state_of(key)


def start(key: str) -> ServiceState:
    """启。已在跑 ⇒ 拒（并说清 pid）—— 重复启动的后果不是报错，
    是两个实例写同一个库/抢同一个飞书锁，那比拒绝坏得多。"""
    state = _state_of(key)
    if state.running:
        raise ServiceRefused(
            f"{state.title}已经在跑（pid {state.pid}）—— 先停再启，或用「重启」。"
        )
    if key == "bridge":
        _start_bridge()
    elif key == "executor":
        _start_executor()
    else:  # pragma: no cover - 登记表外进不来
        raise ServiceRefused(f"不认识的服务：{key!r}")
    return _state_of(key)


def restart(key: str) -> ServiceState:
    """重启。没在跑也照启（重启的语义就是「要它在跑」）。"""
    state = _state_of(key)
    if state.running:
        hits = _scan()
        for pid, _cmdline in (hits.get(key) or []):
            _kill(pid)
        _PAGE_SPAWNED.discard(state.pid)
    if key == "bridge":
        _start_bridge()
    else:
        _start_executor()
    return _state_of(key)


def _start_bridge() -> None:
    """桥接启动**复用 supervisor**：它有凭据预检（错了当场说清，而不是
    起一个几秒后自己退掉的进程），且那条路有测试守着。

    它 Popen 出来的实例是 Web 的子进程 —— 这没关系：Web 死了桥接以
    孤儿身份继续跑（正是要的韧性），而状态显示走命令行扫描，Web 重启
    后照样认得它。真正被淘汰的是「Web 只认自己启的那个」那套记录。
    """
    from ..feishu.supervisor import SupervisorError, supervise

    try:
        supervise("start")
    except SupervisorError as exc:
        raise ServiceRefused(f"桥接起不来：{exc}") from exc


def _start_executor() -> None:
    """执行器启动：脱离式拉起（见模块 docstring 的边界 2）。

    日志**每次起开一个新文件**而不是都往 ``data/executor.log`` 追：
    那个文件还可能被 Startup 里的 .cmd 实例以 GBK 追加，两个写入者、
    两种编码混进同一文件 —— supervisor 那边为这个踩过坑（11.9.5），
    不重犯。
    """
    root = _repo_root()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    log_path = root / "data" / f"executor-page-{stamp}.log"
    # data/ 一般都在（.cmd 也会建），但页面启动不该赌它存在 ——
    # 赌输的症状就是用户看到的 Errno 2。
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(root / "src")
    env["PYTHONIOENCODING"] = "utf-8"
    argv = [
        sys.executable, "-u", "-m", "freeagent.delegate",
        "--tool-gate", "--watch", "5",
    ]
    # 与 tools/_svc_executor.cmd 同一条命令 —— 页面启的和开机自启的
    # 是同一个东西，别在两处各写一份参数。
    try:
        with log_path.open("wb") as log:
            proc = subprocess.Popen(
                argv, cwd=str(root), env=env,
                stdout=log, stderr=log, shell=False,
                **_spawn_flags(),
            )
    except OSError as exc:
        raise ServiceRefused(f"执行器起不来：{exc}") from exc
    _PAGE_SPAWNED.add(proc.pid)


def _spawn_flags() -> dict[str, object]:
    if os.name == "nt":
        return {"creationflags": (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NO_WINDOW
        )}
    return {"start_new_session": True}
