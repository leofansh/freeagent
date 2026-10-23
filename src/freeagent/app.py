"""应用组装：把仓储、时钟、LLM、服务一次性接起来。

CLI 与测试都从这里取依赖，保证两边行为一致。
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config, load_config
from .services.artifacts import ArtifactService
from .services.approval import ApprovalStore
from .services.capability import CapabilityRouter, CapabilityView
from .services.chat import ChatService
from .services.clock import Clock, SystemClock
from .services.knowledge import KnowledgeService
from .services.memory import MemoryService
from .services.nudge import NudgeService
from .services.llm import LLMProvider, build_provider
from .services.llm.vision import VisionProvider
from .services.pending import PendingService
from .services.promise import PromiseService
from .services.reminders import QuietHours, ReminderEngine
from .services.restore import RestoreService
from .services.roles import RoleService
from .services.sorting import EnergyWindows
from .services.tasks import TaskService
from .services.today import TodayService
from .services.week import WeekService
from .services.personal_model import PersonalModelService
from .storage.db import connect, migrate, resolve_db_path
from .storage.repos import (
    ArtifactRepo,
    CapturedRepo,
    ContactRepo,
    DailyDigestRepo,
    PromiseRepo,
    RecordRepo,
    RoleRepo,
    TaskRepo,
)

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
    week: WeekService
    personal_model: PersonalModelService
    #: 待处理收集器（11.18）。捕获侧（飞书）与澄清侧（/pending）共用这一个
    #: 服务层与同一写入口 —— 不许为第二个通道复制逻辑（12.1.1）。
    pending: PendingService
    #: 关系对象与对外承诺（11.19）。承诺要建兑现物，所以它拿 TaskService
    #: （唯一写入口）；关系对象只是记账层，不碰事务。
    promise: PromiseService
    #: 催办草稿（20.1.4）。**只读 + 拼文本**：不持有任何发送通道，
    #: 想代发的人找不到那条路 —— 见模块纪律 1。
    nudge: NudgeService
    restore: RestoreService
    reminders: ReminderEngine

    chat: ChatService
    #: 角色脉络知识。检索结果**自带来源说明**（关键词命中 / 近期回落 / 被截断）——
    #: 见 :class:`~freeagent.services.knowledge.RetrievalResult`。
    knowledge: KnowledgeService
    #: core 记忆（3.7）。跨角色的偏好 / 约定 / 修正，检索同样**自带来源**。
    memory: MemoryService
    task_repo: TaskRepo
    role_repo: RoleRepo
    record_repo: RecordRepo
    artifact_repo: ArtifactRepo
    daily_digests: DailyDigestRepo
    energy_windows: EnergyWindows | None
    #: 安静窗口（9.8）。``None`` = 不启用。与 ``energy_windows`` **并列而不嵌套**——
    #: 见 :class:`~freeagent.config.Config` 与设计文档 9.8 的「为什么不复用」。
    quiet_hours: QuietHours | None = None
    config: Config | None = None
    #: 视觉能力。**没配 Key 时是 None** —— 那就是「没有这个能力」，
    #: 不是「有个能力返回失败」。界面据此直接说清原因，不假装试过了。
    vision: VisionProvider | None = None
    #: Web 表面的 :class:`~freeagent.cli.app.Repl`，**懒建**（见
    #: :func:`freeagent.web.endpoints_read._web_repl`）。
    #:
    #: 为什么放在这里：``App`` 是 ``slots=True``，挂不了任意属性；而命令层
    #: 必须在请求之间**保持同一个 Repl** —— 否则角色追问的半截输入会被切断，
    #: 而 ``/mode-plan`` 攒计划、``/mode-build`` 确认本来就是多轮的。
    #:
    #: 声明成 ``Any`` 而不是 ``Repl``：``cli`` 层较重，而 ``app`` 不该为
    #: 某一层通道的会话对象付导入代价。实际类型由使用方保证。
    #:
    #: **位置在默认字段区** —— dataclass 要求带默认值的字段排在无默认值的
    #: 之后。我第一版把它放在 ``chat`` 前面，于是整个包 import 就炸
    #: （``TypeError: non-default argument 'chat' follows default argument``）。
    web_repl: Any = None
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
        self.quiet_hours = config.quiet_hours
        self.reminders.set_quiet_hours(config.quiet_hours)

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


def _capability_view(config: Config | None) -> CapabilityView:
    """从配置装配「助手自己有哪些能力」。

    刻意在这里做、而不是让 :class:`ChatService` 自己读盘：那样
    ChatService 就得知道 ``Config`` 的形状，边界也就糊了。

    ## 项目名优先用 OpenCode 注册过的显示名

    用户嘴里的项目是「FreeAgent」，磁盘上是 ``D:/PycharmProjects/freeagent``。
    回答「有哪些项目」时给他一串路径是没用的 —— 他要的是**能说出口的名字**，
    好让他下一句直接说「让 opencode 改 FreeAgent」。

    取不到显示名时**退回路径本身**，不静默变空：空列表会让
    「有哪些项目」回答成「没有授权任何项目」，那是在**撒谎**。
    """
    if config is None:
        return CapabilityView()

    projects = tuple(config.delegate_projects)
    names: tuple[str, ...] = ()
    executors = ("opencode",) if projects else ()

    # 没有项目就没有名字可查 —— 跳过查询不是省事，是别顺手踩上它的副作用：
    # ``list_projects()`` 会经 ``OpenCodeServer.discovery().start()`` 把
    # **进程级**协议适配器采纳为本机版本（``_adopt_installed_adapter``，
    # 本机装了 V2 就翻成 V2）。那对长驻进程是有意为之，但「建库装配」阶段
    # 本不该碰它 —— 测试里它会越过用例边界发作（单跑绿、全量红，2026-10-16）。
    if projects:
        # 显示名来自 OpenCode 的注册表；查不到不是错，走回退路径即可。
        try:
            from .services.opencode_projects import list_projects

            known = list_projects()
            by_norm = {_norm_path(p.worktree): p.display for p in known if p.display}
            names = tuple(
                by_norm.get(_norm_path(p), "") or p for p in projects
            )
        except Exception:
            names = ()

    return CapabilityView(
        projects=projects,
        project_names=names,
        executors=executors,
    )


def _norm_path(path: str) -> str:
    return str(path).replace("\\", "/").rstrip("/").casefold()


def build_app(
    db_path: Path | None = None,
    *,
    clock: Clock | None = None,
    llm: LLMProvider | None = None,
    vision: VisionProvider | None = None,
    energy_windows: EnergyWindows | None = None,
    quiet_hours: QuietHours | None = None,
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
    if quiet_hours is None and the_config is not None:
        quiet_hours = the_config.quiet_hours
    the_llm = llm if llm is not None else build_provider(the_config)

    task_repo = TaskRepo(conn)
    role_repo = RoleRepo(conn)
    record_repo = RecordRepo(conn)
    artifact_repo = ArtifactRepo(conn)
    captured_repo = CapturedRepo(conn)
    contact_repo = ContactRepo(conn)
    promise_repo = PromiseRepo(conn)
    daily_digest_repo = DailyDigestRepo(conn)

    role_service = RoleService(conn, role_repo, task_repo, record_repo, the_clock)
    # has_pending 只给一个「有没有未决待批项」的答案：待批行的过滤口径
    # （未决、未过期、提问要答完）全在 ApprovalStore.pending_for_web 里，
    # 这里不再写第二份。TaskService 只删事务，不直接依赖那边的 store。
    def _has_pending(task_id: str) -> bool:
        store = ApprovalStore(conn, clock=the_clock.now)
        return any(p.task_id == task_id for p in store.pending_for_web())

    task_service = TaskService(
        task_repo, record_repo, the_clock, has_pending=_has_pending
    )
    artifact_service = ArtifactService(
        artifact_repo, task_repo, record_repo, the_clock
    )
    # 待处理收集器（11.18）。澄清要建事务，所以它拿 TaskService（唯一写入口）
    # 与 RoleService（默认脉络）；不自己拼 INSERT —— 一份能力一份实现。
    pending_service = PendingService(
        captured_repo, task_service, role_service, the_clock
    )
    # 关系对象与对外承诺（11.19）。承诺要挂兑现物，所以拿 TaskService
    # （唯一写入口）；关系对象只做记账，不碰事务。
    promise_service = PromiseService(
        conn, contact_repo, promise_repo, task_service, role_service, the_clock
    )
    # 催办草稿（20.1.4）。**只读**：拿任务服务（列未结束）、承诺与关系仓储
    # （读事实），一个写仓储都不碰 —— 草稿生成不该留下痕迹。
    nudge_service = NudgeService(
        task_service, promise_repo, contact_repo, the_clock
    )
    # 今天视图**排在**承诺服务之后建：它要用承诺算「逾期未兑现」的权重
    # （11.19），所以得先有承诺服务才能给出那个查找回调。
    # 回调而不是直接依赖仓储 —— 见 TodayService.__init__ 的注释。
    today_service = TodayService(
        task_repo, record_repo, the_clock,
        energy_windows=energy_windows,
        promise_of=promise_service.promise_of,
    )
    # 周回顾复用今天视图的顺延实现（不复制）。见 services/week.py 模块文档。
    week_service = WeekService(
        task_repo, record_repo, the_clock, today=today_service
    )
    restore_service = RestoreService(
        task_service, artifact_service, record_repo, role_service, the_clock
    )
    reminder_engine = ReminderEngine(
        task_repo, record_repo, the_clock, quiet_hours=quiet_hours
    )
    # 角色知识只需要连接与角色仓储（要校验 role_id 存在），不碰任务仓储 ——
    # 知识是**角色的**，不是事务的。
    knowledge_service = KnowledgeService(conn, role_repo, the_clock)
    # 记忆（3.7）与角色知识**共用同一套检索机制**（bigram + BM25 + tail +
    # 来源自报），但作用域不同：知识按角色，记忆分 global / role:<id>。
    # 同样只需要连接与角色仓储（role:<id> 要校验角色存在）。
    memory_service = MemoryService(conn, role_repo, the_clock)
    # 连续性个人模型（11.17 / V1.59）：纯只读聚合，复用既有的 TaskRepo /
    # RecordRepo，不建表、不写库。V1.59 起再并入关系账与承诺/目标，所以
    # 也要拿 PromiseService（读 contacts / open_promises）与 MemoryService
    # （读 约定 / 目标）—— 必须在这两个服务建好之后才建它。role_repo 用于
    # 把 role_id 翻成名字（活跃度展示用）。
    personal_model_service = PersonalModelService(
        task_repo, record_repo, the_clock, role_repo=role_repo,
        promise=promise_service, memory=memory_service,
    )
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
        # 能力问答**由 LLM 路由**：把 ``the_llm`` 一并给路由器，规则
        # 不参与判断（见 services/capability.py 的模块文档）。
        capabilities=_capability_view(the_config),
        # 显式开：能力路由与 ``view_routing`` 是两次独立测量（见
        # CapabilityRouter.__init__ 的注释）。默认 False 会让能力通道
        # 永远闭嘴，而它恰恰是今天这批投诉的解药。
        capability_router=CapabilityRouter(the_llm, enabled=True),
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
        week=week_service,
        personal_model=personal_model_service,
        pending=pending_service,
        promise=promise_service,
        nudge=nudge_service,
        restore=restore_service,
        reminders=reminder_engine,
        chat=chat_service,
        knowledge=knowledge_service,
        memory=memory_service,
        task_repo=task_repo,
        role_repo=role_repo,
        record_repo=record_repo,
        artifact_repo=artifact_repo,
        daily_digests=daily_digest_repo,
        energy_windows=energy_windows,
        quiet_hours=quiet_hours,
        config=the_config,
        vision=vision,
    )
