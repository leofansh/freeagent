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
    # 12.11.2 / V1.39：每日滚动备份。逻辑只在 services 层一份
    # （:func:`daily_backup`：当日一次、留 7 份、失败只记日志不抛各方）。
    # 三个进程入口各调一行 —— 谁启动谁先把当天那份备份落掉。
    # 从**活连接**问路径（``db_path_of``）：不重新解析，避免显式 --db
    # 时备到另一个库文件上（那是 reset 模块踩过的坑，同一处纪律）。
    from ..services.reset import daily_backup, db_path_of
    try:
        daily_backup(db_path_of(app.conn))
    except Exception as exc:  # noqa: BLE001 - 备份失败绝不挡住 Web 启动
        print(f"⚠ 今日备份没能执行（{exc}）；功能不受影响，明天会再试。")
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
