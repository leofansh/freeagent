"""SQLite 连接、建表与迁移。

分层纪律（设计文档 12.3）：本模块只负责建库与连接，**不做任何业务判断**。

存储约定
--------
* ``datetime`` 存 ISO-8601 ``TEXT``，``date`` 存 ``YYYY-MM-DD`` ``TEXT``。
  一律通过 ``.isoformat()`` / ``fromisoformat()`` 转换，不引入时区换算，
  以便测试注入 ``FrozenClock`` 后可以精确比对字符串。
* 多值字段不存 JSON 数组，而是用带外键的关联表
  （``task_roles`` / ``task_dependencies``），让「删角色时被引用则拒绝」
  这类约束由数据库自己保证。
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

__all__ = [
    "SCHEMA_VERSION",
    "DEFAULT_HOME_ENV",
    "resolve_db_path",
    "connect",
    "init_schema",
    "migrate",
    "transaction",
]

#: 每次 DDL 结构变更递增。
SCHEMA_VERSION = 7

#: 按版本递增的迁移。**每一步都必须能在已有库上原地跑**：
#: ``init_schema`` 只建新表，不会给已存在的表加列，所以列变更必须显式 ALTER。
_MIGRATIONS: tuple[tuple[str, str], ...] = (
    (
        "1 → 2：tasks 加 project_path（委派用）",
        "ALTER TABLE tasks ADD COLUMN project_path TEXT",
    ),
    (
        "2 → 3：tasks 加 delegate_chat_id（委派结果回传飞书用）",
        "ALTER TABLE tasks ADD COLUMN delegate_chat_id TEXT",
    ),
    (
        "3 → 4：pending_approvals（飞书确认的等待与答复）",
        # 整表新建，所以走 CREATE TABLE IF NOT EXISTS 而不是 ALTER ——
        # 建表本身幂等，不需要 _column_exists 兜底。
        """
        CREATE TABLE IF NOT EXISTS pending_approvals (
            credential  TEXT PRIMARY KEY,
            subject     TEXT NOT NULL,
            detail      TEXT,
            asked_at    TEXT NOT NULL,
            expires_at  TEXT NOT NULL,
            decision    TEXT,
            decided_by  TEXT,
            decided_at  TEXT,
            open_message_id TEXT
        )
        """,
    ),
    (
        "4 → 5：pending_approvals 加 requested_by（只有发起人能批）",
        # 为什么加这一列：设计文档要求「审批不是谁都能点，他人批准等于授权越权」
        # （抄自 QM 的 "only the person who requested this command can
        # approve or deny it"）。原先这一条**只写在文档里、代码没实现** ——
        # 飞书白名单里任何一个人都能点掉别人发起的委派。
        #
        # 可空：历史行没有发起人，判定时按「无记录则不拦」处理
        # （见 ApprovalStore.resolve），不能因为加列让旧库读不出来。
        "ALTER TABLE pending_approvals ADD COLUMN requested_by TEXT",
    ),
    (
        "5 → 6：tasks 加 reminder_rule（重复提醒的生成器）",
        # reminder_time 仍然是「下一次触发的瞬时」，规则只当生成器。
        # 存原始 JSON 文本而不是拆成结构化列，因为规则是**整体替换**的
        # （改时间就是换一条规则），没有按字段查询的需求；而文本列让
        # 「规则坏了」这件事由 services 层一次校验拦住，不落到 SQL。
        "ALTER TABLE tasks ADD COLUMN reminder_rule TEXT",
    ),
    (
        "6 → 7：tasks 加 revision（乐观并发）",
        # 每次 update 自增。委派与提醒的状态变更要能发现
        # 「我读到的已经不是最新的了」—— 之前只能靠 updated_at，
        # 而它是时间戳：同一秒内两次写入分不出先后。
        # 用 INTEGER 而不是时间戳，是因为并发控制要的是**单调计数**，
        # 不是「什么时候改的」。
        "ALTER TABLE tasks ADD COLUMN revision INTEGER NOT NULL DEFAULT 0",
    ),
)

#: 覆盖默认数据目录的环境变量名。
DEFAULT_HOME_ENV = "FREEAGENT_HOME"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS roles (
    id                          TEXT PRIMARY KEY NOT NULL,
    name                        TEXT NOT NULL UNIQUE,
    note                        TEXT,
    default_definition_of_done  TEXT,
    active                      INTEGER NOT NULL DEFAULT 1,
    icon                        TEXT,
    color                       TEXT,
    merged_into                 TEXT REFERENCES roles(id) ON DELETE RESTRICT,
    created_at                  TEXT NOT NULL,
    updated_at                  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id                   TEXT PRIMARY KEY NOT NULL,
    title                TEXT NOT NULL,
    state                TEXT NOT NULL,
    kind                 TEXT NOT NULL,
    intent               TEXT,
    definition_of_done   TEXT,
    scheduled_for        TEXT,
    due_time             TEXT,
    reminder_time        TEXT,
    waiting_on           TEXT,
    entered_at           TEXT NOT NULL,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    completed_at         TEXT,
    dropped_at           TEXT,
    last_resumed_at      TEXT,
    progress_note        TEXT,
    project_path         TEXT,
    delegate_chat_id     TEXT,
    reminder_rule        TEXT,
    revision             INTEGER NOT NULL DEFAULT 0,
    current_artifact_id  TEXT REFERENCES artifacts(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS task_roles (
    task_id  TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    role_id  TEXT NOT NULL REFERENCES roles(id) ON DELETE RESTRICT,
    ord      INTEGER NOT NULL,
    PRIMARY KEY (task_id, role_id)
);

CREATE TABLE IF NOT EXISTS artifacts (
    id           TEXT PRIMARY KEY NOT NULL,
    task_id      TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    version      INTEGER NOT NULL,
    title        TEXT NOT NULL,
    content      TEXT NOT NULL,
    status       TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    accepted_at  TEXT,
    supersedes   TEXT REFERENCES artifacts(id) ON DELETE SET NULL,
    UNIQUE (task_id, version)
);

CREATE TABLE IF NOT EXISTS task_records (
    id       TEXT PRIMARY KEY NOT NULL,
    task_id  TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    ts       TEXT NOT NULL,
    type     TEXT NOT NULL,
    content  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_dependencies (
    task_id            TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    depends_on_task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    PRIMARY KEY (task_id, depends_on_task_id)
);

CREATE INDEX IF NOT EXISTS idx_tasks_state          ON tasks(state);
CREATE INDEX IF NOT EXISTS idx_tasks_scheduled_for  ON tasks(scheduled_for);
CREATE INDEX IF NOT EXISTS idx_tasks_kind           ON tasks(kind);
CREATE INDEX IF NOT EXISTS idx_tasks_reminder_time  ON tasks(reminder_time);
CREATE INDEX IF NOT EXISTS idx_task_roles_role      ON task_roles(role_id, ord);
CREATE INDEX IF NOT EXISTS idx_records_task_ts      ON task_records(task_id, ts);
CREATE INDEX IF NOT EXISTS idx_artifacts_task_ver   ON artifacts(task_id, version);
CREATE INDEX IF NOT EXISTS idx_deps_depends_on      ON task_dependencies(depends_on_task_id);

-- 待确认的本地操作。跨进程：桥接进程**写答复**，等待方进程**读答复**，
-- 所以必须落盘而不能放内存（进程一挂就没了，用户点了也白点）。
CREATE TABLE IF NOT EXISTS pending_approvals (
    credential      TEXT PRIMARY KEY,
    subject         TEXT NOT NULL,
    detail          TEXT,
    asked_at        TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    decision        TEXT,
    decided_by      TEXT,
    decided_at      TEXT,
    open_message_id TEXT,
    requested_by    TEXT
);
"""


def resolve_db_path(home: str | Path | None = None) -> Path:
    """解析数据库文件路径。

    优先级：显式 ``home`` 参数 > 环境变量 ``FREEAGENT_HOME`` > ``~/.freeagent``。
    返回的路径父目录会被创建，文件本身不创建。

    环境变量要 ``strip()``，理由同 :func:`freeagent.config.config_path`：
    尾随空格会让 Windows 剥掉路径末尾，导致 ``mkdir`` 建的目录和实际用的
    路径对不上，最后只抛一句 ``unable to open database file``。两处必须
    一起改 —— 漏一处就会出现「库在 A 目录、配置在 B 目录」这种更怪的状态。
    """
    if home is not None:
        base = Path(home)
    else:
        env = (os.environ.get(DEFAULT_HOME_ENV) or "").strip()
        base = Path(env) if env else Path.home() / ".freeagent"
    base.mkdir(parents=True, exist_ok=True)
    return base / "agent.db"


def connect(db_path: Path) -> sqlite3.Connection:
    """打开连接并设置 PRAGMA。调用方负责关闭。

    ``check_same_thread=False``：Web UI 用 ``ThreadingHTTPServer``，
    连接会在工作线程里被使用。SQLite 本身没有并发写问题（写会串行化），
    但**同一时刻只能有一个线程在用这个连接** —— 这个约束由
    :attr:`freeagent.app.App.lock` 负责保证（所有服务调用都在锁内）。
    """
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """幂等建表。可重复调用。"""
    conn.executescript(_SCHEMA)
    conn.commit()


def migrate(conn: sqlite3.Connection) -> None:
    """按 ``PRAGMA user_version`` 逐版本迁移。

    ``user_version`` 是 SQLite 自带的库级版本号，存在 ``PRAGMA`` 里，
    不需要额外的迁移表 —— 这对一个单文件数据库是最省事也最不容易出错的做法。

    迁移**必须幂等**：重复调用不会重复加列（``_column_exists`` 兜底），
    因为用户在旧库上跑新代码时版本号可能已经被人手工改过。
    """
    current = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if current < 1:
        init_schema(conn)
        current = 1
    # ``_MIGRATIONS`` 按**源版本**索引：``_MIGRATIONS[0]`` 是 1 → 2。
    # 所以循环变量是「迁移前版本」，落库的新版本要 +1。
    # 踩过的坑：这里原本写成 ``user_version = target``，于是版本号永远
    # 停在原地 —— 迁移 SQL 照跑（列确实加上了），但版本不推进，
    # 每次启动都重跑一遍迁移。只靠「列在不在」断言会漏掉这个错。
    for source in range(current, SCHEMA_VERSION):
        _label, sql = _MIGRATIONS[source - 1]
        _run_idempotent(conn, sql)
        conn.execute(f"PRAGMA user_version = {source + 1}")
        conn.commit()


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(
        row[1] == column
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    )


def _run_idempotent(conn: sqlite3.Connection, sql: str) -> None:
    """执行一条迁移，**已经做过就跳过**。

    ``ALTER TABLE ... ADD COLUMN`` 没有 ``IF NOT EXISTS``，重复执行会报错。
    库是用户的文件，不能假设版本号一定干净。
    """
    import re

    match = re.match(
        r"ALTER TABLE\s+(\w+)\s+ADD COLUMN\s+(\w+)", sql.strip(), re.I
    )
    if match and _column_exists(conn, match.group(1), match.group(2)):
        return
    conn.execute(sql)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """显式持有事务，用于跨仓储的原子操作。

    配合 ``repos`` 的 ``autocommit=False`` 使用：服务层在这个块内调用
    多个仓储方法，它们不会各自提交，异常时整体回滚。
    """
    if conn.in_transaction:
        # 已在事务中（例如仓储 autocommit 开了），直接借用，不重复开
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    conn.commit()
