"""常驻 OpenCode 引擎宿主（设计文档 21.1 / V1.114）。

## 为什么必须有一个「宿主」

改造前，FreeAgent 把 opencode 当**一次性执行器**：派一件事起一个服务
（实测 6–7 秒），跑完即弃。于是三个症状同时存在：

* 前台看不到「引擎在不在」——它只在某条命令跑的那几分钟里才存在；
* 没有「会话」这个概念，一句话接不上上一句；
* 每次都要重新起步，而用户感知到的是「点一下，转半天」。

新定位要的三件事 —— 把 OpenCode 藏到后台、前台只选目录与模型、由 FreeAgent
自主决定何时交给它 —— 都指向同一个前提：**引擎是常驻对象，不是命令的副产物**。

## 三条纪律（都不是可选项）

1. **一个事实一个写入口**：起停、建会话、发指令、授权、叫停只在这里发生。
   前台 / 飞书 / 终端都调它，谁也不各自拼端点 —— 那会变成三处随版本漂。
2. **默认值决定失效方向**：默认档 ``ask``（逐次授权）。``auto_dir`` 是调用方
   **显式**要求才用，且它只放宽「写类 / shell」，越界与子代理仍是拒绝。
3. **失败要响亮**：目录不存在、引擎起不来、会话建不出来，都带原因抛出去。
   静默退回「没有引擎」会让前台把它显示成「它不想干活」——那是本仓最坏的
   失效形态：看起来在工作，其实少了一块。
"""

from __future__ import annotations

import logging
import pathlib
import threading
from dataclasses import dataclass
from typing import Any, Callable, Iterator

from .agent_router import one_line
from .clock import Clock, SystemClock
from .opencode_server import OpenCodeServer, ServerError, auto_dir_permission_config

log = logging.getLogger(__name__)

__all__ = ["EngineHost", "EngineState", "ENGINE_ASK", "ENGINE_AUTO_DIR", "STOPPED",
           "STARTING", "READY", "ERROR", "resolve_workdir"]

#: 逐次授权（默认）。写文件、跑命令都要人点一下。
ENGINE_ASK = "ask"
#: 本目录内自动（**必须由调用方显式选择**）。见
#: :func:`~freeagent.services.opencode_server.auto_dir_permission_config`。
ENGINE_AUTO_DIR = "auto_dir"

_MODES = (ENGINE_ASK, ENGINE_AUTO_DIR)


def resolve_workdir(directory: str) -> str:
    """校验并解析工作目录，返回**绝对**路径；不合规抛 :class:`ValueError`。

    这是「工作目录」唯一的判定点（21.1）：相对路径的解析基准随通道而变
    （终端在仓库根、Web 在服务进程 cwd），闸门按服务启动时的 cwd 判，
    「以为在 A、其实在 B」等于闸门判错了地方。只收绝对路径，最坏是一句
    能读懂的错误。前台端点在起线程之前调它 —— 不让一个坏目录先落库、
    再起一轮后台失败。
    """
    path = pathlib.Path(directory).expanduser()
    if not path.is_absolute():
        raise ValueError(
            f"工作目录必须是绝对路径（收到 {directory!r}）—— "
            "相对路径在不同通道下会指到不同地方，闸门就判错了目录"
        )
    try:
        resolved = str(path.resolve())
    except OSError as exc:  # pragma: no cover - 平台差异兜底
        raise ValueError(f"工作目录解析失败：{path}（{exc}）") from exc
    if not pathlib.Path(resolved).is_dir():
        raise ValueError(f"工作目录不存在或不是目录：{resolved}")
    return resolved


STOPPED = "stopped"
STARTING = "starting"
READY = "ready"
ERROR = "error"


@dataclass(frozen=True, slots=True)
class EngineState:
    """宿主此刻的样子。**纯快照**，不持有任何活对象。"""

    status: str = STOPPED
    directory: str = ""
    session_id: str = ""
    model: str = ""
    agent: str = ""
    variant: str = ""
    mode: str = ENGINE_ASK
    pid: int = 0
    error: str = ""
    started_at: str = ""


class EngineHost:
    """一个进程内**唯一**的 opencode 宿主。

    ## 复用与重启的判据

    复用条件 = **同一个目录 + 同一个授权档 + 服务还答话**。三个里缺一个就
    重建。刻意**不**把模型 / 工作模式 / 推理档算进指纹：它们在 V2 是
    **会话级**设置（prompt 时下发），换模型不该让服务重起 6 秒 —— 那会把
    「换个模型试试」变成一件有代价的事。

    授权档之所以算指纹：它是**项目级覆盖层**（一份写在项目里的配置），
    服务起来时已经压好了。同一个服务上换档等于让旧规则继续生效 ——
    而「界面显示自动、实际仍在逐次问」是最难查的一类不一致。
    """

    def __init__(
        self,
        *,
        executable: str = "opencode",
        server_factory: Callable[..., Any] | None = None,
        clock: Clock | None = None,
        startup_timeout: float = 60.0,
    ) -> None:
        self._exe = executable
        #: 测试注入点。默认造真服务器；注入时**仍会调它的 ``start()``**，
        #: 所以替身只要像 ``OpenCodeServer`` 一样有 start / create_session /
        #: prompt_async / events / abort / reply_permission / reply_question /
        #: healthy / close 就够。
        self._factory = server_factory
        self._clock = clock or SystemClock()
        self._startup_timeout = startup_timeout
        self._lock = threading.RLock()
        self._server: Any = None
        self._directory = ""
        self._mode = ""
        self._session = ""
        self._model = ""
        self._agent = ""
        self._variant = ""
        self._status = STOPPED
        self._error = ""
        self._started_at = ""

    # -- 对外 ---------------------------------------------------------- #
    def ensure(
        self,
        *,
        directory: str,
        mode: str = ENGINE_ASK,
        model: str = "",
        agent: str = "",
        variant: str = "",
    ) -> str:
        """确保引擎在选定目录上就绪，返回会话 id。

        ## 为什么目录校验在这里、且是**绝对路径**

        这是「工作目录」唯一的判定点。相对路径的解析基准会随调用方而变
        （终端在仓库根、Web 在服务进程的 cwd），于是同一次点击在不同通道
        落在不同目录 —— 而闸门（``external_directory: deny``）是按**服务启动
        时的 cwd** 判的，所以「以为在 A、其实在 B」等于闸门判错了地方。
        只收绝对路径，最坏是一句能读懂的错误。
        """
        if mode not in _MODES:
            raise ValueError(
                f"未知的授权档 {mode!r}（可选：{' / '.join(_MODES)}）"
                "—— 不猜，也不悄悄退回逐次授权"
            )
        resolved = resolve_workdir(directory)

        with self._lock:
            if (
                self._server is not None
                and self._directory == resolved
                and self._mode == mode
                and self._healthy()
            ):
                # 指纹相同且答话 → 复用。**不重建**：重建会丢会话上下文，
                # 而「接着上一句说」正是这次改造要拿回来的东西。
                self._model, self._agent, self._variant = model, agent, variant
                self._status = READY
                return self._session

            if self._server is not None:
                # 换目录 / 换档 / 服务不答话 —— 三者都要求**先收口再起**。
                # 先起再收会短暂出现两个服务写同一个项目覆盖层。
                self._halt_locked(reason="换目录、换授权档，或服务已不答话")
            self._status = STARTING
            self._error = ""
            server = None
            try:
                server = self._spawn(pathlib.Path(resolved), mode)
                session = server.create_session()
                if not session:
                    raise ServerError("引擎建了会话但没给出会话 id")
            except Exception as exc:  # noqa: BLE001 - 任何失败都要收口
                if server is not None:
                    self._close_quietly(server)
                self._status = ERROR
                self._error = _explain(exc)
                self._server = None
                self._session = ""
                log.warning("【引擎】起服务失败（%s）：%s", resolved, exc,
                            exc_info=True)
                raise ServerError(
                    f"起引擎失败：{self._error}"
                ) from exc

            self._server = server
            self._directory = resolved
            self._mode = mode
            self._session = session
            self._model, self._agent, self._variant = model, agent, variant
            self._status = READY
            self._started_at = self._clock.now().isoformat(timespec="seconds")
            log.info("【引擎】就绪：目录=%s 档=%s 会话=%s", resolved, mode, session)
            return session

    def prompt(self, text: str, *, model: str = "", agent: str = "",
               variant: str = "") -> None:
        """把一句话发进当前会话。**发完就返回**，等的是事件流。

        ``text`` 必须是**单行**。换了行会让 opencode 把这次对话当成
        「多轮强指令」而升级到更强的模型/模式 —— 那是它的内部规则，
        我们不去赌它，所以在这里把换行折成空格，并且**明确**说这是我们的
        处理而不是丢掉内容。
        """
        with self._lock:
            if self._server is None:
                raise ServerError("引擎还没就绪 —— 先 ensure 再 prompt")
            brief = one_line(text)
            self._server.prompt_async(
                self._session, brief,
                model=model, agent=agent, variant=variant,
            )

    def events(self, *, timeout: float = 600.0) -> Iterator[tuple[str, Any]]:
        """订阅事件流。**调用方负责关闭生成器**（``contextlib.closing``）。"""
        with self._lock:
            if self._server is None:
                raise ServerError("引擎还没就绪 —— 没有事件可读")
            server = self._server
        return server.events(timeout=timeout)

    def reply_permission(self, request_id: str, decision: str) -> None:
        with self._lock:
            if self._server is None:
                raise ServerError("引擎不在，没法回授权")
            self._server.reply_permission(request_id, decision)

    def reply_question(self, request_id: str, answers: list[list[str]]) -> None:
        with self._lock:
            if self._server is None:
                raise ServerError("引擎不在，没法回提问")
            self._server.reply_question(request_id, answers)

    def abort(self) -> None:
        """尽力叫停。失败只记不抛 —— 叫停不是必须成功的事。"""
        with self._lock:
            if self._server is None or not self._session:
                return
            try:
                self._server.abort(self._session)
            except Exception as exc:  # noqa: BLE001
                log.warning("【引擎】中止失败（仍按中止处理）：%s", exc)

    def halt(self) -> None:
        """停掉服务并**还原项目覆盖层**。可重复调用。"""
        with self._lock:
            self._halt_locked(reason="主动停止")

    def close(self) -> None:
        self.halt()

    # -- 状态 ---------------------------------------------------------- #
    def state(self) -> EngineState:
        with self._lock:
            return EngineState(
                status=self._status,
                directory=self._directory,
                session_id=self._session,
                model=self._model,
                agent=self._agent,
                variant=self._variant,
                mode=self._mode,
                pid=self._pid(),
                error=self._error,
                started_at=self._started_at,
            )

    # -- 内部 ---------------------------------------------------------- #
    def _spawn(self, project: pathlib.Path, mode: str) -> Any:
        """造一个**带闸门**的服务实例并启动它。

        ``isolate=False`` 是刻意的：常驻引擎要看得见用户真实的插件 / MCP /
        模型凭据。闸门由**项目级覆盖层**压进去（``apply_gate=True``），
        而不是靠隔离世界 —— 后者会每起一次重装一遍插件，而常驻宿主会把
        这个代价乘以用户点击的次数。
        """
        kwargs: dict[str, Any] = dict(
            project=project,
            executable=self._exe,
            isolate=False,
            apply_gate=True,
            startup_timeout=self._startup_timeout,
            # 自动档传**惰性 getter**：权限配置的键名随主版本而变，而版本要
            # 等服务器自己探测出来（见 opencode_server.__init__ 那段）。
            permission_config=(auto_dir_permission_config
                               if mode == ENGINE_AUTO_DIR else None),
        )
        server = self._factory(**kwargs) if self._factory else OpenCodeServer(**kwargs)
        server.start()
        return server

    def _healthy(self) -> bool:
        server = self._server
        if server is None:
            return False
        try:
            return bool(server.healthy())
        except Exception:  # noqa: BLE001 - 判活失败就是「不健康」
            return False

    def _pid(self) -> int:
        proc_owner = getattr(self._server, "pid", 0)
        try:
            return int(proc_owner)
        except (TypeError, ValueError):  # pragma: no cover - 替身没有 pid
            return 0

    def _halt_locked(self, *, reason: str) -> None:
        server, self._server = self._server, None
        self._session = ""
        self._directory = ""
        self._mode = ""
        self._status = STOPPED
        self._started_at = ""
        if server is not None:
            self._close_quietly(server)
            log.info("【引擎】已停（%s）", reason)

    def _close_quietly(self, server: Any) -> None:
        """收口。**失败不许改结论** —— 我们要报的是调用方那一侧的事实。"""
        try:
            server.close()
        except Exception as exc:  # noqa: BLE001
            log.warning("【引擎】收口失败（进程可能残留）：%s", exc)


def _explain(exc: Exception) -> str:
    """把异常翻成一句人能照着做的话。"""
    detail = str(exc).strip()
    if not detail:
        detail = exc.__class__.__name__
    hint = ""
    if "No such file" in detail or "不是内部或外部命令" in detail:
        hint = "（找不到 opencode 可执行文件，先确认 `opencode --version` 有输出）"
    elif "timeout" in detail.lower() or "timed out" in detail.lower():
        hint = "（引擎启动超时，通常是首次装插件；稍后重试）"
    return detail + hint
