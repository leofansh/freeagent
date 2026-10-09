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
#: 13 = 配对码表（设计文档 11.9.8）。
#:
#: ⚠️ 加了 ``_MIGRATIONS`` 条目**必须**同时改这个数，否则那条迁移永远跑不到
#: ——而症状是「表不存在」，不是「版本号不对」：
#: 迁移循环是 ``range(current, SCHEMA_VERSION)``，版本停在 12 时
#: 「12 → 13」那一格根本不在循环里。
SCHEMA_VERSION = 14

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
    (
        "7 → 8：占位 —— v8 的真实步骤是多语句（虚拟表 + 触发器），走 _DOMAIN_STEPS。",
        # 为什么要占位：_MIGRATIONS 按「源版本 − 1」**位置索引**，所以这条
        # 必须在**索引 6** 上。不占位的话，下面两条会整体前移一格 ——
        # 「8→9」会被当成「7→8」执行，而版本号照样往上涨，
        # 于是**迁移记录与版本号对不上**。看起来跑通了，其实做过一遍。
        #
        # 而不能简单留空：_run_idempotent 会 conn.execute(sql)，空语句直接炸。
        "SELECT 1",
    ),
    (
        "8 → 9：pending_approvals 加 kind（授权 / 提问，桥接要分清）",
        # 为什么必须有这一列，不能靠 credential 前缀推：执行器的循环要按它分
        # 两条 wait 臂（等 allow/deny 与等文本）。前缀是一种**隐式契约** ——
        # 哪天 new_credential 换了前缀，两处一起悄悄坏掉。
        "ALTER TABLE pending_approvals ADD COLUMN kind TEXT",
    ),
    (
        "9 → 10：pending_approvals 加 answer_text（提问的答复是文本）",
        # 可空：历史行没有。判据是「is_question 且 answer_text 非空 = 已答」，
        # 空字符串**不算已答** —— 「用户打了一行空白」不是答案。
        "ALTER TABLE pending_approvals ADD COLUMN answer_text TEXT",
    ),
    (
        "10 → 11：pending_approvals 加 question_spec（一次问一个、逐轮积累）",
        # 一列装下 {questions, answers}：问题数 = len(questions)（不另存），
        # 发给 opencode 的载荷 = answers（形状天然是 string[][]）。
        #
        # 原来只有一个 answer_text 文本，N>1 时**只回答了第一个问题** ——
        # 而 agent 是会一次问两问的。详见 ApprovalStore.request_question。
        "ALTER TABLE pending_approvals ADD COLUMN question_spec TEXT",
    ),
    (
        "11 → 12：tasks 加 delegate_requested_by（「只有发起人能批」的输入）",
        # 之前 tasks 只有 delegate_chat_id（**会话**），没有「**人**」——
        # 于是闸门拿不到发起人，「只有发起人能批」
        # 实际退化成「白名单里排序第一的人能批」
        # （见设计文档 11.8.1「取值错在哪」）。
        #
        # 可空：终端发起的没有飞书身份，那是正常情况——
        # 不该为了非空而填一个「随便某个人」。
        "ALTER TABLE tasks ADD COLUMN delegate_requested_by TEXT",
    ),
    (
        "12 → 13：pairing_codes（配对流程，设计文档 11.9.8）",
        # 治「第一次装时手里没有 open_id」的死循环：陌生私聊里 bot 回一条
        # 带配对码的卡，用户把码粘回终端，码过期即失效。
        #
        # ## 为什么**单独一张表**，而不是复用 pending_approvals
        #
        # 那张表的语义是「**有人要批准某件事**」：有 subject / decision /
        # decided_by / kind，且 resolve() 里写死了「首次决定不可篡改」。
        # 配对码不是批准 —— 它是「**证明我认识这个人**」，没有「谁批准谁」
        # 这一层，硬塞进去会让「决策不可篡改」那条纪律跑到不相干的地方。
        #
        # ## 为什么存**哈希**而不是明文
        #
        # 配对码是「谁能指挥本机」的一次性门票。明文落库 = 多一份可被
        # 读走的凭据；而这里没有任何查询需求（校验就是「拿码算哈希比对」）。
        # 代价是「码丢了只能重新生成」，而那本来就是短 TTL 的一次性码。
        #
        # 整表新建所以走 CREATE TABLE IF NOT EXISTS，本身幂等。
        """
        CREATE TABLE IF NOT EXISTS pairing_codes (
            code_hash   TEXT PRIMARY KEY,
            open_id     TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            expires_at  TEXT NOT NULL,
            used_at     TEXT
        )
        """,
    ),
    (
        "13 → 14：stop_requests（第六道闸门「停止」—— 补上漏掉的迁移）",
        # ``663c3f0`` 把这张表加进了 ``_SCHEMA``，但**既没升版本号也没加迁移
        # 条目** —— ``init_schema`` 只在 ``user_version < 1`` 的新库上跑，
        # 于是所有既有库永远缺这张表。症状不是「版本不对」而是
        # ``no such table: stop_requests``，且只在执行器真的走到
        # 「等人点停止」那一步才炸（本文件头部注释预言过的那种病）。
        #
        # 整表新建所以走 CREATE TABLE IF NOT EXISTS，本身幂等；
        # 新库走 init_schema 时已有同款建表，重复执行无害。
        """
        CREATE TABLE IF NOT EXISTS stop_requests (
            credential  TEXT PRIMARY KEY,
            requested_by TEXT,
            requested_at TEXT NOT NULL
        )
        """,
    ),
)


#: 角色知识域（v8）。**与 ``_SCHEMA`` 分开**，因为它含虚拟表与触发器 ——
#: ``_MIGRATIONS`` 一步只跑一条语句（``conn.execute``），而这里是多条。
#:
#: ## 为什么需要自己分词（实测，别凭直觉改）
#:
#: SQLite 的 FTS5 内置分词器对中文**不可用**，实测：
#:
#: * ``unicode61``：把整串中文当**一个 token**。``MATCH '周报'`` → **0 命中**。
#: * ``trigram``：``销售周报``（3 字）能命中，但 ``周报`` / ``王工``（2 字）
#:   **全部 0 命中** —— 而中文查询大量是 2 字词。
#:
#: 所以写入前把 CJK 切成**重叠二元组**（``交付销售周报`` →
#: ``交付 付销 销售 售周 周报``），查询用同样形态。实测 2 字词全部命中。
#:
#: ## 为什么 ``search_text`` 是一列而不是触发器里算
#:
#: SQLite **没有 bigram 函数**，触发器里做不了这个变换。所以变换由 Python
#: 算好写进 ``search_text``，触发器只负责把它**复制**进 FTS5 ——
#: 于是索引不会与真源漂移（删除/更新自动同步），而变换仍然只有一处实现。
#:
#: ``content='role_knowledge'`` 是**外部内容表**：FTS5 不复制正文，只存索引。
#: 原文永远在 ``content`` 列 —— FTS5 里只有 bigram，取不回原文。
#:
#: 刻意用 ``executescript``（可多条、``IF NOT EXISTS`` 幂等）而不是
#: ``_MIGRATIONS`` 的一步一条：虚拟表 + 三个触发器没法塞进一条语句。
_KNOWLEDGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS role_knowledge (
    id          TEXT PRIMARY KEY NOT NULL,
    role_id     TEXT NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    content     TEXT NOT NULL,          -- 原文，唯一真源
    search_text TEXT NOT NULL,          -- bigram 形态，由 Python 算好写入
    source_ref  TEXT,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rk_role ON role_knowledge(role_id, created_at);

-- 注意 FTS5 的语法：索引列名写在**前面**，content=/content_rowid= 这些
-- 选项写在**后面**。写成 `fts5(content='role_knowledge')` 会被当成
-- 「有一个叫 content 的列，值是 role_knowledge」，于是外部内容表根本没生效，
-- 建表直接报 vtable constructor failed。
CREATE VIRTUAL TABLE IF NOT EXISTS role_knowledge_fts
    USING fts5(search_text, content='role_knowledge', content_rowid='rowid');

CREATE TRIGGER IF NOT EXISTS role_knowledge_ai AFTER INSERT ON role_knowledge BEGIN
    INSERT INTO role_knowledge_fts(rowid, search_text)
        VALUES (new.rowid, new.search_text);
END;

-- delete / update 用 **BEFORE** 而不是 AFTER：外部内容表的 FTS5 **不存正文**，
-- 「删掉这一行」这条指令要回内容表读该行的值才知道该移除哪些 token。
-- 官方示例给的就是 BEFORE。
--
-- 验证同步时**不要用 JOIN 查**：内容行删掉之后 JOIN 必然返回空，
-- 那个结果与索引是否同步无关。踩过的坑就是这个 —— 第一版验证写成
-- `... FROM role_knowledge_fts f JOIN role_knowledge k ON k.rowid=f.rowid
-- WHERE role_knowledge_fts MATCH ?`，于是「触发器坏了」和「触发器好的」
-- 跑出来一模一样。正确姿势是**直接查索引**：
--   SELECT rowid FROM role_knowledge_fts WHERE role_knowledge_fts MATCH ?
-- 再补一句 `INSERT INTO role_knowledge_fts(role_knowledge_fts)
-- VALUES('integrity-check')`。
CREATE TRIGGER IF NOT EXISTS role_knowledge_ad BEFORE DELETE ON role_knowledge BEGIN
    INSERT INTO role_knowledge_fts(role_knowledge_fts, rowid, search_text)
        VALUES ('delete', old.rowid, old.search_text);
END;

CREATE TRIGGER IF NOT EXISTS role_knowledge_au BEFORE UPDATE ON role_knowledge BEGIN
    INSERT INTO role_knowledge_fts(role_knowledge_fts, rowid, search_text)
        VALUES ('delete', old.rowid, old.search_text);
    INSERT INTO role_knowledge_fts(rowid, search_text)
        VALUES (new.rowid, new.search_text);
END;
"""

#: **多语句**的域变更，键是**目标**版本。
#:
#: 为什么不并进 :data:`_MIGRATIONS`：那个元组一步只跑一条 ``conn.execute``，
#: 而「建虚拟表 + 三个触发器」塞不进一条语句。硬塞的后果是要么拆成四步
#: （中间态是「表建了触发器没建」，一次崩溃就留下不同步的索引），
#: 要么在那个元组里混入多语句（于是「一步一条」这个不变量没了，
#: 而 :func:`_run_idempotent` 的幂等兜底只认单条 ALTER）。
#:
#: 两种步骤共用 :func:`migrate` 的同一个循环与同一个版本号推进 ——
#: 版本号仍然**一步一版**，不会因为加了域步骤就跳版。
_DOMAIN_STEPS: tuple[tuple[int, str], ...] = (
    (8, _KNOWLEDGE_SCHEMA),
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
    delegate_requested_by TEXT,   -- 「谁发起的」；可空（终端发起没有飞书身份）
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

-- 配对码（设计文档 11.9.8）。治「第一次装时手里没有 open_id」的死循环：
-- 陌生**私聊**里 bot 回一张带配对码的卡，用户把码粘回终端 ``/pair <码>``。
--
-- 跨进程：桥接**签发**、终端**核销**，所以必须落盘（进程一挂就没了）。
--
-- ⚠️ 为什么**只存哈希**：配对码是「谁能指挥本机」的一次性门票，而这张表
-- 没有任何查询需求（校验就是「拿码算哈希比对」）。明文落库等于**多一份
-- 可被读走的凭据**，而它的代价只是「码丢了重新生成」—— 那本来就是
-- 短 TTL 的一次性码。
--
-- ⚠️ 为什么**不**复用 pending_approvals：那张表的语义是「**有人要批准
-- 某件事**」，有 subject / decision / decided_by，且 resolve() 里写死了
-- 「首次决定不可篡改」。配对码不是批准 —— 它是「证明我认识这个人」，
-- 没有「谁批准谁」这一层，硬塞进去会让那条纪律跑到不相干的地方。
CREATE TABLE IF NOT EXISTS pairing_codes (
    code_hash   TEXT PRIMARY KEY,
    open_id     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    used_at     TEXT
);

-- Plan 模式（12.7.2）的会话状态。**刻意独立成表，不进 tasks**：
-- Plan 期是「还没决定要不要成为事务」的对话状态，混进 tasks 会让
-- `/today` 把没批准的草稿也算成承诺 —— 那正是零副作用要杜绝的。
--
-- 存在的理由是**不能只活在内存**：`ChannelService._repls` 是 LRU，
-- `popitem(last=False)` 会连 Repl 一起丢，_plan 与 _mode 一并消失。
-- 症状不是报错，而是用户以为还在规划、其实内容已经没了（实测过）。
-- 「停掉这整条委派」的请求（设计文档 12.7.2 的可中断要求）。
--
-- 为什么单独一张表、而不是给 pending_approvals 加一列：
-- **语义不同**。那张表的 `decision` 是「这一次动作批不批」，
-- 而这里是「这条委派别再往下走了」。用户点「拒绝」只否掉一个动作，
-- agent 还能换个方向继续；点「停止」则是中止整个会话。
-- 混在一列里就再也分不清「他不想干这一步」和「他不想干这件事」了。
--
-- 作用域是**凭据**（即那一次挂起的授权），因为这是用户唯一能准确指向的
-- 东西 —— 他手上只有那张卡。
CREATE TABLE IF NOT EXISTS stop_requests (
    credential  TEXT PRIMARY KEY,
    requested_by TEXT,
    requested_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plan_sessions (
    chat_id    TEXT PRIMARY KEY,
    mode       TEXT NOT NULL,
    lines      TEXT NOT NULL,
    updated_at TEXT NOT NULL
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
    conn.executescript(_KNOWLEDGE_SCHEMA)
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
        # 单语句步骤（ALTER TABLE ADD COLUMN 之类）。``_MIGRATIONS`` 按**源版本**
        # 索引，所以 ``source - 1``。
        #
        # 判 ``source - 1 < len(...)`` 而不是只判上界：v8 那一步是**多语句**
        # 的（见 :data:`_DOMAIN_STEPS`），刻意不进这个元组。写死上界的话
        # 以后再加一个多语句版本就会 IndexError，而那个错发生得很晚 ——
        # 只在用户从旧库升上来时才炸。
        if source - 1 < len(_MIGRATIONS):
            _label, sql = _MIGRATIONS[source - 1]
            _run_idempotent(conn, sql)
        # 多语句步骤（虚拟表 + 触发器）。``executescript`` 可跑多条且幂等。
        for target, script in _DOMAIN_STEPS:
            if target == source + 1:
                conn.executescript(script)
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
