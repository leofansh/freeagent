"""``python -m freeagent.web`` 入口。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .serve import serve
from .server import DEFAULT_HOST, DEFAULT_PORT


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m freeagent.web",
        description="个人事务助手 · 本地 Web UI（仅监听回环地址）",
    )
    parser.add_argument("--db", type=Path, default=None, help="数据库文件路径")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="端口")
    parser.add_argument(
        "--no-browser", action="store_true", help="不要自动打开浏览器"
    )
    args = parser.parse_args(argv)
    return serve(
        args.db,
        port=args.port,
        open_browser=not args.no_browser,
    )


if __name__ == "__main__":
    sys.exit(main())
