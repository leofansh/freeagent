"""启动 Web UI：打印地址、开浏览器、跑起来。

从 :mod:`server` 拆出来，因为它是**进程入口**（有 ``print``、``webbrowser``、
``KeyboardInterrupt``），和「怎么回一个 HTTP 响应」是两件事。
"""

from __future__ import annotations

from pathlib import Path

from ..app import build_app
from .server import DEFAULT_HOST, DEFAULT_PORT, create_server

__all__ = ["serve"]


def serve(
    db_path: Path | None = None,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    open_browser: bool = True,
) -> int:
    """启动并在终端打印地址。"""
    app = build_app(db_path)
    server = create_server(app, host, port)
    url = f"http://{host}:{server.server_address[1]}/"
    print("个人事务助手 · Web UI")
    print(f"  地址：{url}")
    print(f"  智能层：{app.llm_name}")
    print("  仅监听回环地址（局域网不可达）。Ctrl+C 停止。")
    if open_browser:  # pragma: no cover - 取决于桌面环境
        import webbrowser

        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 - 打不开浏览器不是致命错误
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover
        print("\n已停止。")
    finally:
        server.server_close()
        app.close()
    return 0
