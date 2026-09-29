"""storage 层单元测试。"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta

import pytest

from freeagent.domain import (
    ArtifactStatus,
    ConflictError,
    InvariantViolation,
    Task,
    TaskKind,
    TaskState,
    WaitingKind,
    WaitingOn,
)
from freeagent.storage.db import connect, init_schema, resolve_db_path
from freeagent.storage.repos import ArtifactRepo, RecordRepo, RoleRepo, TaskRepo
from freeagent.domain import RecordType

NOW = datetime(2026, 9, 26, 9, 0)


@pytest.fixture()
def conn(tmp_path):
    connection = connect(resolve_db_path(tmp_path))
    init_schema(connection)
    yield connection
    connection.close()


@pytest.fixture()
def repos(conn):
    return RoleRepo(conn), TaskRepo(conn), RecordRepo(conn), ArtifactRepo(conn)


def _task(**kw) -> Task:
    base = dict(
        id="t1",
        title="标题",
        role_ids=("r1",),
        state=TaskState.INBOX,
        kind=TaskKind.ACTION,
        entered_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )
    base.update(kw)
    return Task(**base)


def test_schema_is_idempotent(conn):
    init_schema(conn)
    init_schema(conn)
    tables = {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"roles", "tasks", "task_roles", "artifacts", "task_records", "task_dependencies"} <= tables


def test_primary_keys_are_not_null(conn):
    """回归：SQLite 对非 INTEGER 主键不隐式补 NOT NULL，漏写 id 会插入 NULL 行。"""
    conn.execute(
        "INSERT INTO roles (id,name,active,created_at,updated_at) VALUES ('r1','n',1,'t','t')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO tasks (title,state,kind,entered_at,created_at,updated_at)"
            " VALUES ('x','inbox','action','t','t','t')"
        )


def test_db_path_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("FREEAGENT_HOME", str(tmp_path / "custom"))
    path = resolve_db_path()
    assert path.parent == tmp_path / "custom"
    assert path.name == "agent.db"
    assert path.parent.is_dir()


def test_db_path_explicit_beats_env(tmp_path, monkeypatch):
    monkeypatch.setenv("FREEAGENT_HOME", str(tmp_path / "env"))
    path = resolve_db_path(tmp_path / "explicit")
    assert path.parent == tmp_path / "explicit"


def test_task_full_roundtrip(repos):
    _, tasks, _, _ = repos
    roles = RoleRepo(repos[0]._conn)
    r1 = roles.add("甲", NOW)
    r2 = roles.add("乙", NOW)
    dep = tasks.add(_task(id="dep", title="被依赖", role_ids=(r1.id,)))
    waiting = WaitingOn(
        kind=WaitingKind.PERSON,
        who_or_what="客户",
        since=NOW,
        follow_up_at=NOW + timedelta(days=2),
        note="邮件已发",
    )
    tasks.add(
        _task(
            id="t1",
            role_ids=(r1.id, r2.id),
            state=TaskState.ACTIVE,
            scheduled_for=date(2026, 9, 26),
            due_time=datetime(2026, 9, 27, 18, 0),
            reminder_time=datetime(2026, 9, 26, 15, 0),
            waiting_on=waiting,
            blocked_by=(dep.id,),
            last_resumed_at=NOW,
            progress_note="有框架了",
            intent="先理一版",
            definition_of_done="能给领导看",
        )
    )
    got = tasks.get("t1")
    assert got.role_ids == (r1.id, r2.id)
    assert got.waiting_on == waiting
    assert got.blocked_by == (dep.id,)
    assert got.scheduled_for == date(2026, 9, 26)
    assert got.progress_note == "有框架了"
    assert got.state is TaskState.ACTIVE


def test_task_requires_a_role(repos):
    _, tasks, _, _ = repos
    with pytest.raises(InvariantViolation):
        tasks.add(_task(role_ids=()))


def test_task_rejects_unknown_role(repos):
    _, tasks, _, _ = repos
    with pytest.raises(InvariantViolation):
        tasks.add(_task(role_ids=("nope",)))


def test_replace_roles_dedups_and_keeps_order(repos):
    conn = repos[0]._conn
    roles = RoleRepo(conn)
    r1 = roles.add("甲", NOW)
    r2 = roles.add("乙", NOW)
    _, tasks, _, _ = repos
    tasks.add(_task(role_ids=(r1.id,)))
    got = tasks.replace_roles("t1", [r2.id, r1.id, r2.id], NOW)
    assert got.role_ids == (r2.id, r1.id)


def test_replace_roles_rejects_unknown_role(repos):
    conn = repos[0]._conn
    role = RoleRepo(conn).add("甲", NOW)
    _, tasks, _, _ = repos
    tasks.add(_task(role_ids=(role.id,)))
    with pytest.raises(InvariantViolation):
        tasks.replace_roles("t1", ("nope",), NOW)


def test_records_recent_returns_ascending(repos):
    conn = repos[0]._conn
    role = RoleRepo(conn).add("甲", NOW)
    _, _, records, _ = repos
    TaskRepo(conn).add(_task(role_ids=(role.id,)))
    for i in range(5):
        records.append("t1", RecordType.NOTE, f"n{i}", NOW + timedelta(minutes=i))
    assert [r.content for r in records.recent("t1", limit=2)] == ["n3", "n4"]
    assert len(records.list_for_task("t1")) == 5


def test_artifact_versions_and_acceptance(repos):
    conn = repos[0]._conn
    role = RoleRepo(conn).add("甲", NOW)
    _, tasks, _, arts = repos
    tasks.add(_task(role_ids=(role.id,)))
    a1 = arts.add("t1", "v1", "内容1", NOW)
    a2 = arts.add("t1", "v2", "内容2", NOW, supersedes=a1.id)
    assert (a1.version, a2.version) == (1, 2)
    assert arts.next_version("t1") == 3
    arts.set_status(a1.id, ArtifactStatus.ACCEPTED, NOW)
    assert arts.current_accepted("t1").id == a1.id
    arts.set_status(a2.id, ArtifactStatus.ACCEPTED, NOW)
    arts.set_status(a1.id, ArtifactStatus.SUPERSEDED, NOW)
    assert arts.current_accepted("t1").id == a2.id
    assert arts.get(a1.id).status is ArtifactStatus.SUPERSEDED


def test_role_resolve_follows_chain(repos):
    roles, _, _, _ = repos
    a = roles.add("a", NOW)
    b = roles.add("b", NOW)
    c = roles.add("c", NOW)
    roles.set_merged_into(a.id, b.id, NOW)
    roles.set_merged_into(c.id, a.id, NOW)
    assert roles.resolve(c.id).id == b.id
    assert len(roles.list_all()) == 1
    assert len(roles.list_all(include_merged=True)) == 3


def test_role_cycle_is_rejected(repos):
    from freeagent.domain import ValidationError

    roles, _, _, _ = repos
    a = roles.add("a", NOW)
    b = roles.add("b", NOW)
    roles.set_merged_into(a.id, b.id, NOW)
    with pytest.raises(ValidationError):
        # 人为造环：b -> a -> b
        roles._conn.execute("UPDATE roles SET merged_into=? WHERE id=?", (a.id, b.id))
        roles.resolve(b.id)


def test_delete_referenced_role_conflicts(repos):
    conn = repos[0]._conn
    roles = RoleRepo(conn)
    r = roles.add("甲", NOW)
    TaskRepo(conn).add(_task(role_ids=(r.id,)))
    with pytest.raises(ConflictError):
        roles.delete(r.id)


def test_list_open_before_excludes_terminal(repos):
    conn = repos[0]._conn
    roles = RoleRepo(conn)
    r = roles.add("甲", NOW)
    tasks = TaskRepo(conn)
    tasks.add(_task(id="a", role_ids=(r.id,), scheduled_for=date(2026, 9, 20)))
    tasks.add(
        _task(
            id="b",
            role_ids=(r.id,),
            scheduled_for=date(2026, 9, 21),
            state=TaskState.DONE,
            completed_at=NOW,
        )
    )
    tasks.add(_task(id="c", role_ids=(r.id,), scheduled_for=date(2026, 9, 22)))
    got = [t.id for t in tasks.list_open_before(date(2026, 9, 26))]
    assert got == ["a", "c"]


def test_count_dependents(repos):
    conn = repos[0]._conn
    roles = RoleRepo(conn)
    r = roles.add("甲", NOW)
    tasks = TaskRepo(conn)
    tasks.add(_task(id="blocker", role_ids=(r.id,)))
    tasks.add(_task(id="w1", role_ids=(r.id,), blocked_by=("blocker",)))
    tasks.add(_task(id="w2", role_ids=(r.id,), blocked_by=("blocker",)))
    assert tasks.count_dependents("blocker") == 2
    assert tasks.count_dependents("w1") == 0


def test_transaction_helper_rolls_back(conn):
    from freeagent.storage.db import transaction

    roles = RoleRepo(conn, autocommit=False)
    with pytest.raises(RuntimeError):
        with transaction(conn):
            roles.add("x", NOW)
            raise RuntimeError("boom")
    assert conn.execute("SELECT COUNT(*) FROM roles").fetchone()[0] == 0


# =============================================================================
# 迁移：旧库升级
# =============================================================================
class TestSchemaMigration:
    """**回归**：`build_app()` 曾经只调 `init_schema`，从不调 `migrate`。

    `init_schema` 全是 `CREATE TABLE IF NOT EXISTS`，所以它对**已存在**的旧表
    完全不起作用 —— 新加的列在旧库里永远不会出现。用户升级后一读就崩，
    而且不在建库时崩，等到 ``row["project_path"]`` 取不到才炸，极难排查。

    当时 800 多个测试全绿，因为它们全都建的是**新库**。这个用例是唯一
    能抓住它的：只有它先造一个 v1 库。
    """

    def _make_v1_db(self, path):
        """用**真实** schema 还原 v1：删掉 project_path 列，版本号设 1。

        刻意不用手写的简化表 —— 上一次这么干，测出来的失败是「表没有
        body 列」这种假象，跟迁移毫无关系，结论是错的。
        """
        from freeagent.storage.db import _SCHEMA, connect

        v1 = "\n".join(
            line for line in _SCHEMA.splitlines() if "project_path" not in line
        ) + "\nPRAGMA user_version = 1;\n"
        c = connect(path)
        c.executescript(v1)
        # raw SQL 播种，**必须挂上角色** —— 否则读出来报的是
        # 「没有关联角色」这种无关错误，掩盖真正的迁移问题。
        c.execute(
            "INSERT INTO roles (id, name, active, created_at, updated_at)"
            " VALUES (?, ?, 1, ?, ?)",
            ("r-old", "旧角色", NOW.isoformat(), NOW.isoformat()),
        )
        c.execute(
            "INSERT INTO tasks (id, title, state, kind, entered_at,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("t-old", "升级前建的", "inbox", "action",
             NOW.isoformat(), NOW.isoformat(), NOW.isoformat()),
        )
        c.execute(
            "INSERT INTO task_roles (task_id, role_id, ord) VALUES (?, ?, 0)",
            ("t-old", "r-old"),
        )
        c.commit()
        c.close()
        return c

    def test_v1_db_gets_new_column_on_build_app(self, tmp_path):
        from freeagent.app import build_app
        from freeagent.storage.db import connect

        db = tmp_path / "old.db"
        self._make_v1_db(db)

        before = [r[1] for r in connect(db).execute("PRAGMA table_info(tasks)")]
        assert "project_path" not in before, "前置条件：还原出的 v1 库不该有这一列"

        app = build_app(db)
        try:
            after = [r[1] for r in connect(db).execute("PRAGMA table_info(tasks)")]
            assert "project_path" in after, "build_app 必须把旧库升到当前版本"
            version = connect(db).execute("PRAGMA user_version").fetchone()[0]
            from freeagent.storage.db import SCHEMA_VERSION
            assert version == SCHEMA_VERSION

            # 关键：不只是列在，旧数据要能**真的读出来**
            tasks = app.tasks.list_all()
            assert [t.id for t in tasks] == ["t-old"]
            assert tasks[0].project_path is None, "老事务迁移后应是普通事务"
        finally:
            app.close()

    def test_migration_is_idempotent(self, tmp_path):
        """重复 build_app 同一个库不能炸（迁移必须幂等）。"""
        from freeagent.app import build_app

        db = tmp_path / "old.db"
        self._make_v1_db(db)
        for _ in range(3):
            app = build_app(db)
            app.close()

    def _make_v2_db(self, path):
        """还原 v2：有 ``project_path``、还没有 ``delegate_chat_id``。

        加这列时（委派结果回推飞书）升到了 v3。**每一版都要有迁移测试**：
        只测「从最早那版升上来」会漏掉「从上一版升上来」这条真实路径 ——
        而绝大多数用户升级时，起点正是上一版。
        """
        from freeagent.storage.db import _SCHEMA, connect

        v2 = "\n".join(
            line for line in _SCHEMA.splitlines()
            if "delegate_chat_id" not in line
        ) + "\nPRAGMA user_version = 2;\n"
        c = connect(path)
        c.executescript(v2)
        c.execute(
            "INSERT INTO roles (id, name, active, created_at, updated_at)"
            " VALUES (?, ?, 1, ?, ?)",
            ("r2", "角色", NOW.isoformat(), NOW.isoformat()),
        )
        c.execute(
            "INSERT INTO tasks (id, title, state, kind, entered_at,"
            " created_at, updated_at, project_path)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("t2", "v2 建的委派", "inbox", "action",
             NOW.isoformat(), NOW.isoformat(), NOW.isoformat(), "D:/proj/app"),
        )
        c.execute(
            "INSERT INTO task_roles (task_id, role_id, ord) VALUES (?, ?, 0)",
            ("t2", "r2"),
        )
        c.commit()
        c.close()

    def test_v2_db_gets_delegate_chat_id_column(self, tmp_path):
        """v2 → v3：加上 ``delegate_chat_id``，且旧数据读得出来。"""
        from freeagent.app import build_app
        from freeagent.storage.db import SCHEMA_VERSION, connect

        db = tmp_path / "v2.db"
        self._make_v2_db(db)
        assert "delegate_chat_id" not in [
            r[1] for r in connect(db).execute("PRAGMA table_info(tasks)")
        ], "前置条件：还原出的 v2 库不该有这一列"

        app = build_app(db)
        try:
            assert connect(db).execute(
                "PRAGMA user_version"
            ).fetchone()[0] == SCHEMA_VERSION
            tasks = app.tasks.list_all()
            assert [t.id for t in tasks] == ["t2"]
            # 老事务迁移后：不是委派来源，回推字段为空
            assert tasks[0].project_path == "D:/proj/app", "已有列不该被清掉"
            assert tasks[0].delegate_chat_id is None
        finally:
            app.close()

    def test_v2_to_v3_migration_is_idempotent(self, tmp_path):
        from freeagent.app import build_app

        db = tmp_path / "v2b.db"
        self._make_v2_db(db)
        for _ in range(3):
            app = build_app(db)
            app.close()
