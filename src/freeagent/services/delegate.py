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
    #: 这次**实际**用的 ``provider/model``，形如 ``deepseek/deepseek-v4-pro``。
    #:
    #: 为什么必须记：委派**不继承**你 ``~/.config/opencode/`` 里的 provider 配置
    #: （设计文档 11.8.1 配置隔离），所以「用哪个模型」由 ``--model`` 显式决定、
    #: 否则由 opencode 自己挑。**两者都会静默变化** ——
    #: 实测加了环境白名单之后，委派从 ``deepseek/deepseek-v4-pro``
    #: 悄悄换成了 ``opencode/big-pickle``，而产物里**一个字都没留**。
    #:
    #: 记下来，它就从「事后没人知道」变成「一眼可查」。
    model: str = ""


#: 一次委派在日志里可能留下的三种记录。**按出现顺序**看最后一条，
#: 才知道现在处于什么状态 —— 只看「有没有派过」是不够的。
_DISPATCH_TERMINAL = (
    RecordType.DELEGATION_DISPATCHED,
    RecordType.DELEGATION_SUCCEEDED,
    RecordType.DELEGATION_FAILED,
)


def already_dispatched(records: Sequence) -> bool:
    """这条事务**现在**该不该被跳过。

    踩过的坑（真机测出来的）：原来只问「有没有 ``delegation_dispatched``」，
    于是**失败的委派永远无法重派** —— 而落库时写的那条 note 偏偏写着
    「执行失败，可以 /note 记下原因后重派」。**文档承诺了重派，守卫禁止它。**
    结果是：一次失败就变成一条死任务，只能手工建新事务顶替。

    第二次踩坑（``--watch`` 实测，2026-09-30）：上面那条「失败可重派」修好之后，
    ``--watch`` 常驻把**配置类失败**放大成了持续损坏 —— 180 秒内扫了 60 轮、
    写了 60 个产物版本（v51→v60），而真正的委派一条没干成。

    根因：**「失败可重派」被无条件化了。** 但有些失败是**确定性**的 ——
    「没有闸门」「白名单拒绝」这类阻塞在任务之外，不改配置就永远不会成功。
    可重试的只有**执行期**的失败（模型偶发 402、opencode 崩了、网络抖了）。

    判据是**从记录推出来的**，不靠给失败加标签：
    **一次尝试里有没有真的 ``delegation_dispatched``。**

    ==============================  ==============  ================
    最后一次尝试                        记录形状          判定
    ==============================  ==============  ================
    （没有）                          —               没试过 → 派
    派发前就被拒                        failed（无前置）    **不重试**（本轮新增）
    执行期失败                        dispatched→failed  可重试
    跑着                              dispatched        别再派（会并发）
    做完                              succeeded         别再派
    ==============================  ==============  ================

    「派发前就被拒」不再自动重试，代价是：**修好配置后这条不会自动恢复**。
    那个恢复入口（让 ``/note`` 能重置）**至今没有实现** —— 落库那句
    「可以 /note 记下原因后重派」是**空头承诺**（note 不是 terminal 记录，
    什么也重置不了）。要么手工建新事务，要么将来真做那个入口。
    明确写在这里，免得下一个人以为有重试机制。

    ⚠️ ``--watch`` 下的**另一类**副作用仍然存在：``--tool-gate`` 路径下，
    持续失败会**周期性地重新发批准卡**（闸门仍要人点，不会静默重跑，
    但会刷卡片）。盯着失败任务排查时**别开** ``--watch``。
    """
    #: 本次尝试里**真的**派出去过吗。派发前就被拒的（缺闸门 / 白名单不符）
    #: 压根没走到 :func:`subprocess_runner`，所以这个标记是 ``False``。
    dispatched_in_attempt = False
    #: 最后一次尝试的结局。``None`` = 还没试过；
    #: ``"refused"`` = 试了但**派发前就被拒**（确定性失败，重试无用）。
    last: str | None = None
    for record in records:
        kind = record.type
        if kind is RecordType.DELEGATION_DISPATCHED:
            dispatched_in_attempt = True
        elif kind in _DISPATCH_TERMINAL:
            if kind is RecordType.DELEGATION_FAILED and not dispatched_in_attempt:
                last = "refused"
            else:
                last = "failed" if kind is RecordType.DELEGATION_FAILED else "succeeded"
            dispatched_in_attempt = False

    # 末尾是「已派出但还没有收尾记录」—— **正在跑**。
    #
    # 这一条我第一版漏了，回归测试当场抓住：只问「最后一条 terminal 是什么」
    # 的话，「在跑」和「没试过」都落在「没有 terminal」上，
    # 于是 ``--watch`` 会在 opencode 还在跑的时候再派一遍 ——
    # 两个进程改同一个项目目录。宁可漏派也不能并发派。
    if dispatched_in_attempt:
        return True
    if last is None:
        return False
    # 确定性失败不重派：重试一千次也是同一个结果。
    # 而执行期失败（last == "failed"）**要**能重派 ——
    # 否则又回到「一次失败变死任务」那个旧坑。
    return last != "failed"


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
    model = ""

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
        # **实际用的模型**。取**第一个**带 model 的那条，不追着最后一条 ——
        # 中途换模型时要能回答「它是怎么开跑的」，那才是「我以为我配的是哪个」
        # 这个问题的答案。（第一版写成无条件覆盖，结果拿到的是最后一个，
        # 与下面这句注释矛盾 —— 是回归测试把两者对出来的。）
        if not model:
            info = event.get("info")
            if isinstance(info, dict):
                m = info.get("model")
                if isinstance(m, dict) and m.get("providerID") and m.get("modelID"):
                    model = f"{m['providerID']}/{m['modelID']}"
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
        model=model,
    )
