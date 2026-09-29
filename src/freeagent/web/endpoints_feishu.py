"""飞书通道的只读端点（设计方案 12.7）。

**为什么单独一个文件**：`endpoints_read.py` 有 250 行上限（`test_web.py`
里有守卫）。更重要的是切分纪律本来就要求「只读 / 写」分开，而这一组端点
和事务查询毫无关系 —— 混进去会让「这个函数会不会动我的飞书配置」要看全文
才知道。

这一组端点**只读文件**，不碰 `App` 的任何服务：桥接是**独立进程**，
Web 这边拿不到它的内存状态，只能看它写出来的状态文件。

**为什么没有「启停桥接」**：那要起子进程、注入 Secret、还要管住它死后的
清理 —— 它需要令牌（12.7 已规定），属于下一片。现在只把「状态 + 日志」
露出来，先解决「不知道它到底活没活」。
"""

from __future__ import annotations

import time
from pathlib import Path

from ..app import App
from ..feishu.secret_store import read_env
from ..feishu.status import STALE_AFTER_SECONDS, is_stale, read_status, status_path

__all__ = [
    "feishu_status",
    "feishu_log",
    "state_home_for",
    "LOG_TAIL_DEFAULT",
    "LOG_TAIL_MAX",
]

#: 日志尾部默认行数。刻意不给太大：这个响应每次刷新都要重传，
#: 而日志可能有几万行。
LOG_TAIL_DEFAULT = 200
LOG_TAIL_MAX = 2000


def state_home_for(app: App) -> str | Path | None:
    """状态文件所在目录。

    复用 ``app.config.home``：``build_app`` 里「显式给了 db_path 就取它的
    父目录」，所以这里自动跟着 ``--db`` 走 —— 与桥接的落点一致。
    拿不到配置时退回默认解析（``FREEAGENT_HOME`` / ``~/.freeagent``），
    宁可位置猜错也要**把路径如实返回**让用户看出来，而不是静默显示「未运行」。

    返回类型和 :func:`freeagent.feishu.config.state_home` 一致（它收
    ``str | Path | None``）—— 标注成 ``Path | None`` 会把 ``Config.home``
    那个 ``str`` 拒掉，是个假的严格。
    """
    return app.config.home if app.config is not None else None


def _log_path(app: App) -> Path:
    """桥接日志放哪。

    与状态文件同目录 —— 状态文件是桥接自己写的、日志是启动方式决定的，
    两者都在 ``state_home`` 下最容易找。文件名与 ``tools/run_feishu.bat``
    里那个一致，两处不会各叫各的。
    """
    return status_path(state_home_for(app)).parent / "feishu.log"


def _seen_sender_rows(raw: object, allowed: frozenset[str] | None = None) -> list[dict[str, object]]:
    """把状态文件里的 seen_senders 整成**按时间倒序**的行，**最近的在最前**。

    刻意排序：用户来查「我的 ID 是多少」时，要的是**最新那个**。
    不排序的话字典顺序（= 首次出现顺序）会把最早那个放最前，答非所问。

    每行补一个 ``matched``：这个人现在**在不在白名单里**。用户照着填之前
    就能看出哪条还缺，不必来回试。

    ``allowed`` **必须**传真正的白名单。踩过的坑：最初这里只回 ``matched=True``
    ——「这个人在事件里带了 ID」而已，跟白名单毫无关系。界面上那个标签写着
    「已在白名单」，而实际上对方根本还没被授权 —— 那比没有这个标签更坏。
    """
    if not isinstance(raw, dict):
        return []
    allow = allowed or frozenset()
    rows: list[dict[str, object]] = []
    for key, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        ids = {
            field: str(entry.get(field) or "")
            for field in ("open_id", "user_id", "union_id")
        }
        rows.append({
            "key": key,
            **ids,
            "is_group": bool(entry.get("is_group")),
            "seen_at": entry.get("seen_at"),
            # 和 ChannelService.is_allowed 同一套判据：多层求交集。
            "matched": bool({v for v in ids.values() if v} & allow),
        })
    rows.sort(key=lambda r: float(r.get("seen_at") or 0), reverse=True)
    return rows


def feishu_status(app: App) -> dict[str, object]:
    """桥接运行状态。**陈旧的「已连接」比没有状态更坏** —— 见 12.7。"""
    path = status_path(state_home_for(app))
    raw = read_status(path)
    now = time.time()
    stale = is_stale(raw, now=now)
    updated = raw.get("updated_at") if isinstance(raw, dict) else None
    age = (now - float(updated)) if isinstance(updated, (int, float)) else None
    data = raw or {}

    return {
        "kind": "feishu_status",
        # ``running`` = 状态新鲜 **且** 自称连上 **且** 不处于降级。
        #
        # 踩过的坑（测试抓出来的）：原先只看 ``connected``，而 ``degraded``
        # 状态下 ``connected`` 恰好是 ``True``（进程活着、连接也在，只是
        # bot 身份没探到）。于是界面会对一个**群里 @ 它不回**的桥接显示
        # 「已连接」—— 那正是 12.7 要防的「分不清原因」。
        "running": bool(
            raw is not None
            and not stale
            and data.get("connected")
            and data.get("state") != "degraded"
        ),
        "stale": stale,
        "stale_after_seconds": STALE_AFTER_SECONDS,
        "age_seconds": age,
        "status_file_exists": raw is not None,
        "status_path": str(path),
        "state": data.get("state") or "",
        "connected": bool(data.get("connected")),
        "pid": data.get("pid"),
        "bot_open_id": data.get("bot_open_id") or "",
        "bot_name": data.get("bot_name") or "",
        "allowed_users_count": data.get("allowed_users_count"),
        # 最近主动找过 bot 的人及其三层 ID。界面上照着填白名单 ——
        # 「不知道该填什么」是白名单最大的坑，见 record_sender 的说明。
        # 读白名单是为了算 matched（那个标签必须是真的）。
        "seen_senders": _seen_sender_rows(
            data.get("seen_senders"),
            frozenset(
                part.strip()
                for part in read_env(
                    state_home_for(app)
                ).get("FEISHU_ALLOWED_USERS", "").split(",")
                if part.strip()
            ),
        ),
        "lock_port": data.get("lock_port"),
        "dedup_entries": data.get("dedup_entries"),
        "last_error": data.get("last_error") or "",
    }


def feishu_log(app: App, query: dict[str, list[str]]) -> dict[str, object]:
    """桥接日志的尾部。

    **日志落盘要显式开**：桥接默认只写 stdout，所以从界面启动必须带重定向。
    这件事刻意在这里说清 —— 否则「日志区空白」会被当成「没出错」，
    而真相是「压根没在写」。
    """
    path = _log_path(app)
    try:
        want = int((query.get("lines") or [LOG_TAIL_DEFAULT])[0])
    except (TypeError, ValueError):
        want = LOG_TAIL_DEFAULT
    want = max(1, min(want, LOG_TAIL_MAX))

    if not path.exists():
        return {
            "kind": "feishu_log",
            "exists": False,
            "path": str(path),
            "lines": [],
            "notice": (
                "还没有日志文件。桥接默认把日志打到 stdout，不落盘 —— "
                "从界面启动时需要重定向到这个路径，否则这里永远是空的。"
            ),
        }
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {
            "kind": "feishu_log",
            "exists": True,
            "path": str(path),
            "lines": [],
            "notice": f"读不出来：{exc}",
        }
    all_lines = text.splitlines()
    return {
        "kind": "feishu_log",
        "exists": True,
        "path": str(path),
        "total_lines": len(all_lines),
        "lines": all_lines[-want:],
        "notice": "",
    }
