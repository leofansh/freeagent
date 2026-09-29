"""从控制面启停桥接（设计方案 12.7）。

目标是把「另开一个窗口跑 ``run_feishu.bat``」这件事从用户的日常里去掉。

## 三条不能违反的约束

1. **「是否已在运行」只有一个真相来源** —— 桥接自己的 :class:`PortLock`。
   探测方式就是「试着 bind 一下那个端口」。不另造判据：另造必然漂移，
   而漂移的表现是界面说「没在跑」、去起第二个、飞书只把事件投给其中一个
   （见 :class:`PortLock` 文档里「bot 时好时坏」那段）。

   探测用的锁**必须立刻释放**。忘了释放的话，Web 进程会一直占着端口，
   而它刚启动的那个桥接永远拿不到锁 —— 自己把自己堵死。

2. **只停自己启的进程**（安全性）。进程句柄只存在内存里。
   拿不到句柄时**如实报错**，而不是照着 pid 去杀 ——
   状态文件里的 pid 可能是**回收后属于别的程序**的（重启、或进程早就退了），
   照着它 terminate 会杀掉一个毫不相干的进程。这类「看起来很方便」的写法
   必须拒绝，代价是「Web 重启后停不掉之前那个桥接」，如实说明即可。

3. **子进程读得到界面存的配置**。做法是**什么都不做**：子进程继承环境变量，
   而 :func:`freeagent.feishu.config.merged_env` 会用 ``feishu.env`` 补齐
   缺的那些键。刻意不往子进程环境里塞配置 —— 那等于把优先级规则复制一份，
   而复制的那份迟早和 :func:`merged_env` 不一致。

## 已知限制（如实记着，不是「以后再说」）

- Web 进程被强杀时，子进程会**变孤儿**并继续运行（状态文件照常心跳，
  界面会如实显示它在跑），但那时**停不掉**它 —— 句柄已经随 Web 一起没了。
  正常退出路径有 :func:`stop` 兜底，强杀没有。
- 停止是「终止」不是「优雅关闭」，所以强杀时状态文件会留在盘上；
  界面靠 :func:`freeagent.feishu.status.is_stale` 判活，不靠终态。
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .bridge import AlreadyRunning, PortLock, lock_port_from_env
from .status import is_stale, read_status, status_path

__all__ = [
    "supervise",
    "port_probe",
    "bridge_state",
    "SupervisorError",
]

#: 启动后等这么久再看一眼进程死没死。用来**抓早退**（配置错、缺 Secret），
#: 那种情况 start() 会在一秒内退出 —— 报「已启动」是骗人。
_STARTUP_GRACE_SECONDS = 1.0

#: 停止时先 terminate，等这么久还不退就 kill。
_STOP_TIMEOUT_SECONDS = 5.0

#: 进程句柄。**只在内存里**，理由见模块说明第 2 条。
_child: subprocess.Popen[bytes] | None = None
_lock = threading.Lock()


class SupervisorError(RuntimeError):
    """启停失败。消息要能直接给用户看懂，不要只说「操作失败」。"""


def _log_path(home: str | Path | None = None) -> Path:
    """桥接日志落哪。

    与状态页读的那个路径**必须一致** —— 状态页已经告诉用户「从界面启动时
    需要重定向到这个路径」，写去别处的话那块区域会永远空白，
    而空白会被当成「没出错」（见 :func:`freeagent.web.endpoints_feishu.feishu_log`）。
    """
    return status_path(home).parent / "feishu.log"


def _repo_root() -> Path:
    """仓库根。子进程的 cwd。

    ``run_feishu.bat`` 做的也是 ``cd /d "%~dp0.."``：相对路径（``data/``）
    都按这个根算，换个 cwd 会让日志和库落到不同地方。
    """
    # <root>/src/freeagent/feishu/supervisor.py -> <root>
    return Path(__file__).resolve().parents[3]


def port_probe(home: str | Path | None = None) -> dict[str, Any]:
    """「已经有桥接在跑吗」—— 试 bind 一次锁端口，**立刻放开**。

    拿不到端口就直接返回，**不做重试**：桥接刚起时锁还没绑上，
    重试几次只会让「启动」这个动作变慢，而真正的裁决权在桥接自己
    （它启动时同样会 bind，拿不到就报 AlreadyRunning）。
    """
    port = lock_port_from_env()
    lock = PortLock(port)
    try:
        lock.acquire()
    except AlreadyRunning:
        return {"running": True, "lock_port": port, "reason": "端口被占用"}
    finally:
        # 无论如何都释放。留着的话，Web 就占着自己孩子的端口。
        lock.release()
    return {"running": False, "lock_port": port, "reason": ""}


def bridge_state(home: str | Path | None = None) -> dict[str, Any]:
    """桥接现状。**把「谁在跑」和「状态文件说什么」分开报**，不混成一个字段。

    两者会不一致，而且不一致本身就是信息：
    端口有人占但状态文件陈旧 = 有人占着端口但不是我们的桥接（或它刚死）；
    句柄还在但进程早退了 = 我们启的那个已经死了。
    """
    raw = read_status(status_path(home))
    stale = is_stale(raw, now=time.time())
    with _lock:
        child = _child
    if child is not None:
        returncode = child.poll()          # None = 还在跑
        supervised = True
    else:
        returncode = None
        supervised = False
    probe = port_probe(home)
    # 「有没有在跑」只问一件事：**我们启的那个进程活着吗**。
    #
    # 刻意**不**把状态文件的新鲜度算进来。踩过的坑：桥接刚被 spawn 出来时，
    # 它还没来得及写第一次心跳，而陈旧判定对「没有状态文件」一律返回 True ——
    # 于是点完「启动」立刻回读，看到的必然是「没在跑」。那不是状态陈旧，
    # 那只是它还没开口说话。
    #
    # 状态文件的作用是回答「它连上飞书了吗」（见 feishu_status），不是回答
    # 「进程活着吗」。这两件事由两个字段分别说，不混。
    alive = supervised and returncode is None
    return {
        "supervised": supervised,
        "returncode": returncode,
        "pid": child.pid if (alive and child is not None) else None,
        "running": bool(alive),
        "port_held": probe["running"],
        "lock_port": probe["lock_port"],
        "stale": stale,
        "state": (raw or {}).get("state") or "",
        "log_path": str(_log_path(home)),
    }


def supervise(action: str, home: str | Path | None = None) -> dict[str, Any]:
    """执行 ``start`` / ``stop`` / ``restart``，返回 :func:`bridge_state`。

    一次只管一个动作，且**全程持锁** —— 两个并发点击「启动」不该起出两个
    桥接（那正是最坏的结果：飞书只投给其中一个，另一个静默失效）。
    """
    if action not in ("start", "stop", "restart"):
        raise SupervisorError(f"不认识的动作：{action!r}")

    with _lock:
        if action == "start":
            _do_start(home)
        elif action == "stop":
            _do_stop()
        else:
            _do_stop()
            _do_start(home)
    return bridge_state(home)


def _do_start(home: str | Path | None = None) -> None:
    global _child

    if _child is not None and _child.poll() is None:
        raise SupervisorError("已经在跑了（进程是我启的，还在）。")

    probe = port_probe(home)
    if probe["running"]:
        # 不硬顶。顶掉一个可能正在正常收消息的桥接，比报错坏得多。
        raise SupervisorError(
            f"端口 {probe['lock_port']} 已被占用，大概率已经有一个桥接在跑了"
            "（可能是在它自己的窗口里启的）。要停它请到那个窗口 Ctrl+C，"
            "或先在上面点「停止」再点「启动」。"
        )

    # 先做一次自检。凭据错/缺 Secret 时，桥接会在几秒内退出 —— 与其让人
    # 启完再对着空日志猜，不如现在就说清。
    #
    # 之所以不预建日志文件：自检不过就不该留下任何痕迹。否则下次真启动时，
    # 那个空文件会被误读成「上次跑过、没写日志」。
    _preflight(home)

    log_path = _log_path(home)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    root = _repo_root()
    env = dict(os.environ)
    # 让 ``python -m freeagent.feishu.bridge`` 在任何 cwd 下都能找到包。
    src = str(root / "src")
    env["PYTHONPATH"] = (
        src if not env.get("PYTHONPATH") else src + os.pathsep + env["PYTHONPATH"]
    )

    # **刻意不把子进程的 stdout/stderr 重定向进 log_path。**
    #
    # 桥接自己建了 ``FileHandler(encoding="utf-8")`` 写那个文件（设计文档
    # 11.9.5）。这里要是再重定向一次，就变成**两个写入者写同一个文件**，
    # 而本进程这条重定向用的是 ``open(..., "ab")`` —— **字节原样落盘、
    # 不做编码转换**，于是它写进去的是**子进程 stderr 的原始编码**
    # （中文 Windows 的 cmd = GBK），和桥接自己写的 UTF-8 混在一起。
    #
    # 那正是 11.9.5 修掉的那个 bug：文件一半 GBK、一半 UTF-8，严格解码
    # 谁都过不了，grep「卡片动作」一条搜不到。
    #
    # 子进程 ``logging.basicConfig`` 仍会写 stderr，所以**控制台/调用方
    # 照样看得到全部日志**；日志文件由桥接自己以 UTF-8 写，格式统一。
    #
    # 仍保留 ``log_path`` 变量：下面的错误信息要告诉人「原因在日志里」。
    try:
        _child = subprocess.Popen(
            [sys.executable, "-m", "freeagent.feishu.bridge", "--verbose"],
            cwd=str(root),
            env=env,
        )
    except OSError as exc:
        raise SupervisorError(f"起不来桥接进程：{exc}") from exc

    time.sleep(_STARTUP_GRACE_SECONDS)
    if _child.poll() is not None:
        code = _child.returncode
        _child = None
        raise SupervisorError(
            f"桥接启动后立刻退出了（退出码 {code}）。原因在日志里：{log_path}"
            "（多半是配置不全或凭据不对，doctor 能查具体哪一项）。"
        )


def _preflight(home: str | Path | None = None) -> None:
    """起之前先自检配置，把「缺什么」说在前面。"""
    from .config import load_config

    try:
        load_config().check_ready()
    except Exception as exc:  # noqa: BLE001 - 转成给人看的话
        raise SupervisorError(f"配置还不能启动桥接：{exc}") from exc


def _do_stop() -> None:
    global _child

    if _child is None:
        raise SupervisorError(
            "没有在跑的桥接可停 —— 至少没有一个是**这个界面**启的。"
            "如果桥接是在它自己的窗口里启的，请到那个窗口 Ctrl+C。"
        )
    proc = _child
    if proc.poll() is not None:
        code = proc.returncode
        _child = None
        raise SupervisorError(f"那个桥接已经自己退了（退出码 {code}），没什么可停的。")

    proc.terminate()
    try:
        proc.wait(timeout=_STOP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        # 赖着不走的（卡在 socket 读上）只能强杀。留着它等于留着第二个
        # 抢事件的桥接，那比强杀坏得多。
        proc.kill()
        try:
            proc.wait(timeout=_STOP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:  # pragma: no cover - 基本不可能
            pass
    _child = None
