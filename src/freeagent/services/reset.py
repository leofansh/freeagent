"""恢复出厂：清空全部事务数据，角色脉络与全部配置保留。

## 为什么独立成模块

这是整个仓里**唯一**真正删数据的功能（其余全是软删——状态置
DROPPED、记录照留）。所以刻意与 :mod:`tasks` 分开：那边管「一条一条
地改」，这边管「一把清空」。混在一起的后果是有人在事务服务里翻出
删除能力来用，而它绕开了这里的三道安全姿态。

## 安全姿态（fail-closed，三道，缺一道就不执行）

1. **服务在跑就拒绝** —— 执行器 / 桥接持着同一个库轮询落事件，
   抹到一半又被写回来，比不抹更糟。拒绝信息里说清怎么停。
2. **强制先备份** —— sqlite3 在线备份 API（``conn.backup``），
   天然包含 WAL 里未合并的内容；文件名带时间戳，还原命令随结果
   一起返回。**备份失败 = 拒绝清空**，不提供「不备份也清」的选项。
3. **调用方二次确认** —— 口令由调用方（终端/Web 表单）校验后传
   ``confirmed=True``。本模块不自己 ``input()``：输入方式有两种
   （终端 input / 网页框），放服务层就得引一套交互抽象，而抽象
   往往就是被绕过的口子。

## 清除范围（钉死，别扩大）

**清**：tasks、task_records、task_roles、task_dependencies、
artifacts、pending_approvals、stop_requests。
**留**：roles 与 role_knowledge（脉络是结构，不是记录）、以及全部
配置（config.json / llm.env / feishu.env / oc_selection / 飞书状态
文件）。清配置等于让用户重配一遍，那是另一种事故。
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime
import sqlite3
import subprocess
from pathlib import Path

__all__ = [
    "RESET_PHRASE",
    "ResetRefused",
    "ResetResult",
    "WIPE_ORDER",
    "backup_database",
    "db_path_of",
    "reset_factory",
    "running_services",
    "wipe_transaction_data",
]

#: 「恢复出厂」的确认口令。**终端与 Web 共用这一份**（各自 import 它）——
#: 各写一份的话，其中一边改了另一边不报错，只是静静地接受旧口令，
#: 而用户按界面提示输入却被拒，看起来像功能坏了。
#:
#: 刻意用一整句而不是「确定」：口令的作用是让人慢一秒。这句话要逐字
#: 输入，顺手敲不过去；而真要清空的人不会介意多打九个字。
RESET_PHRASE = "我确定清空所有事务数据"

#: 进程命令行里的特征串。刻意用模块路径而不是 "python" —— 按命令行找、
#: 不靠 pid（README 的排查命令同一思路）：pid 会复用，命令行不会。
_EXECUTOR_MARK = "freeagent.delegate"
_BRIDGE_MARK = "freeagent.feishu.bridge"

#: 子表先删、父表（tasks）最后。FK 开着，顺序反了会当场违反约束。
WIPE_ORDER: tuple[str, ...] = (
    "task_records",
    "task_roles",
    "task_dependencies",
    "artifacts",
    "pending_approvals",
    "stop_requests",
    "tasks",
)

#: 备份文件名后缀。跟随用户机器上已有的 ``.bak-*`` 习惯
#:（``~/.freeagent`` 下一批 agent.db.bak-*）。
_BACKUP_TAG = "bak-factory"


class ResetRefused(RuntimeError):
    """拒绝执行。**消息是给用户看的**，必须说清为什么、下一步做什么。"""


@dataclasses.dataclass(frozen=True, slots=True)
class ResetResult:
    """一次恢复出厂的结果。"""

    db: Path
    backup: Path
    deleted: dict[str, int]
    restore_hint: str


def running_services() -> list[str]:
    """列出**在跑**的相关服务名。检测不到平台 ⇒ 抛（fail-closed）。

    为什么不「检测不到就放行」：那等于让「没装 powershell 的机器」
    和「服务恰好没在跑」共用同一条路径，而前者是不该清空的场景。
    宁可让用户在受支持的平台上跑（本仓的目标环境就是 Windows）。
    """
    if _on_windows():
        return _running_services_windows()
    if Path("/proc").is_dir():
        return _running_services_proc()
    raise ResetRefused(
        "无法检测服务进程状态（未知平台）—— 为安全起见拒绝清空。"
        "请手动确认执行器与桥接都没在跑，然后用 --force 明确放过检测。"
    )


def _on_windows() -> bool:
    import os

    return os.name == "nt"


def _psql_rows(script: str) -> list[str]:
    """跑一段 powershell，返回非空输出行。跑不动 ⇒ 空列表。"""
    try:
        proc = subprocess.run(  # noqa: S603 - argv 固定，无用户输入
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]


def _running_services_windows() -> list[str]:
    rows = _psql_rows(
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" "
        "| Select-Object -ExpandProperty CommandLine"
    )
    return _services_from(rows)


def _running_services_proc() -> list[str]:
    rows: list[str] = []
    for entry in sorted(Path("/proc").glob("[0-9]*/cmdline")):
        try:
            raw = entry.read_bytes().replace(b"\x00", b" ").decode(
                "utf-8", "replace"
            )
        except OSError:
            continue
        rows.append(raw)
    return _services_from(rows)


def _services_from(cmdlines: list[str]) -> list[str]:
    found: list[str] = []
    joined = "\n".join(cmdlines)
    if _BRIDGE_MARK in joined:
        found.append("飞书桥接（freeagent.feishu.bridge）")
    if _EXECUTOR_MARK in joined:
        found.append("委派执行器（freeagent.delegate）")
    return found


def db_path_of(conn: sqlite3.Connection) -> Path:
    """从**活连接**问出它此刻连的是哪个库文件。

    不重新解析（``resolve_db_path``）：调用方显式 ``--db`` 时重新解析
    会得到另一个路径，于是备份了一个库、清空的是另一个 —— 而这种错
    没有任何报错，只是「备份怎么是旧的」。
    """
    row = conn.execute("PRAGMA database_list").fetchone()
    if not row or not row[2]:
        raise ResetRefused("问不出当前库路径 —— 拒绝清空")
    return Path(row[2])


def backup_database(db_path: Path) -> Path:
    """在线备份（含 WAL 内容）到带时间戳的兄弟文件。失败 ⇒ 抛。

    用 sqlite3 的 backup API 而不是文件复制：WAL 模式下库里最新的
    提交还在 ``-wal`` 里，直接复制 ``.db`` 会得到一个**过期快照**，
    而用户以为备份是好的。备份失败时绝不继续清空 —— 没有退路
    的操作不该给「跳过备份」的选项。
    """
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = db_path.with_name(f"{db_path.name}.{_BACKUP_TAG}-{stamp}")
    src = sqlite3.connect(db_path)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    if not dest.is_file() or dest.stat().st_size == 0:
        raise ResetRefused(f"备份没写成功：{dest}——拒绝清空")
    return dest


def wipe_transaction_data(db_path: Path) -> dict[str, int]:
    """一个事务里清完所有事务表，返回各表删除行数。

    用**新连接**且 autocommit：调用方（CLI/Web）自己持着库连接，
    在这里嵌套事务会把两边的状态搅在一起。显式 BEGIN/COMMIT 而不是
    靠隐式事务 —— 「要么全清要么全不清」必须看得见。
    """
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        counts: dict[str, int] = {}
        for table in WIPE_ORDER:
            cur = conn.execute(f"DELETE FROM {table}")  # noqa: S608 - 表名是模块常量
            counts[table] = cur.rowcount if cur.rowcount is not None else 0
        conn.execute("COMMIT")
        conn.execute("VACUUM")
        return counts
    except Exception:
        with contextlib.suppress(Exception):
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def reset_factory(db_path: Path, *, confirmed: bool) -> ResetResult:
    """恢复出厂。三道闸：确认 → 服务检测 → 备份，全过才抹。"""
    if not confirmed:
        raise ResetRefused(
            "未确认。这一步会清空全部事务数据（角色脉络与配置保留），"
            "确认时请输入指定口令。"
        )
    running = running_services()
    if running:
        raise ResetRefused(
            "这些服务还在跑，拒绝清空（它们正在写同一个库）：\n  - "
            + "\n  - ".join(running)
            + "\n先停掉它们再试。两个停法：\n"
            "  1) 桥接可以在网页「飞书通道」页签点停止；\n"
            "  2) 执行器没有界面入口（独立进程是刻意设计），找它的终端窗口，\n"
            "     或用命令行按 pid 停（**别用 taskkill /f /im python.exe** ——\n"
            "     那会把机器上所有 python 一起杀）：\n"
            '     powershell -NoProfile -Command "Get-CimInstance Win32_Process '
            '-Filter \\"Name=\'python.exe\'\\" | Select-Object ProcessId,CommandLine"\n'
            "     照输出找 freeagent.delegate 那行，taskkill /f /pid <pid>。"
        )
    backup = backup_database(db_path)
    deleted = wipe_transaction_data(db_path)
    return ResetResult(
        db=db_path,
        backup=backup,
        deleted=deleted,
        restore_hint=(
            f"要还原：停掉执行器与桥接，然后\n"
            f'  copy /y "{backup}" "{db_path}"\n'
            f'  del "{db_path}-wal" "{db_path}-shm" 2>nul'
        ),
    )
