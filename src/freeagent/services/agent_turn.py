"""一轮「交给引擎动手」的执行（设计文档 21.3 / V1.114）。

## 它不是一个新的 agent 循环

模型与工具全在 opencode 那边。这里只做四件它不做的事：

1. **起转**：按前台选定的目录 / 模型 / 授权档把一句话发进去；
2. **读事件**：把流式正文、工具痕迹投影成前台能显示的形状；
3. **把授权请求摆到人面前**（默认档），并把人的结论送回去；
4. **收口**：用户点停、工具调用超限、总时长超时，三条都必须真的停下来，
   而不是「界面显示已停止、引擎还在跑」。

## 为什么状态是五态而不是一个布尔

``running`` / ``waiting`` / ``done`` / ``failed`` / ``stopped`` 必须分开：
「在等授权」与「在思考」对用户是完全不同的两件事，而它们都没有输出。
把它们合并成「running」的后果，就是 12.7.2 那条纪律说的——「卡住」和
「正在工作」长得一模一样，人只能干等。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from ..domain import new_id
from .clock import Clock, SystemClock
from .engine_host import ENGINE_ASK, ENGINE_AUTO_DIR, one_line
from .opencode_server import current_adapter
from .permission_reply import deliver as deliver_reply
from .tool_evidence import tool_evidence

log = logging.getLogger(__name__)

__all__ = [
    "AgentTurnService", "TurnView", "AskCard", "StepView",
    "RUNNING", "WAITING", "DONE", "FAILED", "STOPPED",
    "STEP_RUNNING", "STEP_DONE", "STEP_FAILED",
]

RUNNING = "running"
WAITING = "waiting"
DONE = "done"
FAILED = "failed"
STOPPED = "stopped"

#: 一轮最多允许几次工具调用。与 :mod:`freeagent.delegate` 的同名上限**不是
#: 同一个数**：那边是无人值守的长活，这边是人在旁边盯着的一句话任务。
#: 40 次已经足够「读几个文件 + 改一处 + 跑一次测试」。
DEFAULT_MAX_TOOL_CALLS = 40
#: 一轮最多跑多久（秒）。超了会**真的**叫停 —— 只报「超时」而不停的，
#: 是把一个还在改文件的进程留在后台。
DEFAULT_MAX_SECONDS = 600.0
#: 挂起授权兜底拉取的间隔（秒）。主路径是实时事件流，轮询只兜「事件漏了」
#: 这一种情况；间隔不用短 —— 真漏了，晚两三秒摆出来也比卡住没人管强。
_RECOVER_INTERVAL = 3.0

#: 步骤的三种状态。与一轮的五态**刻意不是同一套**：一轮是「这件事成没成」，
#: 一步是「这个动作走完没有」。合并会让「读文件失败了但这一轮还活着」这种事
#: 无处安放（它很常见：grep 找不到东西不是失败）。
STEP_RUNNING = "running"
STEP_DONE = "done"
STEP_FAILED = "failed"

#: 会**改内容**的工具（界面据此单列「碰过的文件」，V1.116）。
#:
#: 名字按小写比较：opencode 的 V1/V2 实测都是小写，但没有契约保证 ——
#: 大写认不出只是漏列一行，认错却会让人以为它没动那个文件。
EDIT_TOOLS = frozenset({"edit", "write", "multiedit", "patch", "apply_patch"})
#: 会**执行命令**的工具（界面据此单列「它跑过的命令」，V1.116）。
#:
#: 这一列存在的理由是「自动档下没有卡」：命令不会弹框、也不会进
#: ``pending_approvals``，如果界面上再不留痕，**「它刚才跑了什么」就只有
#: 引擎自己知道**。所以命令必须与文件一样，逐条摆到人眼前。
SHELL_TOOLS = frozenset({"bash", "shell", "pwsh", "powershell", "cmd"})


@dataclass(frozen=True, slots=True)
class StepView:
    """一条工具步骤 —— 计划与进度的最小可见单位（V1.115）。

    ``target`` 是**原文**（路径 / 命令 / 模式），与 ``tool_evidence`` 同一条
    纪律：不判断它是哪种，原样摆出来。认不出来就空着，界面上只显示工具名 ——
    猜一个目标比空着更坏（人会以为它动的是那个文件）。
    """

    name: str
    target: str = ""
    status: str = STEP_DONE

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "target": self.target, "status": self.status}


@dataclass(frozen=True, slots=True)
class AskCard:
    """一条等着人回答的东西。``kind`` 只有 ``permission`` / ``question``。"""

    request_id: str
    kind: str
    label: str
    detail: str = ""
    #: 原始动作名（如 ``edit``）。「本会话内允许此类」用它当免问的 key
    #: （V1.138）—— 不能用 ``label``，那是给显示的兜底文案（「请求授权」），
    #: 拿它当 key 会把所有认不出动作名的请求归成一类，扩大授权就失控了。
    #: 空串 = 认不出动作名，此时**不记入**免问集合（fail-closed 到每次都问）。
    permission: str = ""


@dataclass(frozen=True, slots=True)
class TurnView:
    """一轮的**纯快照**。前台只读它，不持有任何活对象。"""

    turn_id: str
    status: str
    text: str = ""
    #: 我发出去的那一句（折成单行）。**正文里不混它**，但历史列表要靠它告诉
    #: 人「这轮说的是哪件事」—— 只说状态码的列表等于一排没有标题的行。
    brief: str = ""
    tools: tuple[str, ...] = ()
    #: 逐步清单（V1.115）。``tools`` 保留给老界面与日志，新界面读 ``steps``：
    #: 前者只说「碰过哪些工具」，后者说「第几步做了什么、到没到」。
    steps: tuple[StepView, ...] = ()
    #: 这一轮带了什么上下文（21.5 / V1.115）。空串 = 什么都没带。
    #: **它是说明不是原文**：原文在发给引擎的那一行里，界面要的是「带了什么」。
    context_note: str = ""
    tool_calls: int = 0
    reason: str = ""
    notes: tuple[str, ...] = ()
    ask: AskCard | None = None
    directory: str = ""
    model: str = ""
    agent: str = ""
    variant: str = ""
    mode: str = ENGINE_ASK
    started_at: str = ""
    elapsed: float = 0.0
    ok: bool = False

    # -- 派生物（V1.116）--------------------------------------------------- #
    #: 「它碰过哪些文件」/「它跑了哪些命令」。
    #:
    #: **从 ``steps`` 现算、不落库、不加列**：步骤清单已经是这一轮全部工具
    #: 事件的投影，再存一份就成了第二个真源（两份必然有一份过期）。
    #: 去重按目标原文：同一个文件被改三次，界面上只该出现一行。
    @property
    def touched_files(self) -> tuple[str, ...]:
        return _targets(self.steps, EDIT_TOOLS)

    @property
    def commands(self) -> tuple[str, ...]:
        return _targets(self.steps, SHELL_TOOLS)


def _targets(steps: tuple[StepView, ...], names: frozenset[str]) -> tuple[str, ...]:
    out: list[str] = []
    for step in steps:
        if step.name.strip().lower() not in names:
            continue
        target = step.target.strip()
        # 认不出目标的步骤不列：空一行的「碰过的文件」会被人读成「它
        # 动了一个我不知道的文件」—— 那比不列更吓人。
        if target and target not in out:
            out.append(target)
    return tuple(out)


class _Turn:
    """一轮的可变状态。**所有跨线程字段都在这把锁下读写**。"""

    __slots__ = (
        "turn_id", "brief", "text", "directory", "model", "agent", "variant",
        "mode", "status", "reason", "notes", "tools", "steps", "tool_calls", "ask",
        "context", "context_note",
        "answer", "decision", "stop_requested", "gate", "done", "ask_event",
        "started_dt", "started_at", "started_mono", "finished_mono",
        #: call_id → steps 下标。**只在内存里配对，不落库**：步骤持久化只有
        #: name/target/status 三字段（StepView.to_dict），id 是内部串联坐标。
        "call_index", "seen_cards", "last_recover",
        #: 本轮内「点过本会话内允许此类」的动作名集合（V1.138）。挂 _Turn
        #: 上 = 一轮有效，与飞书「一次委派」边界对齐；换一轮重新问。
        "session_allows",
    )

    def __init__(self, *, turn_id: str, text: str, directory: str, model: str,
                 agent: str, variant: str, mode: str, started_dt: Any,
                 started_mono: float, context: str = "",
                 context_note: str = "") -> None:
        self.turn_id = turn_id
        #: 发出去的那一句（**已折成单行**）。``text`` 是引擎回的话，
        #: 两者刻意分开：界面上的正文只能是**引擎说的**，不能混进用户的问题，
        #: 否则回看时读不出「哪句是我说的」。
        self.brief = one_line(text)
        #: 随这句话一起发出去的**背景**（单行原文，21.5 / V1.115）。
        #: 与 ``brief`` 分开存：界面上「我说了什么」只能是用户那句，
        #: 上下文是系统加的，混在一起会让人以为那句话是自己说的。
        self.context = context
        self.context_note = context_note
        self.text = ""
        self.directory = directory
        self.model = model
        self.agent = agent
        self.variant = variant
        self.mode = mode
        self.status = RUNNING
        self.reason = ""
        self.notes: list[str] = []
        self.tools: list[str] = []
        self.steps: list[StepView] = []
        self.tool_calls = 0
        self.ask: AskCard | None = None
        self.answer = ""
        self.decision: str | None = None
        self.stop_requested = False
        self.gate = threading.Event()
        self.done = threading.Event()
        #: 「有一张卡挂上了」的信号。前台不轮询它（它每 1 秒读快照就够），
        #: 但脚本与测试需要一个**事件**而不是「睡一会儿再看」——
        #: 后者在本仓是被登记的例外，不是默认做法。
        self.ask_event = threading.Event()
        #: 落库用的**时刻对象**（``started_at`` 是给人看的秒级串）。
        #: 两个都留：串进快照／进库的列用串，算 ``elapsed`` 与"多久以前"
        #: 必须用对象 —— 拿串去算会把它当本地时间再解释一遍。
        self.started_dt = started_dt
        self.started_at = started_dt.isoformat(timespec="seconds")
        self.started_mono = started_mono
        self.finished_mono = 0.0
        self.call_index: dict[str, int] = {}
        self.seen_cards: set[str] = set()
        self.last_recover = 0.0
        self.session_allows: set[str] = set()


class AgentTurnService:
    """前台的一轮执行。**一个服务实例 = 一个进程里唯一的那条线。**

    刻意不复用多个并行轮：同一个 opencode 会话上并发两轮，事件流会互相
    串（V2 的事件按会话广播）。前台是单用户，串行是正确的那一档。
    """

    def __init__(
        self,
        host: Any,
        *,
        repo: Any = None,
        max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
        max_seconds: float = DEFAULT_MAX_SECONDS,
        now: Callable[[], float] | None = None,
        clock: Clock | None = None,
        # 「把答复送回引擎」的重试间隔可注入：失败路径的测试不真睡。
        reply_sleep: Callable[[float], Any] | None = None,
    ) -> None:
        self._host = host
        self._repo = repo
        self._max_tool_calls = max_tool_calls
        self._max_seconds = max_seconds
        self._now = now or time.monotonic
        self._clock = clock or SystemClock()
        self._reply_sleep = reply_sleep or time.sleep
        self._lock = threading.RLock()
        self._turns: dict[str, _Turn] = {}
        self._active: str = ""
        self._seq = 0
        # 上一进程留下的 ``running`` 行要先收口，否则列表里会一直转着一个
        # 永远不会结束的圈 —— 「看起来在工作」是本仓最坏的失效形态。
        # 这里做（而不是让端点记得做）：**任何**建起服务的人都该得到同一
        # 个干净的起点，而这正是「一份能力一份实现」。
        self._reap_stale()

    def _reap_stale(self) -> None:
        if self._repo is None:
            return
        try:
            self._repo.reap(
                self._clock.now(),
                reason="FreeAgent 重启了，这一轮没跑完（它可能改了一半文件）",
            )
        except Exception:  # noqa: BLE001 - 收口失败不该让工作台开不起来
            log.warning("【工作台】上次残留的轮次没能收口", exc_info=True)

    # -- 对外 ---------------------------------------------------------- #
    def start(
        self,
        *,
        text: str,
        directory: str,
        model: str = "",
        agent: str = "",
        variant: str = "",
        mode: str = ENGINE_ASK,
        context: str = "",
        context_note: str = "",
    ) -> TurnView:
        """开一轮。**立即返回**，真正的执行在后台线程。"""
        if mode not in (ENGINE_ASK, ENGINE_AUTO_DIR):
            raise ValueError(f"未知授权档 {mode!r} —— 不猜，也不悄悄退回默认")
        if not (text or "").strip():
            raise ValueError("这句话是空的")
        with self._lock:
            active = self._turns.get(self._active)
            if active is not None and active.status in (RUNNING, WAITING):
                raise ValueError(
                    "上一轮还在跑 —— 先停它，或等它结束（同一个会话不能开两条线）"
                )
            self._seq += 1  # 只用于线程名与日志，不再是 id（id 由 new_id() 给）
            # ``new_id()`` 而不是 ``t{seq}``：**id 要跨进程唯一**。V1.114 用
            # 进程内自增，重启后从 t1 重新数 —— 一旦落库，新的一轮会把上一
            # 次「t1」那一行顶掉，而主人点开历史看到的是另一件事的正文。
            turn_id = f"w{new_id()}"
            turn = _Turn(
                turn_id=turn_id, text=text.strip(), directory=directory,
                model=model, agent=agent, variant=variant, mode=mode,
                started_dt=self._clock.now(), started_mono=self._now(),
                context=context.strip(), context_note=context_note,
            )
            self._turns[turn_id] = turn
            self._active = turn_id
            # 只留最近 20 轮在**内存**里：那是前台的一次性现场（快照、卡、
            # 事件），长期记录已经落库（``workbench_turns``）。
            while len(self._turns) > 20:
                oldest = next(iter(self._turns))
                if oldest == turn_id:
                    break
                self._turns.pop(oldest, None)
        self._persist_start(turn)
        thread = threading.Thread(
            target=self._run, args=(turn,), daemon=True,
            name=f"agent-turn-{turn_id}",
        )
        thread.start()
        return self.view(turn_id)  # type: ignore[return-value]

    def view(self, turn_id: str) -> TurnView | None:
        """一轮的快照。**活的那一轮优先，否则回库读。**

        为什么回库：重启之后内存里什么都没有，而「点开最近那件事」必须
        还能读到它 —— 否则「最近任务」列表点开是空的，比没有列表更糟。
        """
        with self._lock:
            turn = self._turns.get(turn_id)
        if turn is None:
            if self._repo is None:
                return None
            try:
                row = self._repo.get(turn_id)
            except Exception:  # noqa: BLE001 - 读坏一条不该让整个面板崩
                log.warning("【工作台】回合 %s 回读失败", turn_id, exc_info=True)
                return None
            return self._row_to_view(row) if row is not None else None
        with self._lock:
            now = turn.finished_mono or self._now()
            return TurnView(
                turn_id=turn.turn_id,
                status=turn.status,
                text=turn.text,
                brief=turn.brief,
                tools=tuple(turn.tools),
                steps=tuple(turn.steps),
                context_note=turn.context_note,
                tool_calls=turn.tool_calls,
                reason=turn.reason,
                notes=tuple(turn.notes),
                ask=turn.ask,
                directory=turn.directory,
                model=turn.model,
                agent=turn.agent,
                variant=turn.variant,
                mode=turn.mode,
                started_at=turn.started_at,
                elapsed=round(max(0.0, now - turn.started_mono), 1),
                ok=turn.status == DONE,
            )

    def latest(self) -> TurnView | None:
        """最近一轮。**在跑的那一轮优先，否则取库里最新的一行。**

        这样「重启之后打开工作台」看到的是上次干到哪，而不是一片空白。
        """
        with self._lock:
            active = self._active
        if active:
            view = self.view(active)
            if view is not None:
                return view
        if self._repo is None:
            return None
        try:
            rows = self._repo.recent(1)
        except Exception:  # noqa: BLE001
            log.warning("【工作台】最近一轮回读失败", exc_info=True)
            return None
        return self._row_to_view(rows[0]) if rows else None

    def history(self, limit: int = 20) -> tuple[TurnView, ...]:
        """最近的若干轮（新的先列）。**只给列表用**，不带卡、不带事件。

        没有仓储时返回空元组并**不装作有历史** —— 那与「历史是空的」
        是两件事，界面据 ``available`` 分开说（见端点层）。
        """
        if self._repo is None:
            return ()
        try:
            rows = self._repo.recent(limit)
        except Exception:  # noqa: BLE001
            log.warning("【工作台】历史回读失败", exc_info=True)
            return ()
        return tuple(v for v in (self._row_to_view(r) for r in rows) if v)

    def has_history(self) -> bool:
        """有没有「可以回访的历史」这件能力（而不是「历史里有没有东西」）。"""
        return self._repo is not None

    def decide(self, turn_id: str, *, allow: bool, answer: str = "",
               scope: str = "") -> bool:
        """人点了授权卡 / 答了提问。**只记账，不在这里发请求**。

        为什么不在 HTTP 线程里直接调 ``reply_permission``：那会把一次网络
        往返挂在服务端锁上，而"点了没反应"正是本仓反复防的那种症状。
        记录结论 + 唤醒执行线程，后者负责发。

        ``scope="session"``（V1.138）表示人点的是「本会话内允许此类」：
        在唤醒执行线程前先把这张卡的动作名记进本轮免问集合。只在 ``allow``
        且动作名认得出（非空）时记 —— 空动作名不记，fail-closed 到每次都问。
        """
        with self._lock:
            turn = self._turns.get(turn_id)
            if turn is None or turn.ask is None:
                return False
            turn.decision = "allow" if allow else "deny"
            turn.answer = answer
            if allow and scope == "session" and turn.ask.permission:
                turn.session_allows.add(turn.ask.permission)
            turn.gate.set()
            return True

    def stop(self, turn_id: str) -> bool:
        """人点了停止。**先记意图，再尽力叫停**（顺序不能反）。"""
        with self._lock:
            turn = self._turns.get(turn_id)
            if turn is None or turn.status not in (RUNNING, WAITING):
                return False
            turn.stop_requested = True
            turn.gate.set()
        self._host.abort()
        return True

    def wait(self, turn_id: str, timeout: float = 30.0) -> TurnView | None:
        """等一轮结束。**只给测试与脚本用** —— 前台走轮询快照。"""
        with self._lock:
            turn = self._turns.get(turn_id)
        if turn is None:
            return None
        turn.done.wait(timeout)
        return self.view(turn_id)

    def wait_for_ask(self, turn_id: str, timeout: float = 30.0) -> AskCard | None:
        """等「有一张卡挂出来了」。返回那张卡；等不到就 ``None``。

        为什么不用「睡一会儿再读快照」：那样既慢又赌时间，而这里本来就
        有一个可等待的事件。本仓对 ``time.sleep`` 的态度是「必须有理由，
        而且要登记」—— 能用事件等的地方不该用它。
        """
        with self._lock:
            turn = self._turns.get(turn_id)
        if turn is None:
            return None
        turn.ask_event.wait(timeout)
        view = self.view(turn_id)
        return view.ask if view else None

    # -- 执行 ---------------------------------------------------------- #
    def _run(self, turn: _Turn) -> None:
        deadline = turn.started_mono + self._max_seconds
        try:
            self._host.ensure(
                directory=turn.directory, mode=turn.mode,
                model=turn.model, agent=turn.agent, variant=turn.variant,
            )
            # **先挂事件流，再发指令**：SSE 是 volatile 的，订阅前发出的帧会丢。
            # 启动瞬间就失败（模型不可路由 / 额度没了）那一轮，终态帧在指令后
            # 不到一秒就发出来；流要是 prompt 之后才建，这一帧就漏了，轮次只能
            # 干等到总时长超时 —— 界面上一直「在跑」、步骤与正文全空、也停不掉。
            budget = max(5.0, deadline - self._now())
            events = self._host.events(timeout=budget)
            self._host.prompt(
                one_line(_prompt_text(turn)), model=turn.model, agent=turn.agent,
                variant=turn.variant,
            )
            # 先发指令再捞一次挂起：起轮次前就等着的请求，事件流可能漏带。
            turn.last_recover = self._now()
            self._recover_pending(turn)
            reached_end = False
            try:
                for kind, props in events:
                    self._on_event(turn, kind, props)
                    with self._lock:
                        stopped = turn.stop_requested
                    if stopped:
                        # 人点了停止：当场收成 stopped，不等到下一条事件 ——
                        # 否则挂起答复把状态退回 running 后，收口会误判成 failed。
                        self._set(turn, STOPPED, "你点了停止")
                        reached_end = True
                        break
                    if turn.status not in (RUNNING, WAITING):
                        reached_end = True
                        break
                    if self._now() - turn.last_recover >= _RECOVER_INTERVAL:
                        turn.last_recover = self._now()
                        self._recover_pending(turn)
                    if self._now() >= deadline:
                        self._fail(turn, "总时长超限 —— 已叫停（它可能还在改文件，所以必须真的停）")
                        self._host.abort()
                        reached_end = True
                        break
            finally:
                # 收口生成器 = 关掉那条 SSE 连接。**尽力而为**：迭代器没有
                # ``close`` 时（替身、未来换成一次性的可迭代）不该让整轮
                # 失败 —— 那会把「流读完了」误报成「连接坏了」。
                close = getattr(events, "close", None)
                if close is not None:
                    close()
            if not reached_end and turn.status in (RUNNING, WAITING):
                # 迭代器结束却没有终态事件：流断了。
                self._fail(turn, "事件流结束了但没有「本轮结束」信号 —— 不能报成功")
        except Exception as exc:  # noqa: BLE001 - 任何异常都要落成一条能读的原因
            log.warning("【工作台】一轮失败（%s）：%s", turn.turn_id, exc,
                        exc_info=True)
            self._fail(turn, _readable_error(exc))
        finally:
            self._finish(turn)

    def _on_event(self, turn: _Turn, kind: str, props: Any) -> None:
        if kind == "session.error":
            self._fail(turn, _error_text(props))
            return
        if kind == "session.idle":
            # idle 不是一律「完成」：V2 的 idle 消息带 ``outcome``，failed /
            # interrupted 都混在这个事件族里。不看 outcome 就把失败报成完成，
            # 正是「它说做完了、其实没做完」那一类静默失效。
            outcome = _idle_outcome(props)
            if outcome == "failed":
                self._fail(turn, _error_text(props))
                return
            if outcome == "interrupted":
                self._set(turn, STOPPED, "引擎被中断")
                return
            self._set(turn, DONE, "完成")
            return
        # 工具步骤**先认**：``message.part.updated`` 既可能是文本块，也可能是
        # 一个 tool part —— 先按文本处理的话，带 ``part.type=="tool"`` 的
        # 事件会被当成「没有正文」，步骤就永远记不上（静默丢）。
        #
        # 内部工具事件名（``tool`` / ``tool.started`` / ``tool.done``）先按
        # 路由走：V2 的 called / done 帧不带工具名、没有 part，靠
        # ``tool_evidence`` 认形状会被挡在门外，那三段只能按事件名进来。
        if kind in ("tool", "tool.started", "tool.done"):
            self._on_tool(turn, kind, props, tool_evidence(kind, props))
            return
        evidence = tool_evidence(kind, props)
        if evidence is not None:
            self._on_tool(turn, kind, props, evidence)
            return
        if kind in ("message.part.delta", "message.part.updated"):
            self._apply_text(turn, kind, props)
            return
        if kind == "permission.asked":
            self._handle_permission(turn, props)
            return
        if kind == "question.asked":
            self._handle_question(turn, props)
            return

    # -- 事件投影 ------------------------------------------------------ #
    def _on_tool(self, turn: _Turn, kind: str, props: Any,
                 evidence: Any) -> None:
        """记一步。

        **数调用次数只认 ``tool``**：``tool.started`` / ``tool.done`` /
        ``message.part.updated`` 描述的是同一次调用的另两个时点，一起数会让
        40 次上限提前用完——症状是「活干到一半被叫停」，而日志里看着像真超了。

        **配对优先按 ``call_id``**（V2 真机 2.0.29：name 只在 started、参数在
        called、结果在 success/failed，三段各带同一个 ``id``）。没有 id 的 V1
        形状才退回「最后一行同名同目标」合并。
        """
        over = ""
        if kind == "tool":
            with self._lock:
                turn.tool_calls += 1
                if turn.tool_calls > self._max_tool_calls:
                    over = (
                        f"工具调用超限（{turn.tool_calls} 次 > "
                        f"{self._max_tool_calls} 次）—— 已叫停"
                    )
        if over:
            self._fail(turn, over)
            self._host.abort()
            return

        call_id = props.get("call_id") if isinstance(props, dict) else None
        status = _step_status(kind, props)

        with self._lock:
            idx = turn.call_index.get(call_id) if isinstance(call_id, str) else None
            if idx is not None and idx < len(turn.steps):
                # 同一次调用的后段（called 补目标 / done 收终态）：回到早先那行。
                row = turn.steps[idx]
                name = evidence.tool if evidence is not None else ""
                name = name or row.name
                merged = dict(props)
                merged.setdefault("tool", name)
                ev = tool_evidence(kind, merged)
                target = (ev.target if ev else "") or row.target
                turn.steps[idx] = StepView(name=name, target=target, status=status)
                return

            name = evidence.tool if evidence is not None else ""
            if not isinstance(name, str) or not name:
                # 完成态丢了开始帧、又认不出名字：不编一个名字硬列一行。
                return
            merged = dict(props)
            merged.setdefault("tool", name)
            ev = tool_evidence(kind, merged)
            target = ev.target if ev else ""

            same = (
                bool(turn.steps)
                and (turn.steps[-1].name, turn.steps[-1].target) == (name, target)
            )
            if not turn.tools or turn.tools[-1] != name:
                turn.tools.append(name)
            step = StepView(name=name, target=target, status=status)
            if same:
                turn.steps[-1] = step
            else:
                turn.steps.append(step)
                if isinstance(call_id, str) and call_id:
                    turn.call_index[call_id] = len(turn.steps) - 1

    def _apply_text(self, turn: _Turn, kind: str, props: Any) -> None:
        if not isinstance(props, dict):
            return
        if kind == "message.part.delta":
            delta = props.get("delta")
            if isinstance(delta, str):
                with self._lock:
                    turn.text += delta
            return
        part = props.get("part")
        whole = part.get("text") if isinstance(part, dict) else None
        if isinstance(whole, str):
            # 整块替换而不是拼接：``message.part.updated`` 给的是**整段**，
            # 拼接会让同一段出现两遍（V1 踩过，症状是正文越来越长）。
            with self._lock:
                turn.text = _strip_reference_markers(whole)

    def _handle_permission(self, turn: _Turn, props: Any) -> None:
        request = current_adapter().parse_request(props)
        if request is None:
            # 载荷认不出：**不能放行**。放行等于「看不懂就问都不问」。
            self._note(turn, "收到一条认不出的授权请求 —— 已按拒绝处理")
            self._fail(turn, "授权请求的载荷认不出，已中止（fail-closed）")
            self._host.abort()
            return
        detail = "、".join(request.paths[:3]) if request.paths else ""
        with self._lock:
            if request.request_id in turn.seen_cards:
                return
            turn.seen_cards.add(request.request_id)
            session_hit = bool(
                request.permission and request.permission in turn.session_allows
            )
        if session_hit:
            # 本轮内此类动作已放行 → 直接送 allow 回引擎，不挂卡、不置 WAITING
            # （V1.138）。**必须真的回一句 allow**：只跳过不回，引擎那边仍挂着
            # 请求，又变成「点了没反应」卡到超时 —— 与 #5 同一类失效。
            self._note(
                turn,
                f"本会话内已放行同类动作（{request.permission}）—— 直接放行，不再问",
            )
            _reply = deliver_reply(
                lambda: self._host.reply_permission(request.request_id, "allow"),
                sleep=self._reply_sleep,
            )
            if not _reply.ok:
                self._note(
                    turn,
                    f"自动放行没能送回引擎（{_reply.error}）—— 已停住本轮",
                )
                self._fail(turn, f"授权结论没能送回引擎：{_reply.error}")
                self._host.abort()
            return
        card = AskCard(
            request_id=request.request_id, kind="permission",
            label=request.permission or "请求授权", detail=detail,
            permission=request.permission or "",
        )
        if turn.mode == ENGINE_AUTO_DIR:
            # 自动档下**本该一条都不来**。来了说明覆盖层没生效 ——
            # 那时唯一安全的动作是拒绝并说出来，而不是顺手放行。
            self._note(
                turn,
                "本目录自动档下引擎仍来问了一次 —— 权限配置可能没生效，已按拒绝处理",
            )
            # 自动档下这条授权本就不该来。拒答要真的送回去 —— 吞掉异常等于
            # 引擎还挂着请求，而界面上「已按拒绝处理」是本轮唯一的说法。
            _reply = deliver_reply(
                lambda: self._host.reply_permission(request.request_id, "deny"),
                sleep=self._reply_sleep,
            )
            if not _reply.ok:
                self._note(
                    turn,
                    f"自动档下拒答也没能送回引擎（{_reply.error}）—— 已停住本轮",
                )
                self._fail(turn, f"授权结论没能送回引擎：{_reply.error}")
                self._host.abort()
            return
        self._ask_and_reply(turn, card, permission_reply=request.request_id)

    def _handle_question(self, turn: _Turn, props: Any) -> None:
        question = current_adapter().parse_question(props)
        if question is None:
            self._note(turn, "收到一条认不出的提问 —— 已中止（不猜答案）")
            self._fail(turn, "引擎的提问认不出，已中止")
            self._host.abort()
            return
        card = AskCard(
            request_id=question.request_id, kind="question",
            label="、".join(question.questions[:2]) or "引擎想问一件事",
            detail=" / ".join(question.options[:4]),
        )
        answer = self._ask_and_reply(turn, card)
        if not answer:
            self._fail(turn, "引擎在等你回答问题，但没有拿到答案 —— 已中止")
            self._host.abort()
            return
        # 回答送不到 = 引擎还挂着那个问题。此前 suppress 吞掉，界面上却是
        # 「已答复」—— 与授权那侧同一种「看起来在工作」的失效。
        _reply = deliver_reply(
            lambda: self._host.reply_question(question.request_id, [[answer]]),
            sleep=self._reply_sleep,
        )
        if not _reply.ok:
            self._note(turn, f"你的回答没能送回引擎（{_reply.error}）—— 已停住本轮")
            self._fail(turn, f"回答没能送回引擎：{_reply.error}")
            self._host.abort()

    def _recover_pending(self, turn: _Turn) -> None:
        """兜底：把实时事件漏掉的挂起授权捞出来摆成卡。

        与事件流走同一份 ``parse_request`` 判据；认不出的挂起项在这里跳过
        （不在兜底里 Fail 整轮 —— 那是主路径收到坏载荷时的处置）。已挂过卡 /
        已应答过的 rid 不重复摆。
        """
        if turn.status not in (RUNNING, WAITING):
            return
        try:
            pending = self._host.pending_permissions(directory=turn.directory)
        except Exception:  # noqa: BLE001 - 兜底查询失败不阻断这一轮
            return
        adapter = current_adapter()
        for props in pending:
            request = adapter.parse_request(props)
            if request is None:
                continue
            rid = request.request_id
            with self._lock:
                if rid in turn.seen_cards:
                    continue
            # 登记进 seen_cards 由 ``_handle_permission`` 统一做；这里先登记
            # 会让内部去重把刚要摆的卡自己跳过。
            self._handle_permission(turn, props)

    def _ask_and_reply(self, turn: _Turn, card: AskCard,
                       *, permission_reply: str | None = None) -> str:
        """把卡挂出去等人；返回答案（授权卡返回 ``allow``/``deny``）。"""
        with self._lock:
            turn.ask = card
            turn.decision = None
            turn.answer = ""
            turn.gate.clear()
            turn.ask_event.clear()
            turn.status = WAITING
        # 先置 WAITING 再发信号：读快照的一方看到卡时，状态已经是「在等你」。
        turn.ask_event.set()
        deadline = turn.started_mono + self._max_seconds
        while True:
            with self._lock:
                if turn.decision is not None:
                    decision, answer = turn.decision, turn.answer
                    turn.decision = None
                    break
                stopped = turn.stop_requested
            if stopped:
                decision, answer = "deny", ""
                break
            remain = deadline - self._now()
            if remain <= 0:
                decision, answer = "deny", ""
                self._note(turn, "等回答超时 —— 已按拒绝处理")
                break
            turn.gate.wait(min(0.5, remain))
        with self._lock:
            turn.ask = None
            turn.status = RUNNING
        if permission_reply is not None:
            # 卡面已经画了「已允许 / 已拒绝」，这是人能看到的唯一结论；
            # 若它没送回引擎，引擎会一直等 —— 必须让这一轮停在这里并说出来，
            # 不能让「点过了」变成「生效了」。
            _reply = deliver_reply(
                lambda: self._host.reply_permission(permission_reply, decision),
                sleep=self._reply_sleep,
            )
            if not _reply.ok:
                self._note(
                    turn,
                    f"你批了，但没能送回引擎（{_reply.error}）—— 已停住本轮",
                )
                self._fail(turn, f"授权结论没能送回引擎：{_reply.error}")
                self._host.abort()
            return decision
        return answer if decision == "allow" else ""

    # -- 状态 ---------------------------------------------------------- #
    def _set(self, turn: _Turn, status: str, reason: str) -> None:
        with self._lock:
            turn.status = status
            turn.reason = reason

    def _fail(self, turn: _Turn, reason: str) -> None:
        with self._lock:
            if turn.status in (DONE, FAILED, STOPPED):
                return
            turn.status = STOPPED if turn.stop_requested else FAILED
            turn.reason = reason

    def _note(self, turn: _Turn, note: str) -> None:
        with self._lock:
            if note not in turn.notes:
                turn.notes.append(note)

    def _close_steps_locked(self, turn: _Turn) -> None:
        """轮次收口时把还停在 running 的步骤补终态。

        真机 2.0.29 曾出现：完成事件一条都没被认到，15 条步骤全卡「进行中」，
        而轮次已经是完成。但**完成态没有完成证据不往完成倒**（本仓纪律：往
        「还没完」倒）——所以只在失败 / 停止时把未竟步骤记 failed；正常完成
        仍停在 running 的步骤，说明我们没看到它收尾，不替它判完成。
        """
        if turn.status not in (FAILED, STOPPED):
            return
        turn.steps = [
            StepView(name=s.name, target=s.target,
                     status=STEP_FAILED if s.status == STEP_RUNNING else s.status)
            for s in turn.steps
        ]

    def _finish(self, turn: _Turn) -> None:
        with self._lock:
            if turn.status in (RUNNING, WAITING):
                # 线程收尾时还是「在跑」：说明既没 idle 也没 error ——
                # 那种情况**不能**落成完成。
                turn.status = FAILED
                turn.reason = turn.reason or "这一轮没有正常结束"
            if turn.stop_requested and turn.status not in (FAILED,):
                turn.status = STOPPED
                turn.reason = turn.reason or "你点了停止"
            turn.finished_mono = self._now()
            self._close_steps_locked(turn)
        # 先落库再放信号：``wait()`` 返回时，库里已经是终态 —— 否则测试与
        # 「点开最近那件事」会读到一个还在 running 的旧行（看起来在工作）。
        self._persist_finish(turn)
        turn.done.set()
        turn.ask_event.set()  # 让还在等卡的人醒过来，而不是等满超时

    # -- 落库 ---------------------------------------------------------- #
    def _persist_start(self, turn: _Turn) -> None:
        """把「这一轮开始了」写进去。**失败不阻断执行。**

        记账失败不该让一件活跑不起来 —— 但**必须看得见**：失败会在当前
        快照里留一条备注（「这一轮没能落库」），而不是悄悄没有历史。
        """
        if self._repo is None:
            return
        try:
            self._repo.add(
                turn_id=turn.turn_id, brief=turn.brief,
                directory=turn.directory, mode=turn.mode,
                started_at=turn.started_dt, model=turn.model,
                agent=turn.agent, variant=turn.variant,
                context_note=turn.context_note,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("【工作台】这一轮没能落库：%s", exc)
            self._note(turn, f"这一轮没能落库（{exc}）—— 关掉网页之后就看不到它了")

    def _persist_finish(self, turn: _Turn) -> None:
        if self._repo is None:
            return
        elapsed = round(max(0.0, turn.finished_mono - turn.started_mono), 1)
        try:
            self._repo.finish(
                turn.turn_id, status=turn.status,
                text=_strip_reference_markers(turn.text),
                reason=turn.reason, notes=list(turn.notes),
                tools=list(turn.tools),
                steps=[s.to_dict() for s in turn.steps],
                tool_calls=turn.tool_calls, elapsed=elapsed,
                finished_at=self._clock.now(),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("【工作台】这一轮的结局没能落库：%s", exc)

    def _row_to_view(self, row: dict[str, object]) -> TurnView | None:
        """库里的一行 → 快照。**JSON 读坏要说出来**，不装作「那轮没有工具」。"""
        try:
            turn_id = str(row.get("id") or "")
            if not turn_id:
                return None
            notes = _decode_list(row.get("notes_json"))
            tools = _decode_list(row.get("tools_json"))
            steps = _decode_steps(row.get("steps_json"))
            broken = notes is None or tools is None or steps is None
            return TurnView(
                turn_id=turn_id,
                status=str(row.get("status") or FAILED),
                text=_strip_reference_markers(str(row.get("text") or "")),
                brief=str(row.get("brief") or ""),
                tools=tuple(tools or ()),
                steps=tuple(steps or ()),
                context_note=str(row.get("context_note") or ""),
                tool_calls=int(row.get("tool_calls") or 0),
                reason=str(row.get("reason") or ""),
                notes=tuple(notes or ()) + (
                    ("这一轮的记录读不全（JSON 坏了）",) if broken else ()
                ),
                directory=str(row.get("directory") or ""),
                model=str(row.get("model") or ""),
                agent=str(row.get("agent") or ""),
                variant=str(row.get("variant") or ""),
                mode=str(row.get("mode") or ENGINE_ASK),
                started_at=str(row.get("started_at") or ""),
                elapsed=float(row.get("elapsed") or 0.0),
                ok=bool(row.get("ok")),
            )
        except (TypeError, ValueError):
            log.warning("【工作台】一行记录读不出来：%r", row)
            return None


_STEP_STATUS_ALIASES = {
    "completed": STEP_DONE,
    "finished": STEP_DONE,
    "done": STEP_DONE,
    "success": STEP_DONE,
    "running": STEP_RUNNING,
    "in_progress": STEP_RUNNING,
    "pending": STEP_RUNNING,
    "error": STEP_FAILED,
    "failed": STEP_FAILED,
}


def _step_status(kind: str, props: Any) -> str:
    """一条工具事件的「到哪了」。

    优先读载荷里的 ``part.state.status``（V2 实测在这里，值形如
    ``completed`` / ``error``）；**读不到才退回事件名**：``tool.started``
    是「开始」，``tool`` 在 V1 实测是执行完之后 —— 事件名是最不稳的一层，
    所以它只当兜底，不当判据。
    """
    part = props.get("part") if isinstance(props, dict) else None
    state = part.get("state") if isinstance(part, dict) else None
    if state is None and isinstance(props, dict):
        state = props.get("state")
    raw = state.get("status") if isinstance(state, dict) else None
    if isinstance(raw, str):
        mapped = _STEP_STATUS_ALIASES.get(raw.strip().lower())
        if mapped is not None:
            return mapped
        # 认得是状态字段但不认识这个值：**当成在跑**而不是当成完成。
        # 往「还没完」那边倒 —— 往完成倒的表现是「它说做完了、其实没做完」。
        return STEP_RUNNING
    return STEP_RUNNING if "started" in kind.lower() else STEP_DONE


def _prompt_text(turn: _Turn) -> str:
    """真正发给引擎的那一行：我说的话 + 背景。

    背景**跟在后面**而不是前面：模型读到的第一句应当是人的要求，
    背景是补充材料（放在前面会让人自己的话看起来像注释）。
    """
    if not turn.context:
        return turn.brief
    return f"{turn.brief} [上下文] {turn.context}"


#: 引擎正文里残留的孤立引用编号标记（形如 ``@17@``）。真机 2.0.29 上模型把
#: 消息 / 部件序号写成这个串透出来，界面上没有对应链接，纯噪音。只剥
#: ``@纯数字@``，不误伤正文里的邮箱或 @提及。
_REF_MARKER = re.compile(r"@\d+@")


def _strip_reference_markers(text: str) -> str:
    """剥掉正文里的 ``@数字@`` 占位标记，并清掉残留的空行 / 行尾空格。"""
    if not text:
        return text
    cleaned = re.sub(rf"[ \t]*{_REF_MARKER.pattern}[ \t]*", "", text)
    cleaned = re.sub(r"[ \t]+(\n)", r"\1", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _decode_steps(raw: Any) -> list[StepView] | None:
    """库里的 ``steps_json`` → 步骤清单；**读坏返回 ``None``**（同 ``_decode_list``）。"""
    if isinstance(raw, (list, tuple)):
        items = raw
    elif isinstance(raw, str):
        try:
            items = json.loads(raw)
        except (TypeError, ValueError):
            return None
    else:
        return None
    if not isinstance(items, list):
        return None
    out: list[StepView] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name:
            continue
        out.append(StepView(
            name=name,
            target=str(item.get("target") or ""),
            status=str(item.get("status") or STEP_DONE),
        ))
    return out


def _decode_list(raw: Any) -> list[str] | None:
    """库里的 JSON 列 → 字符串列表；**读坏返回 ``None``**（与「空列表」分开）。

    两个值必须分开：``[]`` 是「那轮确实没碰工具」，``None`` 是「这一格读
    不出来」—— 合成一个，读坏的那行会被显示成「什么都没做」，那是把故障
    说成事实（本仓最不接受的失效形态）。
    """
    if isinstance(raw, (list, tuple)):
        return [str(x) for x in raw]
    if not isinstance(raw, str):
        return None
    try:
        loaded = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(loaded, list):
        return None
    return [str(x) for x in loaded]


def _idle_outcome(props: Any) -> str:
    """读 idle 事件的 ``outcome``（``succeeded`` / ``failed`` / ``interrupted``）。

    认不出 / 没带时返回空串：旧版本与 V1 的 idle 不带这个字段，按「完成」
    兜底；不把缺字段猜成 failed。
    """
    if not isinstance(props, dict):
        return ""
    value = props.get("outcome")
    return str(value).strip().lower() if isinstance(value, str) else ""


def _error_text(props: Any) -> str:
    """从 ``session.error`` 载荷里取一句能读的话。

    形状随版本变（V1 在 ``properties.error``，V2 在 ``data.error``），
    而两者都可能在 ``error.data.message`` 上再包一层。**认不出就给一句
    说明**，不编原因 —— 编出来的原因会把人引到错误的排查方向。
    """
    if not isinstance(props, dict):
        return "引擎报错（载荷不是对象，无法读出原因）"
    for path in (("error", "data", "message"), ("error", "message"),
                 ("message",), ("error", "name")):
        node: Any = props
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
        if isinstance(node, str) and node.strip():
            return node.strip()
    return "引擎报错（认不出载荷形状，原始内容见引擎日志）"


def _readable_error(exc: Exception) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    return text
