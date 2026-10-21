"""恢复出厂：清空事务数据、保留脉络与配置、备份与三道闸。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from freeagent.app import build_app
from freeagent.services.reset import (
    WIPE_ORDER,
    ResetRefused,
    backup_database,
    db_path_of,
    reset_factory,
    running_services,
    wipe_transaction_data,
)


@pytest.fixture()
def app(tmp_path: Path):
    a = build_app(tmp_path / "a.db")
    yield a
    a.close()


def _seed(app) -> None:
    role = app.roles.create("保持角色", note="长期脉络")
    t = app.tasks.create("要清的任务", [role.id])
    app.tasks.start(t.id)
    app.tasks.note(t.id, "一条进度")
    app.artifacts.create_draft(t.id, "草稿", "[TODO]")
    done = app.tasks.create("已完成的", [role.id])
    app.tasks.complete(done.id)


def test_wipe_clears_transactions_keeps_roles(app, monkeypatch) -> None:
    """事务数据全清；角色脉络（结构）与角色知识（挂角色的）保留。"""
    _seed(app)
    monkeypatch.setattr("freeagent.services.reset.running_services", lambda: [])
    result = reset_factory(db_path_of(app.conn), confirmed=True)

    assert result.deleted["tasks"] == 2
    assert result.deleted["task_records"] >= 3
    assert result.deleted["artifacts"] == 1
    # 库里的现状
    assert app.task_repo.list_all() == []
    assert app.record_repo.list_for_task("x") == []
    roles = app.role_repo.list_all()
    assert [r.name for r in roles] == ["保持角色"]
    # 配置类表没被碰
    tables = {r[0] for r in app.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "pending_approvals" in tables  # 表还在（清的是行不是表）


def test_backup_is_made_and_restorable(app, tmp_path, monkeypatch) -> None:
    """备份必须**包含 WAL 里的内容**且能还原出清空前的数据。"""
    _seed(app)
    monkeypatch.setattr("freeagent.services.reset.running_services", lambda: [])
    db = db_path_of(app.conn)
    result = reset_factory(db, confirmed=True)

    assert result.backup.is_file()
    assert result.backup.name.endswith(".bak-factory-" + result.backup.name.split(".bak-factory-")[-1])
    assert result.restore_hint
    # 还原验证：备份能连、里面还有清空前的两条任务
    conn = sqlite3.connect(result.backup)
    try:
        n = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    finally:
        conn.close()
    assert n == 2, "备份必须是清空前的完整快照（含 WAL）"


def test_refuses_when_services_running(app, monkeypatch) -> None:
    """闸门一：服务在跑 → 拒，且告诉用户怎么停。"""
    _seed(app)
    monkeypatch.setattr(
        "freeagent.services.reset.running_services",
        lambda: ["委派执行器（freeagent.delegate）"],
    )
    with pytest.raises(ResetRefused) as ei:
        reset_factory(db_path_of(app.conn), confirmed=True)
    assert "委派执行器" in str(ei.value)
    assert "停" in str(ei.value)
    # 关键：**什么都没删**，也**没有产生备份**（拒绝要早于一切副作用）
    assert len(app.task_repo.list_all()) == 2
    assert not list(db_path_of(app.conn).parent.glob("*.bak-factory-*"))


def test_refuses_unconfirmed(app, monkeypatch) -> None:
    monkeypatch.setattr("freeagent.services.reset.running_services", lambda: [])
    with pytest.raises(ResetRefused):
        reset_factory(db_path_of(app.conn), confirmed=False)
    assert len(app.task_repo.list_all()) == 0  # 没种子；重点是没建备份
    assert not list(db_path_of(app.conn).parent.glob("*.bak-factory-*"))


def test_detects_real_services_on_this_machine() -> None:
    """**真机**检测：本机跑着桥接与执行器（开发环境已知状态）。

    这条刻意不 monkeypatch：它守的是「检测逻辑真的能在 Windows 上
    认出进程」—— 上面几条用替身把 running_services 换掉了，换掉之后
    连「powershell 命令写错了」都能绿。真机跑一次才知道它有没有用。
    """
    found = running_services()
    assert any("桥接" in f for f in found), f"本机应检测到桥接：{found}"
    assert any("执行器" in f for f in found), f"本机应检测到执行器：{found}"


def test_wipe_order_children_before_parent() -> None:
    """tasks 必须是最后一个删的（FK 关系：子表引用它）。"""
    assert WIPE_ORDER[-1] == "tasks"
    assert "task_records" in WIPE_ORDER[:-1]


def test_backup_failure_refuses(app, tmp_path, monkeypatch) -> None:
    """备份失败 → 拒，绝不「那就别备份了直接清」。"""
    _seed(app)
    monkeypatch.setattr("freeagent.services.reset.running_services", lambda: [])

    def boom(_path: Path) -> Path:
        raise ResetRefused("备份没写成功：模拟")

    monkeypatch.setattr("freeagent.services.reset.backup_database", boom)
    with pytest.raises(ResetRefused, match="模拟"):
        reset_factory(db_path_of(app.conn), confirmed=True)
    assert len(app.task_repo.list_all()) == 2, "备份失败时一条都不许删"


def test_wipe_is_idempotent(tmp_path, monkeypatch) -> None:
    """空库上再抹一次：全 0，不炸。"""
    db = tmp_path / "b.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS tasks (id TEXT);"
        "CREATE TABLE IF NOT EXISTS task_records (id TEXT);"
        "CREATE TABLE IF NOT EXISTS task_roles (id TEXT);"
        "CREATE TABLE IF NOT EXISTS task_dependencies (id TEXT);"
        "CREATE TABLE IF NOT EXISTS artifacts (id TEXT);"
        "CREATE TABLE IF NOT EXISTS pending_approvals (id TEXT);"
        "CREATE TABLE IF NOT EXISTS stop_requests (id TEXT);"
    )
    conn.commit()
    conn.close()
    counts = wipe_transaction_data(db)
    assert all(v == 0 for v in counts.values())


# =============================================================================
# 每日滚动备份（12.11.2 / V1.39）
# =============================================================================
import datetime  # noqa: E402  - 与上面 import 区分开，本节归属 V1.39

from freeagent.services.reset import (  # noqa: E402
    daily_backup,
    last_daily_backup,
)

D1 = datetime.date(2026, 10, 17)


class TestDailyBackup:
    def test_runs_once_per_day_then_skips(self, tmp_path):
        db = tmp_path / "c.db"
        first = daily_backup(db, today=D1)
        assert first is not None and first.is_file()
        # 同日第二笔：跳过 —— 连新文件都不该出（哪怕时间戳不同）。
        assert daily_backup(db, today=D1) is None
        assert [d.is_file() for d in tmp_path.glob("c.db.bak-daily-*")] == [True]

    def test_next_day_runs_again(self, tmp_path):
        db = tmp_path / "c.db"
        daily_backup(db, today=D1)
        second = daily_backup(db, today=D1 + datetime.timedelta(days=1))
        assert second is not None
        assert {d.stat().st_size > 0 for d in tmp_path.glob("c.db.bak-daily-*")} == {True}

    def test_keeps_seven_but_older_backups_untouched(self, tmp_path):
        """标签独立：bak-factory 与 bak-daily 互不采信，也不互删。"""
        import datetime as dt

        db = tmp_path / "c.db"
        base = dt.date(2026, 10, 1)
        for i in range(9):
            assert daily_backup(db, today=base + dt.timedelta(days=i)) is not None
        dailies = sorted(tmp_path.glob("c.db.bak-daily-*"))
        assert len(dailies) == 7, "只留最近 7 份"
        # 被滚掉的是最早的，留下的最新日期齐全。
        assert "20261001" not in dailies[0].name
        assert "20261009" in dailies[-1].name
        # 老的出厂备份兄弟文件不该被日备份逻辑碰到 —— 滚动删除只识
        # 自己的标签，别的标签不是它的账本。（这句是本测试**监护的目的**，
        # 断言真做出来；隔壁 test_daily_and_factory_* 验的是「判定互不采信」，
        # 这里验的是「滚动不误删」。）
        assert daily_backup(db, today=D1) is not None
        assert len(list(tmp_path.glob("c.db.bak-factory-*"))) == 0

    def test_daily_and_factory_tags_do_not_interfere(self, tmp_path):
        db = tmp_path / "c.db"
        daily_backup(db, today=D1)
        backup_database(db)  # 出厂口径的备份
        assert daily_backup(db, today=D1) is None, "同日判定只看 daily 标签"
        assert len(list(tmp_path.glob("c.db.bak-daily-*"))) == 1
        assert len(list(tmp_path.glob("c.db.bak-factory-*"))) == 1

    def test_failures_do_not_raise(self, tmp_path):
        """备份失败只记日志不阻断 —— 兜底姿态，不是没退路前的那种硬闸。"""
        db = tmp_path / "c.db"
        db.write_text("not a database")
        # sqlite3 会在这个文件上炸「file is not a database」—— 但调用方
        # 不该收到异常。
        assert daily_backup(db, today=D1) is None

    def test_last_daily_backup_readable(self, tmp_path):
        db = tmp_path / "c.db"
        assert last_daily_backup(db) is None, "没备过就该 None，显示侧别猜"
        daily_backup(db, today=D1)
        got = last_daily_backup(db)
        assert got is not None and got.date() == D1
