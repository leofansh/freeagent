"""角色服务：增删改、静置、**合并重定向**。

合并是原则 1 的核心能力，实现要点（设计文档 3.5）：
读时 ``resolve`` 沿 ``merged_into`` 追到根；写时在一个事务里把
所有引用重指向目标角色。不留下孤儿引用，也不制造多级歧义链。
"""

from __future__ import annotations

import dataclasses
import sqlite3
from dataclasses import dataclass
from typing import TypeVar, cast

from ..domain import (
    ConflictError,
    NotFoundError,
    RecordType,
    Role,
    Task,
    ValidationError,
)
from ..storage.db import transaction
from ..storage.repos import RecordRepo, RoleRepo, TaskRepo
from .clock import Clock

__all__ = ["RoleService", "MergeReport"]

_T = TypeVar("_T")


def _replace(obj: _T, **changes: object) -> _T:
    return cast(_T, dataclasses.replace(obj, **changes))


@dataclass(frozen=True, slots=True)
class MergeReport:
    """一次合并的结果。"""

    source_name: str
    target_name: str
    moved_tasks: tuple[str, ...] = ()
    absorbed: bool = False
    """源角色本就指向目标（无需重定向），仅做了标记。"""


class RoleService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        roles: RoleRepo,
        tasks: TaskRepo,
        records: RecordRepo,
        clock: Clock,
    ) -> None:
        self._conn = conn
        # 合并要跨仓储原子操作，因此这几个仓储不自行提交
        self._roles = RoleRepo(conn, autocommit=False)
        self._tasks = TaskRepo(conn, autocommit=False)
        self._records = RecordRepo(conn, autocommit=False)
        self._clock = clock

    # -- 增删改 ------------------------------------------------------------- #
    def create(
        self,
        name: str,
        *,
        note: str | None = None,
        default_definition_of_done: str | None = None,
        icon: str | None = None,
        color: str | None = None,
    ) -> Role:
        return self._roles.add(
            name,
            self._clock.now(),
            note=note,
            default_definition_of_done=default_definition_of_done,
            icon=icon,
            color=color,
        )

    def rename(self, role_id: str, name: str) -> Role:
        return self._roles.update(_replace(self._roles.get(role_id), name=name.strip()), self._clock.now())

    def set_note(self, role_id: str, note: str | None) -> Role:
        """脉络级沉淀：这个角色下我做过什么、我的口径是什么。"""
        return self._roles.update(
            _replace(self._roles.get(role_id), note=note), self._clock.now()
        )

    def set_default_definition_of_done(self, role_id: str, dod: str | None) -> Role:
        return self._roles.update(
            _replace(self._roles.get(role_id), default_definition_of_done=dod),
            self._clock.now(),
        )

    def set_active(self, role_id: str, active: bool) -> Role:
        return self._roles.update(
            _replace(self._roles.get(role_id), active=active), self._clock.now()
        )

    def delete(self, role_id: str) -> None:
        role = self._roles.get(role_id)
        if role.merged_into is not None:
            raise ConflictError(f"「{role.name}」已被合并进其他角色，请勿直接删除")
        if self._roles.count_referencing_tasks(role_id):
            raise ConflictError(
                f"「{role.name}」下还有事务，请先合并到别的角色或改归属"
            )
        self._roles.delete(role_id)

    # -- 查询 --------------------------------------------------------------- #
    def get(self, role_id: str) -> Role:
        """原始读取（不做合并解析）。"""
        return self._roles.get(role_id)

    def resolve(self, role_id: str) -> Role:
        """沿 ``merged_into`` 追到根角色。"""
        return self._roles.resolve(role_id)

    def get_by_name(self, name: str, *, include_merged: bool = False) -> Role:
        """按名字取角色。**默认不返回已合并角色** —— 它们已不在用户的列表里。

        要拿原始行（例如检查合并关系）用 ``include_merged=True``。
        """
        role = self._roles.find_by_name(name)
        if role is None:
            raise NotFoundError("角色", name)
        if role.merged_into is not None and not include_merged:
            raise NotFoundError("角色", name)
        return role

    def find_by_name(self, name: str, *, include_merged: bool = False) -> Role | None:
        role = self._roles.find_by_name(name)
        if role is None:
            return None
        if role.merged_into is not None and not include_merged:
            return None
        return role

    def list_roles(
        self, *, include_inactive: bool = True, include_merged: bool = False
    ) -> list[Role]:
        return self._roles.list_all(
            include_merged=include_merged, include_inactive=include_inactive
        )

    def tasks_in(self, role_id: str) -> list[Task]:
        """该角色（含被合并进来的）下的事务，按 id 去重。"""
        root = self.resolve(role_id)
        seen: dict[str, Task] = {}
        for r in self._roles.list_all(include_merged=True, include_inactive=True):
            if self._roles.resolve(r.id).id != root.id:
                continue
            for task in self._tasks.list_by_role(r.id):
                seen[task.id] = task
        return sorted(seen.values(), key=lambda t: (t.entered_at, t.id))

    def default_definition_of_done_for(self, role_id: str) -> str | None:
        return self.resolve(role_id).default_definition_of_done

    # -- 合并（原子重定向） -------------------------------------------------- #
    def merge(self, source_id: str, target_id: str) -> MergeReport:
        """把 ``source`` 合并进 ``target``。

        全过程在**单个事务**内完成：先重指向全部事务，最后才写
        ``merged_into``。中途失败整体回滚，不会出现「角色已标记合并
        但事务还指向旧角色」的半合并状态。
        """
        with transaction(self._conn):
            source_raw = self._roles.get(source_id)
            target_raw = self._roles.get(target_id)
            if source_raw.id == target_raw.id:
                raise ValidationError("不能把角色合并到它自己")

            # 两端都解析到根，避免制造多级链
            source = self._roles.resolve(source_raw.id)
            target = self._roles.resolve(target_raw.id)
            if source.id == target.id:
                return MergeReport(
                    source_name=source.name, target_name=target.name, absorbed=True
                )
            # 成环不需要在这里防：两端都已是根角色，target.merged_into 必为 None。
            # 真正的防线是 RoleRepo.resolve 对脏数据链的环检测。

            now = self._clock.now()
            affected = self._tasks.list_by_role(source.id)
            moved: list[str] = []
            for task in affected:
                new_roles = [target.id if r == source.id else r for r in task.role_ids]
                deduped: list[str] = []
                for r in new_roles:
                    if r not in deduped:
                        deduped.append(r)
                self._tasks.replace_roles(task.id, deduped, now)
                self._records.append(
                    task.id,
                    RecordType.ROLE_CHANGE,
                    f"角色合并：{source.name} → {target.name}",
                    now,
                )
                moved.append(task.id)

            self._roles.set_merged_into(source.id, target.id, now)

        return MergeReport(
            source_name=source.name, target_name=target.name, moved_tasks=tuple(moved)
        )

    # -- 内部：给其他服务用的只读仓储 ---------------------------------------- #
    @property
    def role_repo(self) -> RoleRepo:
        return self._roles
