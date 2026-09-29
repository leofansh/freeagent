"""应用组装：把仓储、时钟、LLM、服务一次性接起来。

CLI 与测试都从这里取依赖，保证两边行为一致。
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config, load_config
from .services.artifacts import ArtifactService
from .services.chat import ChatService
from .services.clock import Clock, SystemClock
from .services.llm import LLMProvider, build_provider
from .services.llm.vision import VisionProvider
from .services.reminders import ReminderEngine
from .services.restore import RestoreService
from .services.roles import RoleService
from .services.sorting import EnergyWindows
from .services.tasks import TaskService
from .services.today import TodayService
from .storage.db import connect, migrate, resolve_db_path
from .storage.repos import ArtifactRepo, RecordRepo, RoleRepo, TaskRepo

__all__ = ["App", "build_app"]


@dataclass(slots=True)
class App:
    """组装好的应用。所有服务共享一个连接与一个时钟。

    **刻意不是 frozen**：设置页要能在运行期换掉智能层和精力档位并立即生效。
    每次请求都持有 ``lock``，所以替换是原子的（换指针 + 换 provider 一起）。
    """

    conn: sqlite3.Connection
    clock: Clock
    llm: LLMProvider
    roles: RoleService
    tasks: TaskService
    artifacts: ArtifactService
    today: TodayService
    restore: RestoreService
    reminders: ReminderEngine
    chat: ChatService
    task_repo: TaskRepo
    role_repo: RoleRepo
    record_repo: RecordRepo
    artifact_repo: ArtifactRepo
    energy_windows: EnergyWindows | None
    config: Config | None = None
    #: 视觉能力。**没配 Key 时是 None** —— 那就是「没有这个能力」，
    #: 不是「有个能力返回失败」。界面据此直接说清原因，不假装试过了。
    vision: VisionProvider | None = None
    #: 串行化所有服务访问。SQLite 连接不能被两个线程同时使用
    #: （``check_same_thread=False`` 只解除了线程归属检查，不提供并发安全）。
    #: CLI 单线程用不到它；Web UI 的每个请求都必须持有它。
    lock: threading.RLock = field(default_factory=threading.RLock)

    @property
    def llm_name(self) -> str:
        """当前智能层实现名，用于如实告知用户。"""
        return type(self.llm).__name__

    def apply_config(self, config: Config) -> None:
        """换用新配置，**立即生效**，不必重启。

        调用方必须持有 ``lock``。
        """
        self.config = config
        self.llm = build_provider(config)
        self.energy_windows = config.energy_windows
        self.today.set_energy_windows(config.energy_windows)

    def close(self) -> None:
        self.conn.close()


def build_vision_provider(config: Config | None) -> VisionProvider | None:
    """按配置建视觉能力。**没 Key 就返回 None**，不返回一个失败的实现。

    有意和 :func:`freeagent.services.llm.build_provider` 不同：文本层没 Key 时
    退回规则层（还能用），视觉层没 Key 时是真的什么都不剩。
    """
    if config is None or not config.has_credentials:
        return None
    from .services.llm.deepseek_vision import (
        DeepSeekVisionConfig,
        DeepSeekVisionProvider,
    )

    return DeepSeekVisionProvider(
        DeepSeekVisionConfig(
            model=config.vision_model,
            base_url=config.base_url,
            timeout=max(config.timeout, 30.0),   # 图比文慢，超时给得宽松些
        ),
        config.api_key.use() if config.api_key else "",
    )


def build_app(
    db_path: Path | None = None,
    *,
    clock: Clock | None = None,
    llm: LLMProvider | None = None,
    vision: VisionProvider | None = None,
    energy_windows: EnergyWindows | None = None,
    config: Config | None = None,
) -> App:
    """建库并组装服务。``db_path`` 为空时按 ``FREEAGENT_HOME`` 解析。

    ``llm`` / ``energy_windows`` / ``config`` 都不传时，**从配置推导**：
    有 API Key 就用真实模型，没配就用规则层 —— 两种情况都能跑。

    显式给了 ``db_path`` 时，配置目录取它的父目录 —— 这样
    ``--db ./data/agent.db`` 会读 ``./data/config.json``，
    而不是远处的 ``~/.freeagent/config.json``（那种行为会让人以为配置没生效）。
    """
    if db_path is not None:
        path = Path(db_path)
        # 显式路径也要保证父目录存在，否则 `--db ./data/agent.db` 会直接崩
        path.parent.mkdir(parents=True, exist_ok=True)
    else:
        path = resolve_db_path()
    conn = connect(path)
    # **必须 migrate，不能只 init_schema。** 踩过的坑：``init_schema`` 全是
    # ``CREATE TABLE IF NOT EXISTS``，所以它对**已存在**的旧表完全不起作用 ——
    # 新加的列在旧库里永远不会出现，于是用户升级后一读就崩，而且不报错、
    # 只在 ``row["project_path"]`` 取不到时才炸，排查起来极难。
    # ``migrate`` 内部会先判断版本，新库走 ``init_schema``，旧库逐版本 ``ALTER``。
    migrate(conn)

    the_clock = clock or SystemClock()
    if config is None and (llm is None or energy_windows is None):
        config = load_config(path.parent)
    the_config = config
    if energy_windows is None and the_config is not None:
        energy_windows = the_config.energy_windows
    the_llm = llm if llm is not None else build_provider(the_config)

    task_repo = TaskRepo(conn)
    role_repo = RoleRepo(conn)
    record_repo = RecordRepo(conn)
    artifact_repo = ArtifactRepo(conn)

    role_service = RoleService(conn, role_repo, task_repo, record_repo, the_clock)
    task_service = TaskService(task_repo, record_repo, the_clock)
    artifact_service = ArtifactService(
        artifact_repo, task_repo, record_repo, the_clock
    )
    today_service = TodayService(
        task_repo, record_repo, the_clock, energy_windows=energy_windows
    )
    restore_service = RestoreService(
        task_service, artifact_service, record_repo, role_service, the_clock
    )
    reminder_engine = ReminderEngine(task_repo, record_repo, the_clock)
    chat_service = ChatService(
        task_service, task_repo, role_service, restore_service,
        today_service, the_clock, the_llm,
        # ``the_config`` **可能真是 None**：上面只在「config 没给**且**
        # llm/energy_windows 至少缺一个」时才从盘上读。所以调用方同时
        # 传 llm= 和 energy_windows= 时它会留成 None。
        #
        # 这里必须判空 —— 踩过的坑：直接 ``the_config.llm_view_routing``
        # 会在那条路上抛 AttributeError，而**全部 1440 条测试都绿**
        # （没有任何一条测试同时传那两个参数）。缺省 False 与该字段的
        # 默认值一致，所以判空不会改变语义。
        view_routing=(
            the_config.llm_view_routing if the_config is not None else False
        ),
    )
    vision = (
        vision if vision is not None else build_vision_provider(the_config)
    )

    return App(
        conn=conn,
        clock=the_clock,
        llm=the_llm,
        roles=role_service,
        tasks=task_service,
        artifacts=artifact_service,
        today=today_service,
        restore=restore_service,
        reminders=reminder_engine,
        chat=chat_service,
        task_repo=task_repo,
        role_repo=role_repo,
        record_repo=record_repo,
        artifact_repo=artifact_repo,
        energy_windows=energy_windows,
        config=the_config,
        vision=vision,
    )
