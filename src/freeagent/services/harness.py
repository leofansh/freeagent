"""Harness（V1.143）：把「它为你累积了什么」摆在一处 —— **只读聚合，不新建存储**。

## 为什么要有这一页

V1.139 把「这个工作区累积了什么」放进了工作台左栏（备注 / 跑过 / 背景 / 规则），
但那只是**一个工作区**的回显。人问「它到底记住了我多少东西」时，答案散在四处：
工作区档案与 `AGENTS.md`、core 记忆（`memory` 表）、事务与承诺、设置（模型 /
凭据 / 授权档）。散着就等于没有入口 —— 「越用越值钱」这句话必须有一个能点开的
地方，否则它只是一句叙事。

这一页名字叫 **Harness**：不是新能力，是给已有的四样东西一个统一名字和一处门。

## 三条纪律

1. **只读**：不写表、不建表、不顺延事务。事务计数走 ``task_repo.list_all()``
   过滤 ``is_open``，**不调 ``today.view()``** —— 那个会顺延并往 ``task_records``
   留记录（V1.115 已经踩过一次：注入层顺手调它，结果每次刷新都在「送达」）。
2. **一块读不出来不拖垮其它块**：每块独立 try，失败在 ``notes`` 里点名原因。
   **绝不把「读不出来」写成 0** —— 那是替人下结论（同 V1.139 的轮次数）。
3. **不解析 ``AGENTS.md`` 的内容**：只报「在不在、多少行、多大、何时改过」。
   规则的解释权在引擎（它自己读那个文件），这里再解析一遍就成了第二个真源，
   而第二个真源迟早与第一个分叉（12.1.1 的同一条道理）。

## 为什么记忆总数允许是 ``None``

``MemoryService.list_all`` 只给「最近 N 条」，不给总数。少于上限时那批就是全部
（精确），恰好等于上限时**只能报「至少这么多」** —— 于是给 ``None`` 并在
``notes`` 里说清。编一个数比给 ``None`` 更糟：人会用那个数判断「我记了多少」。
"""

from __future__ import annotations

import dataclasses
import pathlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..app import App
from ..paths import state_dir

__all__ = [
    "HarnessView", "ProjectFact", "RuleFact", "MemoryItem", "view",
    "RULE_NAME", "MEMORY_LIST_LIMIT", "RULE_READ_BYTES",
]

#: 项目规矩的文件名。引擎自己会读它；这里只报文件事实。
RULE_NAME = "AGENTS.md"
#: 记忆一次读多少条。少于这个数时「这批就是全部」，等于时只能报「至少」。
MEMORY_LIST_LIMIT = 200
#: 数行时只读这么多字节。``AGENTS.md`` 里承载的是论证，几千行是常态；
#: 为了一个行数把几 MB 读进内存不值得 —— 超了就如实说「没数完」。
RULE_READ_BYTES = 8192


@dataclass(frozen=True, slots=True)
class RuleFact:
    """项目规矩文件的**文件事实**（不是内容）。"""

    path: str
    exists: bool
    lines: int | None
    size: int | None
    updated_at: str = ""
    note: str = ""


@dataclass(frozen=True, slots=True)
class ProjectFact:
    id: str
    name: str
    directory: str
    note: str
    turns: int | None
    rule: RuleFact | None


@dataclass(frozen=True, slots=True)
class MemoryItem:
    kind: str
    scope: str
    content: str
    created_at: str


@dataclass
class HarnessView:
    projects: list[ProjectFact] = field(default_factory=list)
    memory: list[MemoryItem] = field(default_factory=list)
    #: ``None`` = 只读了最近一批、不知道一共多少（见模块说明）。
    memory_total: int | None = None
    #: 计数块：读得到才有键。**缺键 ≠ 0**。
    workload: dict[str, int] = field(default_factory=dict)
    #: 能力面（Skills）。本仓没有用户级能力库，这里如实说，不假装有。
    skills: dict[str, Any] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)
    #: 读不出来的那些事，逐条说清。
    notes: list[str] = field(default_factory=list)


def _rule_fact(directory: str) -> RuleFact | None:
    """看一个目录里的 ``AGENTS.md``。目录为空 / 不存在时返回 ``None``。

    ``exists=False`` 与「没有目录」是两件事：前者是「这个工作区没写规矩」，
    后者是「这个工作区没记目录」—— 在界面上是两句话。
    """
    directory = (directory or "").strip()
    if not directory:
        return None
    path = pathlib.Path(directory) / RULE_NAME
    try:
        stat = path.stat()
    except OSError:
        return RuleFact(path=str(path), exists=False, lines=None, size=None,
                        note="这个目录里没有这个文件")
    lines: int | None = None
    note = ""
    try:
        with path.open("rb") as handle:
            head = handle.read(RULE_READ_BYTES)
        lines = len(head.splitlines())
        if stat.st_size > RULE_READ_BYTES:
            # 只数了前 8 KB：报出去的数必须说清它是「前多少」的行数。
            lines = None
            note = f"只读了前 {RULE_READ_BYTES // 1024} KB，行数没数完"
    except OSError as exc:
        note = f"读不出来：{exc}"
    updated = datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds")
    return RuleFact(path=str(path), exists=True, lines=lines,
                    size=stat.st_size, updated_at=updated, note=note)


def _projects(app: App, notes: list[str]) -> list[ProjectFact]:
    repo = getattr(app, "project_repo", None)
    if repo is None:
        notes.append("项目档案读不出来：这套装配里没有项目仓储")
        return []
    turns_repo = getattr(app, "turn_repo", None)
    out: list[ProjectFact] = []
    try:
        rows = repo.list_all()
    except Exception as exc:  # noqa: BLE001 - 一块失败不该拖垮整页
        notes.append(f"项目档案读不出来：{exc}")
        return []
    for row in rows:
        directory = str(row.get("directory") or "")
        try:
            turns = (turns_repo.count_by_directory(directory)
                     if turns_repo is not None else None)
        except Exception:  # noqa: BLE001 - 轮次数是附加项，读不到就 None
            turns = None
        out.append(ProjectFact(
            id=str(row.get("id") or ""),
            name=str(row.get("name") or ""),
            directory=directory,
            note=str(row.get("note") or ""),
            turns=turns,
            rule=_rule_fact(directory),
        ))
    return out


def _memory(app: App, notes: list[str]) -> tuple[list[MemoryItem], int | None]:
    try:
        entries = app.memory.list_all(limit=MEMORY_LIST_LIMIT)
    except Exception as exc:  # noqa: BLE001
        notes.append(f"记忆读不出来：{exc}")
        return [], None
    items = [
        MemoryItem(
            kind=str(getattr(e, "kind", "") or ""),
            scope=str(getattr(e, "scope", "") or ""),
            content=str(getattr(e, "content", "") or ""),
            created_at=str(getattr(e, "created_at", "") or ""),
        )
        for e in entries
    ]
    if len(entries) >= MEMORY_LIST_LIMIT:
        notes.append(
            f"记忆只读了最近 {MEMORY_LIST_LIMIT} 条 —— 比这更多，条数不知道")
        return items, None
    return items, len(entries)


def _workload(app: App, notes: list[str]) -> dict[str, int]:
    """三个计数。**每项独立 try，读不到就不给键**（而不是给 0）。"""
    out: dict[str, int] = {}
    try:
        tasks = app.task_repo.list_all()
        out["open_tasks"] = len([t for t in tasks if t.is_open])
    except Exception as exc:  # noqa: BLE001
        notes.append(f"未结束事务数读不出来：{exc}")
    try:
        out["open_promises"] = int(app.promise.open_count())
    except Exception as exc:  # noqa: BLE001
        notes.append(f"未兑现承诺数读不出来：{exc}")
    try:
        out["pending"] = int(app.pending.count())
    except Exception as exc:  # noqa: BLE001
        notes.append(f"待处理条数读不出来：{exc}")
    return out


def _skills() -> dict[str, Any]:
    """能力面。**本仓没有用户级能力库** —— 如实说，不凑数。

    本仓的能力来自两处：引擎（会改文件、跑命令）与规则层（问答 / 记账）。
    「可反复调用的能力包」这一层从来没有建过，界面上写「有 N 个」就是撒谎。
    """
    return {
        "available": False,
        "reason": ("这个仓库还没有可反复调用的能力库（Skills）—— "
                   "能力来自引擎，规矩来自项目里的 AGENTS.md。"),
    }


def _settings(app: App, mode: str) -> dict[str, Any]:
    config = getattr(app, "config", None)
    try:
        configured = bool(config.has_credentials) if config is not None else None
    except Exception:  # noqa: BLE001 - 凭据判定失败就如实 unknown
        configured = None
    try:
        # 传 ``config.home``：装配里它已是状态目录（``--db`` 的父目录），
        # 不传会回 ``~/.steward`` —— 测试与多实例下就报错路径了。
        data_dir = str(state_dir(getattr(config, "home", None)))
    except Exception:  # noqa: BLE001
        data_dir = ""
    return {
        "provider": app.llm_name,
        "key_configured": configured,
        "mode": mode,
        "data_dir": data_dir,
    }


def view(app: App, *, mode: str = "") -> HarnessView:
    """聚合四块。``mode``（授权档）由端点从偏好里取进来 —— 服务层不读 web 偏好。

    每块自己 catch：一块读不出来只让那一块空着并写进 ``notes``，其余照常。
    """
    return _build(app, mode, [])


def _build(app: App, mode: str, notes: list[str]) -> HarnessView:
    items, total = _memory(app, notes)
    return HarnessView(
        projects=_projects(app, notes),
        memory=items,
        memory_total=total,
        workload=_workload(app, notes),
        skills=_skills(),
        settings=_settings(app, mode),
        notes=notes,
    )


def as_dict(view: HarnessView) -> dict[str, Any]:
    """给端点的形状。``dataclasses.asdict`` 直出 —— 不做措辞加工（L5：文案不是契约）。"""
    return dataclasses.asdict(view)
