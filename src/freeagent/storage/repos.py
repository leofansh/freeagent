"""仓储层：只存取，不做业务判断。

顺延、权重计算、状态迁移合法性一律不在这里 —— 见设计文档 12.3 分层纪律。
每个写方法都负责维护 ``updated_at``。
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from contextlib import AbstractContextManager, nullcontext
from datetime import date, datetime
from typing import Iterable, Sequence, TypeVar, cast

from ..domain import (
    Artifact,
    ArtifactStatus,
    ConflictError,
    InvariantViolation,
    NotFoundError,
    RecordType,
    Role,
    StaleRevisionError,
    Task,
    TaskKind,
    TaskRecord,
    TaskState,
    ValidationError,
    WaitingKind,
    WaitingOn,
    new_id,
)

__all__ = ["RoleRepo", "TaskRepo", "RecordRepo", "ArtifactRepo"]

_TERMINAL_VALUES = (TaskState.DONE.value, TaskState.DROPPED.value)
#: 未结束 = 全部状态减去已结束。顺序按生命周期，查询时的 IN 列表也用它。
_OPEN_VALUES = (
    TaskState.INBOX.value,
    TaskState.ACTIVE.value,
    TaskState.BLOCKED.value,
)

_TASK_COLUMNS = (
    "title",
    "state",
    "kind",
    "intent",
    "definition_of_done",
    "scheduled_for",
    "due_time",
    "reminder_time",
    "reminder_rule",
    "waiting_on",
    "entered_at",
    "created_at",
    "updated_at",
    "completed_at",
    "dropped_at",
    "last_resumed_at",
    "progress_note",
    "project_path",
    "delegate_chat_id",
    "current_artifact_id",
)

_TASK_INSERT_COLUMNS = ("id",) + _TASK_COLUMNS


# --------------------------------------------------------------------------- #
# 序列化helper
# --------------------------------------------------------------------------- #
def _dt_out(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _dt_in(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _d_out(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _d_in(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def _waiting_out(value: WaitingOn | None) -> str | None:
    if value is None:
        return None
    return json.dumps(
        {
            "kind": value.kind.value,
            "who_or_what": value.who_or_what,
            "since": value.since.isoformat(),
            "follow_up_at": _dt_out(value.follow_up_at),
            "note": value.note,
        },
        ensure_ascii=False,
    )


def _waiting_in(raw: str | None) -> WaitingOn | None:
    if not raw:
        return None
    payload = json.loads(raw)
    since = datetime.fromisoformat(payload["since"])
    return WaitingOn(
        kind=WaitingKind(payload["kind"]),
        who_or_what=payload["who_or_what"],
        since=since,
        follow_up_at=_dt_in(payload.get("follow_up_at")),
        note=payload.get("note"),
    )


def _dedup(values: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return tuple(out)


class _RepoBase:
    """仓储基类：统一管理事务所有权。

    ``autocommit=True``（默认）时每个写方法自己提交。
    ``autocommit=False`` 时**不提交**，由上层服务用
    :func:`freeagent.storage.db.transaction` 持有事务，
    从而支持「角色合并」这类跨仓储的原子操作。
    """

    def __init__(self, conn: sqlite3.Connection, *, autocommit: bool = True) -> None:
        self._conn = conn
        self._autocommit = autocommit

    def _tx(self) -> AbstractContextManager[sqlite3.Connection]:
        return self._conn if self._autocommit else nullcontext()


# --------------------------------------------------------------------------- #
# RoleRepo
# --------------------------------------------------------------------------- #
class RoleRepo(_RepoBase):
    def add(
        self,
        name: str,
        now: datetime,
        *,
        role_id: str | None = None,
        note: str | None = None,
        default_definition_of_done: str | None = None,
        active: bool = True,
        icon: str | None = None,
        color: str | None = None,
    ) -> Role:
        if not name.strip():
            raise ValidationError("角色名不能为空")
        role = Role(
            id=role_id or new_id(),
            name=name.strip(),
            created_at=now,
            updated_at=now,
            note=note,
            default_definition_of_done=default_definition_of_done,
            active=active,
            icon=icon,
            color=color,
        )
        try:
            with self._tx():
                self._conn.execute(
                    "INSERT INTO roles (id, name, note, default_definition_of_done,"
                    " active, icon, color, merged_into, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        role.id,
                        role.name,
                        role.note,
                        role.default_definition_of_done,
                        int(role.active),
                        role.icon,
                        role.color,
                        role.merged_into,
                        role.created_at.isoformat(),
                        role.updated_at.isoformat(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ValidationError(f"角色名已存在: {name}") from exc
        return role

    def get(self, role_id: str) -> Role:
        row = self._conn.execute("SELECT * FROM roles WHERE id = ?", (role_id,)).fetchone()
        if row is None:
            raise NotFoundError("角色", role_id)
        return self._row_to_role(row)

    def find(self, role_id: str) -> Role | None:
        row = self._conn.execute("SELECT * FROM roles WHERE id = ?", (role_id,)).fetchone()
        return self._row_to_role(row) if row is not None else None

    def get_by_name(self, name: str) -> Role:
        row = self._conn.execute(
            "SELECT * FROM roles WHERE name = ?", (name.strip(),)
        ).fetchone()
        if row is None:
            raise NotFoundError("角色", name)
        return self._row_to_role(row)

    def find_by_name(self, name: str) -> Role | None:
        row = self._conn.execute(
            "SELECT * FROM roles WHERE name = ?", (name.strip(),)
        ).fetchone()
        return self._row_to_role(row) if row is not None else None

    def list_all(
        self, *, include_merged: bool = False, include_inactive: bool = True
    ) -> list[Role]:
        sql = "SELECT * FROM roles WHERE 1=1"
        params: list[object] = []
        if not include_merged:
            sql += " AND merged_into IS NULL"
        if not include_inactive:
            sql += " AND active = 1"
        sql += " ORDER BY created_at ASC, rowid ASC"
        return [self._row_to_role(r) for r in self._conn.execute(sql, params)]

    def update(self, role: Role, now: datetime) -> Role:
        updated = _replace(role, updated_at=now)
        with self._tx():
            cursor = self._conn.execute(
                "UPDATE roles SET name=?, note=?, default_definition_of_done=?,"
                " active=?, icon=?, color=?, merged_into=?, updated_at=? WHERE id=?",
                (
                    updated.name,
                    updated.note,
                    updated.default_definition_of_done,
                    int(updated.active),
                    updated.icon,
                    updated.color,
                    updated.merged_into,
                    now.isoformat(),
                    updated.id,
                ),
            )
        if cursor.rowcount == 0:
            raise NotFoundError("角色", updated.id)
        return updated

    def set_merged_into(self, role_id: str, target_id: str | None, now: datetime) -> Role:
        with self._tx():
            cursor = self._conn.execute(
                "UPDATE roles SET merged_into=?, updated_at=? WHERE id=?",
                (target_id, now.isoformat(), role_id),
            )
        if cursor.rowcount == 0:
            raise NotFoundError("角色", role_id)
        return self.get(role_id)

    def resolve(self, role_id: str) -> Role:
        """沿 ``merged_into`` 传递追踪到根角色。成环则抛错。"""
        current = self.get(role_id)
        visited = {current.id}
        while current.merged_into is not None:
            nxt = self.get(current.merged_into)
            if nxt.id in visited:
                raise ValidationError(f"角色合并链成环: {role_id}")
            visited.add(nxt.id)
            current = nxt
        return current

    def delete(self, role_id: str) -> None:
        with self._tx():
            try:
                cursor = self._conn.execute("DELETE FROM roles WHERE id = ?", (role_id,))
            except sqlite3.IntegrityError as exc:
                raise ConflictError(
                    f"角色 {role_id} 仍被事务引用，请先合并或改归属"
                ) from exc
        if cursor.rowcount == 0:
            raise NotFoundError("角色", role_id)

    def count_referencing_tasks(self, role_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM task_roles WHERE role_id = ?", (role_id,)
        ).fetchone()
        return int(row[0])

    @staticmethod
    def _row_to_role(row: sqlite3.Row) -> Role:
        return Role(
            id=row["id"],
            name=row["name"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            note=row["note"],
            default_definition_of_done=row["default_definition_of_done"],
            active=bool(row["active"]),
            icon=row["icon"],
            color=row["color"],
            merged_into=row["merged_into"],
        )


# --------------------------------------------------------------------------- #
# TaskRepo
# --------------------------------------------------------------------------- #
class TaskRepo(_RepoBase):
    # -- 写 ------------------------------------------------------------------ #
    def add(self, task: Task) -> Task:
        if not task.role_ids:
            raise InvariantViolation("事务至少属于一个角色")
        role_ids = _dedup(task.role_ids)
        if not role_ids:
            raise InvariantViolation("事务至少属于一个角色")
        try:
            with self._tx():
                self._conn.execute(
                    f"INSERT INTO tasks ({','.join(_TASK_INSERT_COLUMNS)})"
                    f" VALUES ({','.join('?' * len(_TASK_INSERT_COLUMNS))})",
                    self._task_params(task),
                )
                self._write_roles(task.id, role_ids)
                self._write_deps(task.id, task.blocked_by)
        except sqlite3.IntegrityError as exc:
            raise InvariantViolation(f"事务引用了不存在的角色: {task.role_ids}") from exc
        return _replace(task, role_ids=role_ids)

    def update(self, task: Task, now: datetime) -> Task:
        """写入，并把 ``revision`` 自增一。

        **乐观并发**：``task.revision`` 必须是**读出来那一刻**的值。
        库里当前值与之不符说明中间有人改过 —— 抛
        :class:`~freeagent.domain.errors.StaleRevisionError`，**不覆盖**。

        现有服务方法全是「``get()`` → ``_replace()`` → ``update()``」，
        ``get()`` 读到的就是当前 revision，所以它们全都照常工作；
        冲突只在真的有并发写入时触发。覆盖是最后手段 ——
        提醒改期与委派状态都是「读-改-写」，静默覆盖会把对方的改动吃掉，
        而症状是「我明明改过了，怎么又变回去了」。
        """
        role_ids = _dedup(task.role_ids)
        if not role_ids:
            raise InvariantViolation("事务至少属于一个角色")
        assignments = ", ".join(f"{col}=?" for col in _TASK_COLUMNS)
        with self._tx():
            row = self._conn.execute(
                "SELECT revision FROM tasks WHERE id=?", (task.id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("事务", task.id)
            current_revision = int(row[0])
            if current_revision != task.revision:
                raise StaleRevisionError(
                    "事务", task.id, task.revision, current_revision
                )
            next_revision = current_revision + 1
            updated = _replace(
                task, updated_at=now, revision=next_revision, role_ids=role_ids
            )
            cursor = self._conn.execute(
                f"UPDATE tasks SET {assignments}, revision=? WHERE id=?",
                (*self._task_field_params(updated), next_revision, updated.id),
            )
            if cursor.rowcount == 0:
                raise NotFoundError("事务", updated.id)
            self._write_roles(updated.id, role_ids)
            self._write_deps(updated.id, _dedup(updated.blocked_by))
        return _replace(updated, role_ids=role_ids)

    def replace_roles(self, task_id: str, role_ids: Sequence[str], now: datetime) -> Task:
        """事务性替换角色集合：去重、保序。"""
        new_roles = _dedup(role_ids)
        if not new_roles:
            raise InvariantViolation("事务至少属于一个角色")
        try:
            with self._tx():
                cursor = self._conn.execute(
                    "UPDATE tasks SET updated_at=?, revision=revision+1 WHERE id=?",
                    (now.isoformat(), task_id),
                )
                if cursor.rowcount == 0:
                    raise NotFoundError("事务", task_id)
                self._write_roles(task_id, new_roles)
        except sqlite3.IntegrityError as exc:
            raise InvariantViolation(f"角色不存在，无法归属: {role_ids}") from exc
        return self.get(task_id)

    def set_state(
        self,
        task_id: str,
        state: TaskState,
        now: datetime,
        *,
        completed_at: datetime | None = None,
        dropped_at: datetime | None = None,
    ) -> Task:
        with self._tx():
            cursor = self._conn.execute(
                "UPDATE tasks SET state=?, completed_at=?, dropped_at=?, updated_at=?,"
                " revision=revision+1 WHERE id=?",
                (
                    state.value,
                    _dt_out(completed_at),
                    _dt_out(dropped_at),
                    now.isoformat(),
                    task_id,
                ),
            )
        if cursor.rowcount == 0:
            raise NotFoundError("事务", task_id)
        return self.get(task_id)

    def set_schedule(self, task_id: str, scheduled_for: date | None, now: datetime) -> Task:
        with self._tx():
            cursor = self._conn.execute(
                "UPDATE tasks SET scheduled_for=?, updated_at=?,"
                " revision=revision+1 WHERE id=?",
                (_d_out(scheduled_for), now.isoformat(), task_id),
            )
        if cursor.rowcount == 0:
            raise NotFoundError("事务", task_id)
        return self.get(task_id)

    def set_dependencies(self, task_id: str, blocked_by: Sequence[str]) -> Task:
        try:
            with self._tx():
                self._write_deps(task_id, _dedup(blocked_by))
                cursor = self._conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,))
                if cursor.fetchone() is None:
                    raise NotFoundError("事务", task_id)
        except sqlite3.IntegrityError as exc:
            raise InvariantViolation(f"依赖的事务不存在: {blocked_by}") from exc
        return self.get(task_id)

    def set_progress_note(self, task_id: str, note: str | None) -> Task:
        """写入派生缓存 ``progress_note``。

        **刻意不更新 ``updated_at``，也不推进 ``revision``。**
        写派生缓存不是用户活动 —— 若在这里更新时间戳，每次重建进度摘要都会
        被当成「刚刚动过」，``RESUME_STALE`` 信号就永远不会触发了。

        不推进 ``revision`` 是同一个道理的第二面：``revision`` 回答的是
        「这条事务的状态被**人**改过吗」，而缓存是从日志重算出来的。
        跟着它一起 bump 的话，两人各记一笔笔记（``note()`` 会顺带重建缓存）
        就会互相冲突 —— 而那没有任何不变量被破坏，只是缓存重算了两遍。
        """
        with self._tx():
            cursor = self._conn.execute(
                "UPDATE tasks SET progress_note=? WHERE id=?", (note, task_id)
            )
        if cursor.rowcount == 0:
            raise NotFoundError("事务", task_id)
        return self.get(task_id)

    def set_current_artifact(self, task_id: str, artifact_id: str | None, now: datetime) -> Task:
        with self._tx():
            cursor = self._conn.execute(
                "UPDATE tasks SET current_artifact_id=?, updated_at=?,"
                " revision=revision+1 WHERE id=?",
                (artifact_id, now.isoformat(), task_id),
            )
        if cursor.rowcount == 0:
            raise NotFoundError("事务", task_id)
        return self.get(task_id)

    def touch_resumed(self, task_id: str, now: datetime) -> Task:
        with self._tx():
            cursor = self._conn.execute(
                "UPDATE tasks SET last_resumed_at=?, updated_at=?,"
                " revision=revision+1 WHERE id=?",
                (now.isoformat(), now.isoformat(), task_id),
            )
        if cursor.rowcount == 0:
            raise NotFoundError("事务", task_id)
        return self.get(task_id)

    def delete(self, task_id: str) -> None:
        with self._tx():
            cursor = self._conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        if cursor.rowcount == 0:
            raise NotFoundError("事务", task_id)

    # -- 读 ------------------------------------------------------------------ #
    def get(self, task_id: str) -> Task:
        row = self._conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise NotFoundError("事务", task_id)
        return self._hydrate(row)

    def find(self, task_id: str) -> Task | None:
        row = self._conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return self._hydrate(row) if row is not None else None

    def list_all(self) -> list[Task]:
        """按创建顺序返回。排序的同分 tie-break 依赖这个顺序（稳定排序）。"""
        rows = self._conn.execute("SELECT * FROM tasks ORDER BY rowid ASC")
        return [self._hydrate(r) for r in rows]

    def list_by_state(self, state: TaskState) -> list[Task]:
        rows = self._conn.execute(
            "SELECT * FROM tasks WHERE state = ? ORDER BY entered_at ASC, rowid ASC",
            (state.value,),
        )
        return [self._hydrate(r) for r in rows]

    def list_closed_between(self, start: datetime, end: datetime) -> list[Task]:
        """在 ``[start, end)`` 内**结束**的事务，按结束时间倒序。

        问「我上周完成了什么」需要它 —— 之前没有按完成时间查的路径，
        只能靠 ``list_by_state`` 拿到全部再在内存里筛，那样没法分页也不准
        （``completed_at`` 和 ``dropped_at`` 是两列，要分别看）。
        """
        rows = self._conn.execute(
            "SELECT * FROM tasks WHERE"
            " (completed_at >= ? AND completed_at < ?)"
            " OR (dropped_at >= ? AND dropped_at < ?)"
            " ORDER BY COALESCE(completed_at, dropped_at) DESC, rowid DESC",
            (start.isoformat(), end.isoformat(),
             start.isoformat(), end.isoformat()),
        )
        return [self._hydrate(r) for r in rows]

    def list_open(self) -> list[Task]:
        """未结束的事务，按创建顺序。

        与 ``TaskService.list_open`` 同义，放到仓储层是因为「未结束」这个
        谓词属于数据，不属于某个用例。
        """
        placeholders = ", ".join("?" * len(_OPEN_VALUES))
        rows = self._conn.execute(
            f"SELECT * FROM tasks WHERE state IN ({placeholders})"
            " ORDER BY entered_at ASC, rowid ASC",
            tuple(_OPEN_VALUES),
        )
        return [self._hydrate(r) for r in rows]

    def list_by_kind(self, kind: TaskKind) -> list[Task]:
        rows = self._conn.execute(
            "SELECT * FROM tasks WHERE kind = ? ORDER BY entered_at ASC, rowid ASC",
            (kind.value,),
        )
        return [self._hydrate(r) for r in rows]

    def list_by_scheduled_for(self, day: date) -> list[Task]:
        rows = self._conn.execute(
            "SELECT * FROM tasks WHERE scheduled_for = ? ORDER BY entered_at ASC, rowid ASC",
            (day.isoformat(),),
        )
        return [self._hydrate(r) for r in rows]

    def list_open_before(self, day: date) -> list[Task]:
        """顺延候选：排期早于 ``day`` 且未结束。"""
        placeholders = ", ".join("?" * len(_TERMINAL_VALUES))
        rows = self._conn.execute(
            "SELECT * FROM tasks WHERE scheduled_for IS NOT NULL"
            f" AND scheduled_for < ? AND state NOT IN ({placeholders})"
            " ORDER BY scheduled_for ASC, rowid ASC",
            (day.isoformat(), *_TERMINAL_VALUES),
        )
        return [self._hydrate(r) for r in rows]

    def list_by_role(self, role_id: str) -> list[Task]:
        """按角色 id 精确匹配。合并解析由服务层负责。"""
        rows = self._conn.execute(
            "SELECT t.* FROM tasks t"
            " JOIN task_roles tr ON tr.task_id = t.id"
            " WHERE tr.role_id = ?"
            " ORDER BY t.entered_at ASC, t.rowid ASC",
            (role_id,),
        )
        return [self._hydrate(r) for r in rows]

    def list_due_reminders(self, now: datetime) -> list[Task]:
        """已到点但未销账的提醒。"""
        rows = self._conn.execute(
            "SELECT * FROM tasks WHERE reminder_time IS NOT NULL"
            " AND reminder_time <= ?"
            " AND state NOT IN (?, ?)"
            " ORDER BY reminder_time ASC",
            (now.isoformat(), *_TERMINAL_VALUES),
        )
        return [self._hydrate(r) for r in rows]

    def get_dependencies(self, task_id: str) -> tuple[str, ...]:
        rows = self._conn.execute(
            "SELECT depends_on_task_id FROM task_dependencies"
            " WHERE task_id = ? ORDER BY depends_on_task_id",
            (task_id,),
        )
        return tuple(r[0] for r in rows)

    def count_dependents(self, task_id: str) -> int:
        """有多少未完成事务在依赖本事务（``DEPENDED_ON`` 信号用）。"""
        placeholders = ", ".join("?" * len(_TERMINAL_VALUES))
        row = self._conn.execute(
            "SELECT COUNT(*) FROM task_dependencies d"
            " JOIN tasks t ON t.id = d.task_id"
            " WHERE d.depends_on_task_id = ?"
            f" AND t.state NOT IN ({placeholders})",
            (task_id, *_TERMINAL_VALUES),
        ).fetchone()
        return int(row[0])

    # -- 内部 ---------------------------------------------------------------- #
    def _write_roles(self, task_id: str, role_ids: Sequence[str]) -> None:
        self._conn.execute("DELETE FROM task_roles WHERE task_id = ?", (task_id,))
        self._conn.executemany(
            "INSERT INTO task_roles (task_id, role_id, ord) VALUES (?,?,?)",
            [(task_id, role_id, idx) for idx, role_id in enumerate(role_ids)],
        )

    def _write_deps(self, task_id: str, blocked_by: Sequence[str]) -> None:
        self._conn.execute("DELETE FROM task_dependencies WHERE task_id = ?", (task_id,))
        self._conn.executemany(
            "INSERT INTO task_dependencies (task_id, depends_on_task_id) VALUES (?,?)",
            [(task_id, dep) for dep in blocked_by],
        )

    def _hydrate(self, row: sqlite3.Row) -> Task:
        role_ids = tuple(
            r[0]
            for r in self._conn.execute(
                "SELECT role_id FROM task_roles WHERE task_id = ? ORDER BY ord ASC",
                (row["id"],),
            )
        )
        if not role_ids:
            raise InvariantViolation(f"事务 {row['id']} 没有关联角色，数据已损坏")
        return Task(
            id=row["id"],
            title=row["title"],
            role_ids=role_ids,
            state=TaskState(row["state"]),
            kind=TaskKind(row["kind"]),
            intent=row["intent"],
            definition_of_done=row["definition_of_done"],
            scheduled_for=_d_in(row["scheduled_for"]),
            due_time=_dt_in(row["due_time"]),
            reminder_time=_dt_in(row["reminder_time"]),
            reminder_rule=row["reminder_rule"],
            revision=row["revision"],
            waiting_on=_waiting_in(row["waiting_on"]),
            blocked_by=self.get_dependencies(row["id"]),
            entered_at=datetime.fromisoformat(row["entered_at"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            completed_at=_dt_in(row["completed_at"]),
            dropped_at=_dt_in(row["dropped_at"]),
            last_resumed_at=_dt_in(row["last_resumed_at"]),
            progress_note=row["progress_note"],
            project_path=row["project_path"],
            delegate_chat_id=row["delegate_chat_id"],
            current_artifact_id=row["current_artifact_id"],
        )

    def _task_params(self, task: Task) -> tuple[object, ...]:
        """INSERT 用：``id`` + 全部字段。"""
        return (task.id, *self._task_field_params(task))

    def _task_field_params(self, task: Task) -> tuple[object, ...]:
        """全部可变字段，不含 ``id``。UPDATE 用。"""
        return (
            task.title,
            task.state.value,
            task.kind.value,
            task.intent,
            task.definition_of_done,
            _d_out(task.scheduled_for),
            _dt_out(task.due_time),
            _dt_out(task.reminder_time),
            task.reminder_rule,
            _waiting_out(task.waiting_on),
            task.entered_at.isoformat(),
            task.created_at.isoformat(),
            task.updated_at.isoformat(),
            _dt_out(task.completed_at),
            _dt_out(task.dropped_at),
            _dt_out(task.last_resumed_at),
            task.progress_note,
            task.project_path,
            task.delegate_chat_id,
            task.current_artifact_id,
        )


# --------------------------------------------------------------------------- #
# RecordRepo
# --------------------------------------------------------------------------- #
class RecordRepo(_RepoBase):
    def append(
        self, task_id: str, type_: RecordType, content: str, ts: datetime
    ) -> TaskRecord:
        record = TaskRecord(id=new_id(), task_id=task_id, ts=ts, type=type_, content=content)
        with self._tx():
            self._conn.execute(
                "INSERT INTO task_records (id, task_id, ts, type, content) VALUES (?,?,?,?,?)",
                (record.id, record.task_id, record.ts.isoformat(), record.type.value, record.content),
            )
        return record

    def list_for_task(self, task_id: str) -> list[TaskRecord]:
        rows = self._conn.execute(
            "SELECT * FROM task_records WHERE task_id = ? ORDER BY ts ASC, id ASC",
            (task_id,),
        )
        return [self._row_to_record(r) for r in rows]

    def recent(self, task_id: str, limit: int = 20) -> tuple[TaskRecord, ...]:
        """最近 N 条，按时间**升序**返回。"""
        rows = self._conn.execute(
            "SELECT * FROM task_records WHERE task_id = ?"
            " ORDER BY ts DESC, id DESC LIMIT ?",
            (task_id, limit),
        )
        records = [self._row_to_record(r) for r in rows]
        return tuple(reversed(records))

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> TaskRecord:
        return TaskRecord(
            id=row["id"],
            task_id=row["task_id"],
            ts=datetime.fromisoformat(row["ts"]),
            type=RecordType(row["type"]),
            content=row["content"],
        )


# --------------------------------------------------------------------------- #
# ArtifactRepo
# --------------------------------------------------------------------------- #
class ArtifactRepo(_RepoBase):
    def add(
        self,
        task_id: str,
        title: str,
        content: str,
        now: datetime,
        *,
        artifact_id: str | None = None,
        supersedes: str | None = None,
    ) -> Artifact:
        version = self.next_version(task_id)
        artifact = Artifact(
            id=artifact_id or new_id(),
            task_id=task_id,
            version=version,
            title=title,
            content=content,
            status=ArtifactStatus.DRAFT,
            created_at=now,
            supersedes=supersedes,
        )
        with self._tx():
            self._conn.execute(
                "INSERT INTO artifacts (id, task_id, version, title, content, status,"
                " created_at, accepted_at, supersedes) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    artifact.id,
                    artifact.task_id,
                    artifact.version,
                    artifact.title,
                    artifact.content,
                    artifact.status.value,
                    artifact.created_at.isoformat(),
                    None,
                    artifact.supersedes,
                ),
            )
        return artifact

    def get(self, artifact_id: str) -> Artifact:
        row = self._conn.execute(
            "SELECT * FROM artifacts WHERE id = ?", (artifact_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("产物", artifact_id)
        return self._row_to_artifact(row)

    def find(self, artifact_id: str) -> Artifact | None:
        row = self._conn.execute(
            "SELECT * FROM artifacts WHERE id = ?", (artifact_id,)
        ).fetchone()
        return self._row_to_artifact(row) if row is not None else None

    def next_version(self, task_id: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(version), 0) FROM artifacts WHERE task_id = ?", (task_id,)
        ).fetchone()
        return int(row[0]) + 1

    def get_by_version(self, task_id: str, version: int) -> Artifact | None:
        row = self._conn.execute(
            "SELECT * FROM artifacts WHERE task_id = ? AND version = ?", (task_id, version)
        ).fetchone()
        return self._row_to_artifact(row) if row is not None else None

    def set_status(
        self, artifact_id: str, status: ArtifactStatus, now: datetime
    ) -> Artifact:
        accepted_at = now.isoformat() if status is ArtifactStatus.ACCEPTED else None
        with self._tx():
            cursor = self._conn.execute(
                "UPDATE artifacts SET status=?, accepted_at=? WHERE id=?",
                (status.value, accepted_at, artifact_id),
            )
        if cursor.rowcount == 0:
            raise NotFoundError("产物", artifact_id)
        return self.get(artifact_id)

    def current_accepted(self, task_id: str) -> Artifact | None:
        row = self._conn.execute(
            "SELECT * FROM artifacts WHERE task_id = ? AND status = ?"
            " ORDER BY version DESC LIMIT 1",
            (task_id, ArtifactStatus.ACCEPTED.value),
        ).fetchone()
        return self._row_to_artifact(row) if row is not None else None

    def latest(self, task_id: str) -> Artifact | None:
        row = self._conn.execute(
            "SELECT * FROM artifacts WHERE task_id = ? ORDER BY version DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        return self._row_to_artifact(row) if row is not None else None

    def list_for_task(self, task_id: str) -> list[Artifact]:
        rows = self._conn.execute(
            "SELECT * FROM artifacts WHERE task_id = ? ORDER BY version ASC", (task_id,)
        )
        return [self._row_to_artifact(r) for r in rows]

    @staticmethod
    def _row_to_artifact(row: sqlite3.Row) -> Artifact:
        return Artifact(
            id=row["id"],
            task_id=row["task_id"],
            version=int(row["version"]),
            title=row["title"],
            content=row["content"],
            status=ArtifactStatus(row["status"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            accepted_at=_dt_in(row["accepted_at"]),
            supersedes=row["supersedes"],
        )


# --------------------------------------------------------------------------- #
_T = TypeVar("_T")


def _replace(obj: _T, **changes: object) -> _T:
    """``dataclasses.replace`` 的薄封装，避免各处重复 import。"""
    return cast(_T, dataclasses.replace(obj, **changes))
