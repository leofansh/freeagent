"""委派：把一件事交给外部执行器（opencode）在指定项目里完成。

这个模块存在的唯一理由
---------------------
「让编码 agent 去改某个项目」这件事本身**已经有人做得很好了**（opencode）。
所以这里**不去重造执行能力**，只做三件工具该做的事：

1. **把委派记成一个可追踪的工作项** —— 用已有的事务 / artifact / 日志，
   零新概念
2. **人类闸门** —— 只有你 ``/start`` 过的事务才会被派出去
3. **项目白名单** —— 路径必须事先配好，**不接受任意路径**

第 3 条是整个设计里最硬的约束。理由：委派链路的终点是「在你的机器上执行
代码」。如果路径是调用方给的，那么一条伪造的消息就能让 agent 动你任何一个
目录。白名单把这件事从「消息内容」降级为「只能选你预先批准过的选项」。

**不做的事**：不执行任何命令。执行在 :mod:`freeagent.delegate` 那个
**独立进程**里，不在这个模块，也不在 Web 层能触达的地方。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence

from ..domain import FreeAgentError, RecordType, Task
from ..domain.models import Task as DomainTask

__all__ = [
    "DelegationPolicy",
    "DelegationRequest",
    "DispatchOutcome",
    "check_project_allowed",
    "build_brief",
    "parse_opencode_output",
    "eligible_tasks",
    "already_dispatched",
]


@dataclass(frozen=True, slots=True)
class DelegationPolicy:
    """委派策略。**默认关闭** —— 白名单为空时什么都派不出去。"""

    #: 允许委派到的项目目录（绝对路径）。空 = 禁用委派。
    projects: tuple[str, ...] = ()
    #: 派给 opencode 的可执行文件名。刻意不来自配置 ——
    #: 配置项能被环境变量覆盖，而这是个安全相关的路径。
    command: str = "opencode"
    #: 用哪个模型。**空 = 用 opencode 自己的默认**。
    #:
    #: 刻意不给「收费的默认值」：OpenCode 账号余额不足时，收费模型会直接
    #: 402 失败。所以默认留空，用 opencode 的默认（它会挑能用的），
    #: 而要指定就用 ``*-free`` 那批 —— 实测它们不需要余额。
    model: str = ""
    #: 单次执行超时（秒）。opencode 改一个功能可能要很久，但不该无限等。
    timeout: float = 1800.0

    @property
    def enabled(self) -> bool:
        return bool(self.projects)


def check_project_allowed(policy: DelegationPolicy, raw: str) -> Path:
    """校验项目路径在白名单里，返回**规范化**后的绝对路径。

    三层校验，缺一不可：

    1. **必须已启用**（白名单非空）
    2. **必须绝对路径** —— 相对路径的基准会随工作目录变，是绕过白名单的经典手法
    3. **必须落在某个白名单目录内** —— 用 ``resolve`` + ``relative_to`` 判断，
       不用字符串前缀（``/data/proj`` 会匹配 ``/data/project-x``）
    """
    if not policy.enabled:
        raise FreeAgentError(
            "委派未启用。要用的话在配置文件里设 delegate.projects"
            "（只接受绝对路径的白名单）。"
        )
    text = raw.strip().strip('"').strip("'")
    if not text:
        raise FreeAgentError("要委派到哪个项目？给一个绝对路径。")

    candidate = Path(text)
    if not candidate.is_absolute():
        raise FreeAgentError(
            f"项目路径必须是绝对路径，收到「{text}」。"
            "相对路径会随工作目录变化，是绕过白名单的常见手法。"
        )
    resolved = candidate.resolve()
    for allowed in policy.projects:
        root = Path(allowed).expanduser().resolve()
        if resolved == root:
            return resolved
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        return resolved
    raise FreeAgentError(
        f"「{resolved}」不在允许委派的项目白名单里。"
        f"当前白名单：{', '.join(policy.projects) or '（空）'}"
    )


@dataclass(frozen=True, slots=True)
class DelegationRequest:
    """一次待派发的委派。"""

    task_id: str
    project: Path
    prompt: str
    title: str | None = None


@dataclass(frozen=True, slots=True)
class DispatchOutcome:
    """一次执行的结果。"""

    ok: bool
    summary: str
    detail: str = ""
    session_id: str | None = None
    tool_calls: tuple[str, ...] = ()


#: 一次委派在日志里可能留下的三种记录。**按出现顺序**看最后一条，
#: 才知道现在处于什么状态 —— 只看「有没有派过」是不够的。
_DISPATCH_TERMINAL = (
    RecordType.DELEGATION_DISPATCHED,
    RecordType.DELEGATION_SUCCEEDED,
    RecordType.DELEGATION_FAILED,
)


def already_dispatched(records: Sequence) -> bool:
    """这条事务**现在**该不该被跳过（已派且不该重派）。

    踩过的坑（真机测出来的）：原来只问「有没有 ``delegation_dispatched``」，
    于是**失败的委派永远无法重派** —— 而落库时写的那条 note 偏偏写着
    「执行失败，可以 /note 记下原因后重派」。**文档承诺了重派，守卫禁止它。**
    结果是：一次失败就变成一条死任务，只能手工建新事务顶替。

    现在看**最后一条**派发相关记录：

    ==========================  ========
    最后一条是                    含义
    ==========================  ========
    （没有）                     没派过 → 派
    ``delegation_dispatched``   在跑 → **别再派一次**（会并发跑同一件事）
    ``delegation_succeeded``    做完了 → 别再派
    ``delegation_failed``       失败 → **可重派**
    ==========================  ========

    「在跑」那条刻意**不**重派：``--watch`` 每轮都扫，而 opencode 可能要跑
    几分钟。不挡住就会连发好几个进程改同一个目录。

    ⚠️ ``--watch`` 下的副作用：对**持续失败**的任务，每轮都会重派、
    每轮都要重新发一张批准卡。不会静默重跑（闸门仍要人点），
    但会周期性地刷卡片。盯着失败任务排查时**别开** ``--watch``。
    """
    last = None
    for record in records:
        if record.type in _DISPATCH_TERMINAL:
            last = record.type
    if last is None:
        return False
    return last is not RecordType.DELEGATION_FAILED


def eligible_tasks(repo, records_repo) -> list[Task]:
    """够格被派发的事务。

    **闸门在这里，而且只有在这里**：
    * ``project_path`` 非空 —— 是委派事务
    * 状态是 ``active`` —— 你 ``/start`` 过。``inbox`` 的**绝不**派。
    * 还没派过、或者上次**失败** —— 失败可重派，见 :func:`already_dispatched`
    """
    from ..domain import TaskState

    out: list[Task] = []
    for task in repo.list_all():
        if not task.project_path:
            continue
        if task.state is not TaskState.ACTIVE:
            continue
        if already_dispatched(records_repo.list_for_task(task.id)):
            continue
        out.append(task)
    return out


#: 简报里用来拼字段的分隔符。刻意用中文标点，避免和内容里的符号混在一起。
_SEP = "；"


def one_line(text: str) -> str:
    """把任意文本压成**单行**：所有换行、连续空白都折成一个空格。

    这不是洁癖，是实测出来的硬约束（见 :func:`build_brief`）。
    """
    return " ".join(str(text).split())


def build_brief(task: DomainTask, intent: str | None, dod: str | None) -> str:
    """给 opencode 的需求简报。**必须是单行**。

    踩过的坑（实测，opencode 1.18.31）：提示里**只要有一个换行符**，
    opencode 就把它当「复杂任务」，升级到主 agent 那个强模型
    （本机是 ``claude-opus-5``），并**无视 ``--model``**。于是账户余额
    不足就直接 ``402``，委派全盘失败。

    对照实测（同一目录、同一 ``--model opencode/big-pickle``）：

    ==========================  ==========
    简报形态                     结果
    ==========================  ==========
    单行（纯需求 / 带上下文）      rc=0 成功
    任何含 ``\\n`` 的简报          rc=1，升级到 opus，402
    ==========================  ==========

    所以「格式好看」和「能跑」在这里是冲突的，**能跑优先**：
    刻意不用 ``#`` / ``##`` / ``-`` 那套 markdown 排版。
    """
    bits = [f"在当前项目目录里完成：{one_line(task.title)}"]
    if intent:
        bits.append(one_line(intent))
    if dod:
        bits.append(f"做到什么程度算完：{one_line(dod)}")
    bits.append("只在这个项目目录内改动，不要动目录外的文件")
    bits.append("改完跑一遍项目自带的测试（若有）")
    bits.append("用中文说清你改了什么、为什么这么改")
    brief = _SEP.join(bits)
    # 兜底：万一将来有人又往里塞了换行，这里挡住。
    assert "\n" not in brief, "简报必须单行：换行会让 opencode 升级到付费模型"
    return brief


#: opencode --format json 是**逐行** JSON 事件流。
_JSON_LINE = re.compile(r"^\s*\{.*\}\s*$")

#: 只把这些事件类型当成「有用输出」，其余（心跳、工具入参等）忽略。
_TEXT_TYPES = ("text", "result")


def _payload(event: dict) -> dict:
    """取事件真正的载荷。

    踩过的坑：真实输出里文本**嵌在 ``part`` 里**，顶层只有 ``type``：

    ``{"type":"text", "part":{... "type":"text", "text":"OK"}}``

    只读顶层 ``text`` 会拿到 ``None`` —— 于是每一次**成功**的委派
    都被误报成「没有产出」。这是实测才发现的，单靠猜事件形状写不出来。
    """
    part = event.get("part")
    if isinstance(part, dict) and part:
        return {**event, **part}
    return event


def parse_opencode_output(stdout: str) -> DispatchOutcome:
    """解析 ``opencode run --format json`` 的输出。

    它是逐行 JSON 事件流，不是单个 JSON。**必须容忍脏数据**：
    混进来的日志行、半个 JSON、模型吐的非 JSON 文本都不能让整次派发崩掉 ——
    崩掉意味着这条事务永远停在 active，下次重跑还会再崩一次。
    """
    texts: list[str] = []
    errors: list[str] = []
    session_id: str | None = None
    tools: list[str] = []

    for line in stdout.splitlines():
        if not _JSON_LINE.match(line):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue                      # 脏行直接跳过，不猜
        if not isinstance(event, dict):
            continue
        session_id = event.get("sessionID") or session_id
        data = _payload(event)
        kind = data.get("type") or event.get("type")
        if kind in _TEXT_TYPES:
            content = data.get("text") or data.get("content") or ""
            if isinstance(content, str) and content.strip():
                texts.append(content.strip())
        elif kind in ("tool_use", "tool"):
            name = data.get("name") or data.get("tool") or "?"
            if name not in tools:
                tools.append(str(name))
        elif kind in ("error", "apiError"):
            err = data.get("error")
            message = None
            if isinstance(err, dict):
                inner = err.get("data")
                message = (
                    inner.get("message") if isinstance(inner, dict) else None
                ) or err.get("message") or err.get("name")
            errors.append(
                str(message or data.get("message") or "执行失败")
            )

    if errors and not texts:
        return DispatchOutcome(
            ok=False,
            summary=errors[0][:200],
            session_id=session_id,
        )
    if not texts:
        return DispatchOutcome(
            ok=False,
            summary="执行器没有产出任何可读结果",
            detail=stdout[-500:],
            session_id=session_id,
        )
    return DispatchOutcome(
        ok=True,
        summary="\n\n".join(texts)[:4000],
        session_id=session_id,
        tool_calls=tuple(tools),
    )
