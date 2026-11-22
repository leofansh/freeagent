"""桌面入口（V1.144）：无控制台、应用窗口、单实例、窗口关即停。

为什么不给 :func:`freeagent.web.serve.serve` 加参数：那个函数是**终端入口** ——
打印地址、等 ``KeyboardInterrupt``。桌面版由 ``pythonw.exe`` 起，这两件事都不
成立：``print`` 进了虚空，Ctrl+C 永远不会来。共用一个入口会让「怎么停」有两套
说法，所以生命线单独一条。

## 四条纪律

1. **单实例**：端口上已经有人在应答，就只开窗口、不再起服务。第二个进程重复
   ``serve_forever`` 会直接 bind 失败，人要面对一个什么都没做的错误框。
2. **窗口必须是独立的浏览器进程**：``--user-data-dir`` 指到状态目录下的专用
   profile。不加这个开关时 ``--app`` 只是把 URL 交给**已运行**的浏览器，自身
   进程立刻退出 —— ``wait()`` 就永远等不到「人把窗口关了」，服务于是变成
   谁也停不掉的幽灵。
3. **窗口关 = 服务停**：没有托盘就没有第二个可见的停止入口。留下幽灵后台进程
   比「被打断」更糟（fail-closed）。代价是正在跑的那一轮会中断 —— 使用说明书
   写明；想让它继续跑，用网页版。
4. **pythonw 没有 stdout**：任何失败都落到 ``<状态目录>/desktop.log``。不留日志
   的话，「双击了没反应」就真的无从查起。

## 为什么不用 tkinter

打包版的运行时是官方 **embeddable** 发行版，它**不含 Tcl/Tk**。所以这一层不能
用 ``tkinter`` 做确认框或文件夹选择 —— 那会在开发机跑得很顺、到用户机器上
``ImportError``。（同一条也适用于 V1.143 里留白的「原生选文件夹」。）
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from .paths import state_dir
from .web.server import DEFAULT_HOST, DEFAULT_PORT, create_server

__all__ = ["main", "window_command", "find_browser", "probe_running"]

LOG_NAME = "desktop.log"
PROFILE_DIR = "window"


def _log(message: str) -> None:
    """写一行带时间戳的日志。pythonw 下这是唯一能被看见的通道。"""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        home = state_dir()
        home.mkdir(parents=True, exist_ok=True)
        with open(home / LOG_NAME, "a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {message}\n")
    except OSError:
        # 日志写不进去也不能反过来挡住服务；静默降级是这里唯一可接受的。
        pass


def _bind_stdio() -> None:
    """``pythonw.exe`` 下 ``sys.stdout`` / ``sys.stderr`` 都是 **None**。

    这不是洁癖：``http.server`` 每收到一个请求都要往 ``sys.stderr`` 写一行访问
    日志，``None.write`` 会抛 ``AttributeError``，然后连接被直接关掉 —— 客户端
    看到的是**空回复**（curl 报 ``http_code 52``），而日志里一行线索都没有。
    这个 bug 只在 pythonw 下出现，用 python.exe 跑一辈子都复现不了，
    打包版第一次真跑就撞上了（V1.144 的实测记录）。

    兜到一个追加写的文件上：所有 ``print`` 与访问日志都有落处，还正好是排查时
    要看的那个文件。
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        home = state_dir()
        home.mkdir(parents=True, exist_ok=True)
        stream = open(home / LOG_NAME, "a", encoding="utf-8", buffering=1)
    except OSError:
        return  # 连日志都开不了：保持 None，但至少不在这里再抛一次
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


def _candidates() -> list[Path]:
    """Edge 与 Chrome 的标准安装位置，按「本机更可能装了的」排前。"""
    roots = []
    for env in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
        value = os.environ.get(env)
        if value:
            roots.append(Path(value))
    out: list[Path] = []
    for root in roots:
        out.append(root / "Microsoft" / "Edge" / "Application" / "msedge.exe")
        out.append(root / "Google" / "Chrome" / "Application" / "chrome.exe")
    return out


def find_browser() -> Path | None:
    """找一个能开 ``--app`` 窗口的浏览器；找不到返回 ``None``。

    只用 ``--app`` 模式的前提是 Chromium 系。默认浏览器可能是 Firefox，它没有
    这个开关 —— 那种情况下退回「普通浏览器标签页」，并在日志里说清「窗口关了
    服务不会停」。
    """
    for path in _candidates():
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    return None


def window_command(exe: Path, url: str, profile: Path) -> list[str]:
    """``--app`` 窗口的命令行。

    ``--user-data-dir`` 不是可选优化：不隔离 profile 时 Edge/Chrome 会把请求交给
    已在跑的实例然后自己退出，父进程 ``wait()`` 立刻返回，窗口就成了没人管的
    孤儿 —— 见模块 docstring 第 2 条。
    """
    return [
        str(exe),
        f"--app={url}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
    ]


def probe_running(url: str, *, opener=urllib.request.urlopen, timeout: float = 0.6) -> bool:
    """那个地址上是不是已经有一个 Steward 在应答。

    **只认 ``ok`` 字段**：端口上可能有别的东西，任何 200 都不等于「这是我的服务」
    —— 认错了就会把窗口指到一个陌生页面，且不再起自己的服务。
    """
    try:
        response = opener(url + "api/health", timeout=timeout)
        try:
            body = response.read().decode("utf-8", "replace")
        finally:
            response.close()
        return '"ok": true' in body or '"ok":true' in body
    except Exception:  # noqa: BLE001 - 探测失败只有一个含义：没在跑
        return False


def _open(exe: Path, url: str) -> subprocess.Popen | None:
    profile = state_dir() / PROFILE_DIR
    profile.mkdir(parents=True, exist_ok=True)
    try:
        return subprocess.Popen(window_command(exe, url, profile))
    except OSError as exc:
        _log(f"窗口没起来：{exc}")
        return None


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="steward-desktop", add_help=True)
    parser.add_argument("--db", default=None, help="显式库路径（一般不要给，见 run_web.bat 的教训）")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-window", action="store_true", help="只起服务不弹窗（脚本用）")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    _bind_stdio()
    args = _parse(argv)
    url = f"http://{args.host}:{args.port}/"

    if probe_running(url):
        _log(f"服务已在跑，只开窗口：{url}")
        exe = find_browser()
        if exe is None:
            _log("没有找到 Edge/Chrome，无法开应用窗口")
            return 2
        proc = _open(exe, url)
        if proc is None:
            return 2
        proc.wait()
        return 0

    from .app import build_app

    app = build_app(Path(args.db) if args.db else None)
    # 与 serve 同一处纪律：备份失败绝不挡启动（12.11.2 / V1.39）。
    from .services.reset import daily_backup, db_path_of

    try:
        daily_backup(db_path_of(app.conn))
    except Exception as exc:  # noqa: BLE001
        _log(f"今日备份没能执行：{exc}")

    try:
        server = create_server(app, args.host, args.port)
    except OSError as exc:
        _log(f"端口 {args.port} 起不来（可能被别的程序占着）：{exc}")
        app.close()
        return 3

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _log(f"服务已起：{url}")

    if args.no_window:
        # 脚本化场景（探针 / 测试）：不等人关窗口，直接跑到被外部停掉。
        try:
            while thread.is_alive():
                time.sleep(0.5)
        except KeyboardInterrupt:  # pragma: no cover
            pass
        server.shutdown()
        app.close()
        return 0

    exe = find_browser()
    if exe is None:
        # 退回默认浏览器：这种行为下我们拿不到窗口的生命周期，
        # 所以**明确**改成「服务常驻到进程被杀」，而不是假装窗口是服务的主控。
        _log("没有 Edge/Chrome：改用默认浏览器标签页；关掉标签页不会停服务，要停就结束本进程")
        import webbrowser

        try:
            webbrowser.open(url)
        except Exception as exc:  # noqa: BLE001
            _log(f"默认浏览器也没起来：{exc}")
        try:
            while thread.is_alive():
                time.sleep(0.5)
        except KeyboardInterrupt:  # pragma: no cover
            pass
        server.shutdown()
        app.close()
        return 0

    proc = _open(exe, url)
    if proc is None:
        server.shutdown()
        app.close()
        return 2
    proc.wait()
    _log("窗口已关，停服务")
    server.shutdown()
    server.server_close()
    app.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
