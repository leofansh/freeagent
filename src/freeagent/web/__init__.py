"""本地 Web UI。"""

from __future__ import annotations

__all__ = ["serve", "create_server", "main"]


def main() -> int:  # pragma: no cover - 入口
    from .server import serve

    return serve()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
