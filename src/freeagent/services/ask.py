"""``/ask``：把一个问题交给执行器，把答复拿回来（设计文档 11.10.6）。

## 它与11.8 委派的区别（一句话）

**问一句** vs **把一件事做掉**。所以：不落库、不进今天视图、不产出artifact、
**不问任何审批**。

## ⚠️ 「不问任何审批」的前提是「**改不了**」，而那必须被强制

11.10.6 的原话是「它不改任何东西，所以没有『有副作用』可拦」。
**那句话是个假设**：V1 未命中规则时大多默认 ``allow``（11.8.1 版本陷阱表），
所以一个不配权限的 ``/ask`` 就是**无人监督的、能改代码的**自主运行 ——
恰好是那六道闸门要防的东西。

所以这里传的是 :func:`~freeagent.services.opencode_server.read_only_permission_config`
（``edit``/``bash`` 一律 ``deny``）。而本模块**再加一道**：万一执行器仍然
挂起授权请求，本模块**绝不自动允许**，而是**中止会话并如实报错** ——
「配置没生效」这件事必须说出来，而不是被悄悄放行。

## 文本怎么攒（实测，别凭直觉）

一轮真实开发是 414 条事件，其中 ``message.part.delta`` 占 **298 条（72%）**：

- ``message.part.delta`` → **累加**（增量片段）
- ``message.part.updated`` → **覆盖**（整块全文）

拿 ``updated`` 去累加，同一段文字一轮会重复 19 次（实测过）。

## 超预算**不静默截断**

沿用 3.6 的纪律：报告「已用尽预算，还剩什么没做完」。
半途停下而不说，用户会以为它跑完了。

## core 记忆怎么进来（V1.97，设计文档 3.7.6 注入点①）

``/ask`` 是唯一走模型的白话入口，而它此前把原题直接发给执行器 ——
用户记下的约定/偏好，执行器一条都不知道（3.7.6 的注入点①于是成了
「文档说有、代码没有」）。现在调用方可传 ``context``（一段拼好的文本），
非空时拼在问题前面。

三条刻意的边界：

- **本模块只拼，不产**。``context`` 由调用方（CLI 经
  :meth:`~freeagent.services.memory.MemoryService.core_block`）拼好带进来，
  定性（「可能过期，以当前为准」）也由它自带 —— 本模块不另写、也不去掉。
  **为什么不让本模块直接调 memory 服务**：那会把 ask 层耦合到存储层，
  而它现在只认识 opencode_server；「一份能力一份实现」也要求拼装只有
  core_block 一处。
- **空块不注入**。``context`` 为空（或全空白）时问题原样发出 —— 不给
  问题前面留一对孤零零的换行，那会让「没带记忆」看起来像出了错。
- **brief 必须单行那条纪律（11.13.3）不适用于这里**：那是委派简报的
  硬约束（实测换行会让 opencode 升级强模型），``/ask`` 走的是普通
  prompt，多行无害。

## 答复的凭据档（V1.104，设计文档 11.10.9）

``/ask`` 把「它查过没有」和「它说了什么」混在同一段话里，语气还完全一样 ——
用户没法区分「读了代码才这么说」和「凭训练数据这么说」。三档由**可观察
事实**判，不看模型口吻：

* 确定 —— 至少一个文件类工具的目标在本机存在（``_resolve_target``）
* 推理 —— 调过工具，但没有一个目标能核（没带目标 / 通配模式 / 不存在）
* 猜想 —— 一次工具都没调用

**不做逐句归因**：要把「这句话属于哪一档」判出来，得做语义判断；让模型
自己标又不可信 —— 那只是把不确定换了个语气。所以只给**整段答复**的档，
并把它说成「凭据」而不是「正确性」（``KnowledgeBoundary`` 的字段注释）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from .tool_evidence import ToolEvidence, tool_evidence

__all__ = [
    "AskOutcome",
    "ask_executor",
    "DEFAULT_ASK_TIMEOUT",
    "KnowledgeBoundary",
    "knowledge_boundary",
    "GRADE_LABELS",
]

#: 单次 ``/ask`` 的总时长上限。刻意比委派短：问一句不该跑成一场委派。
DEFAULT_ASK_TIMEOUT = 180.0


@dataclass
class AskOutcome:
    """一次 ``/ask`` 的结果。**纯数据** —— 不落库、不进任何视图。"""

    ok: bool
    text: str = ""
    #: 没能完成时给用户看的**一句话原因**。三件必须可区分：
    #: 「超预算」/「执行器想要权限」（配置没生效）/「出错了」。
    reason: str = ""
    #: 真的发起了几次工具调用 —— 超预算时报「已用尽预算」要用它。
    tool_calls: int = 0
    #: 中止时**已经拿到**的正文。刻意保留：半截答案也比一句「失败了」有用，
    #: 而丢掉它就等于让用户重跑一次。
    partial: str = ""
    notes: list[str] = field(default_factory=list)
    #: 这次实际认得出的工具调用（V1.104）。**带默认值必须排最后** ——
    #: 插在前面会让构造器签名直接崩（non-default argument follows default）。
    evidence: tuple[ToolEvidence, ...] = ()


#: 知识边界三档的机器键 → 中文名。键是契约（CLI / 测试按它判）。
GRADE_LABELS = {
    "verified": "确定",
    "inferred": "推理",
    "guessed": "猜想",
}


@dataclass(frozen=True, slots=True)
class KnowledgeBoundary:
    """一条答复的**知识边界**：它站在本机凭据上，还是站在模型自己的知识上。

    ## 「确定」不等于「对」

    这一档说的是**凭据**，不是结论 —— 它读到了真文件，不代表从文件得出的
    判断就对。所以字段叫 ``grade``（凭据档位），输出里也必须带那句定性；
    把它读成「这句话是对的」就是这套东西最坏的误用。
    """

    grade: str
    label: str
    basis: str
    checked: tuple[str, ...] = ()


def _resolve_target(target: str, project: Path) -> Path | None:
    """把工具目标解析成本机路径；**不是可核的路径就返回 ``None``**。

    三类刻意不核：

    * **glob 模式**（含 ``*`` / ``?`` / ``[``）—— 拿去 ``exists()`` 永远为假，
      于是「用它找过文件」会被误判成「凭据落空」。而误判的方向最坏：
      一个老实查过文件、只是用了 glob 的答复会被标成「推理」。
    * 空串 / 明显不是路径的（无分隔符且无扩展名）。
    * 解析或探测抛异常（Windows 上非法字符、路径过长）—— 那一项算**没核到**，
      不往上抛：边界标注坏了不该让一次成功的提问变成失败。
    """
    raw = target.strip()
    if not raw or any(ch in raw for ch in "*?["):
        return None
    try:
        path = Path(raw)
        if not path.is_absolute():
            path = project / path
        return path if path.exists() else None
    except (OSError, ValueError):
        return None


def knowledge_boundary(
    evidence: Sequence[ToolEvidence],
    project: str | Path,
) -> KnowledgeBoundary:
    """按**这次实际碰到的本机凭据**分档（11.10.9），不看模型的口吻。

    三条判据全部是可观察事实，逐条都能反驳：

    * ``verified``（确定）—— 至少一个文件类工具的目标在本机存在：它碰到了
      真的东西，人可以照着清单自己去看。
    * ``inferred``（推理）—— 调过工具，但没有一个目标能核（没带目标 /
      目标不存在 / 只用了 glob）：动过手，但没落到可核的位置。
    * ``guessed``（猜想）—— **一次工具都没调用**：这段答复来自它已有的
      知识，不是本机查证。对通用知识够用，对本项目细节要当心。

    判不出「哪一句话属于哪一档」—— 逐句归因需要语义判断，程序做不到，
    让模型自评又不可信（那只是把不确定换了个语气）。所以这里只给**整段
    答复**的档位，并把它说清楚（12.10.12 同一个取舍：宁可粗，不要假）。
    """
    project_path = Path(project)
    if not evidence:
        return KnowledgeBoundary(
            grade="guessed",
            label=GRADE_LABELS["guessed"],
            basis="一次工具都没调用 —— 这段答复来自它已有的知识，不是本机查证",
        )

    checked: list[str] = []
    for ev in evidence:
        if not ev.is_file_tool or not ev.target:
            continue
        resolved = _resolve_target(ev.target, project_path)
        if resolved is not None and ev.target not in checked:
            checked.append(ev.target)

    if checked:
        shown = "、".join(checked[:5])
        more = f" 等 {len(checked)} 个" if len(checked) > 5 else ""
        return KnowledgeBoundary(
            grade="verified",
            label=GRADE_LABELS["verified"],
            basis=f"查过本机，可自己复核：{shown}{more}",
            checked=tuple(checked),
        )
    return KnowledgeBoundary(
        grade="inferred",
        label=GRADE_LABELS["inferred"],
        basis=(
            f"调了 {len(evidence)} 次工具，但没有一个目标落在"
            "本机能核的位置上（没带路径、用了通配模式，或路径不存在）"
        ),
    )


def _text_from_part(props: Any) -> str:
    """``message.part.updated`` 的正文（**整块**，不是增量）。"""
    if not isinstance(props, dict):
        return ""
    part = props.get("part")
    if not isinstance(part, dict):
        return ""
    text = part.get("text")
    return text if isinstance(text, str) else ""


def _delta_from_part(props: Any) -> str:
    """``message.part.delta`` 的**增量片段**。"""
    if not isinstance(props, dict):
        return ""
    delta = props.get("delta")
    return delta if isinstance(delta, str) else ""


def ask_executor(
    question: str,
    *,
    project: str | Path,
    make_server: Callable[..., Any],
    command: str = "opencode",
    model: str | None = None,
    max_tool_calls: int = 20,
    max_seconds: float = DEFAULT_ASK_TIMEOUT,
    now: Callable[[], float] = time.monotonic,
    context: str = "",
) -> AskOutcome:
    """问执行器一句，把答复拿回来。

    :param make_server: 起服务的工厂。**注入**是为了能测 ——
        真起一个 opencode 进程不是单该做的事（设计文档 11.8.1 同一个理由：
        「版本探测要能被测，就得先把解析和调用拆开」）。
    :param max_tool_calls: 轮次预算（11.10.4）。超了就**停下并报告**。
    :param max_seconds: 时长上限。用 ``time.monotonic`` —— 墙钟会被 NTP
        或用户改时间影响，于是「已经跑了 3 分钟」可能突然变成负数。
    :param context: 拼好的一段 core 记忆（V1.97，3.7.6 注入点①）。
        非空时拼在问题**前面**；空（或全空白）时问题原样发出。
        本模块只拼不产 —— 定性与预算口径由调用方的 ``core_block`` 负责。
    """
    question = (question or "").strip()
    if not question:
        return AskOutcome(ok=False, reason="没问什么。")

    from .opencode_server import read_only_permission_config

    # 记忆块拼在问题前面。块首尾的空白去掉 —— 留着一对换行只会让
    # 「没带记忆」看起来像出了错（见模块 docstring 的边界说明）。
    block = (context or "").strip()
    prompt = f"{block}\n\n{question}" if block else question

    deadline = now() + max_seconds
    text = ""
    tool_calls = 0
    #: 认得出来的工具调用（V1.104）。``tool_calls`` 是**计数**（预算用），
    #: 这个是**凭据**（边界用）—— 两者刻意分开：预算不该因为认不出形状而
    #: 少算一次调用（那会放开轮次上限），边界也不该因为认不出形状而假装
    #: 它查过（``tool_evidence`` 返回 ``None`` 时不进这里）。
    evidence: list[ToolEvidence] = []

    try:
        with make_server(
            Path(project), command,
            permission_config=read_only_permission_config(),
        ) as oc:
            session_id = oc.create_session()
            oc.prompt_async(session_id, prompt, model=model)

            for kind, props in oc.events():
                if now() > deadline:
                    _abort_quietly(oc, session_id)
                    return AskOutcome(
                        ok=False,
                        reason=f"超过单次提问的时长上限（{max_seconds:g}s）—— 已中止。",
                        tool_calls=tool_calls,
                        partial=text,
                        notes=["它可能已经读了一堆文件；重试前先看上面这段。"],
                        evidence=tuple(evidence),
                    )

                if kind == "message.part.delta":
                    text += _delta_from_part(props)
                    continue
                if kind == "message.part.updated":
                    # ⚠️ **覆盖**，不是累加 —— 见模块 docstring。
                    chunk = _text_from_part(props)
                    if chunk:
                        text = chunk
                    continue

                if kind == "tool":
                    # 轮次预算数的是**工具调用**（11.10.4）。
                    # 事件名取自 11.8.1 的实测分布表，不解析载荷 ——
                    # 所以若哪天 opencode 改了这个名字，预算会**静默失效**。
                    # 这个局限写在模块 docstring 里，不藏。
                    tool_calls += 1
                    found = tool_evidence(kind, props)
                    if found is not None:
                        evidence.append(found)
                    if max_tool_calls > 0 and tool_calls > max_tool_calls:
                        # ⚠️ **不静默截断**：说清「用尽预算、停在哪」。
                        # 半途停下而不说，用户会以为它跑完了。
                        _abort_quietly(oc, session_id)
                        return AskOutcome(
                            ok=False,
                            reason=(
                                f"用尽预算（{max_tool_calls} 次工具调用）—— "
                                "已停下，**没有**接着跑。"
                            ),
                            tool_calls=tool_calls,
                            partial=text,
                            notes=["它还没答完；上面是已拿到的半截。"],
                            evidence=tuple(evidence),
                        )
                    continue

                if kind == "permission.asked":
                    # ⚠️ 只读配置下**不该有任何授权请求**。出现它只有两种可能：
                    # 配置没生效（deny 落空）或执行器问了配置管不到的动作。
                    # 两种都**不能自动允许** —— 自动允许就把上面那道防线
                    # 整个绕过去了，而症状只是「看起来能用」。
                    _abort_quietly(oc, session_id)
                    return AskOutcome(
                        ok=False,
                        reason=(
                            "执行器想要一个「只读提问」用不上的权限，我已中止。"
                            "这不是你的错 —— 说明只读配置没生效，"
                            "别急着放宽它。"
                        ),
                        tool_calls=tool_calls,
                        partial=text,
                        evidence=tuple(evidence),
                    )

                if kind == "session.error":
                    _abort_quietly(oc, session_id)
                    # 报出执行器给的**真话**（若有）。opencode 的载荷里没有
                    # ``message`` 键，所以这里默认仍是笼统的「执行器报错」；
                    # hermes 的 ``run.failed`` 带 ``error``（如模型到期/余额
                    # 不足），被 hermes 适配器折进 ``{"message": ...}`` ——
                    # 有才带上，别让用户对着「执行器报错」四个字猜。
                    reason = "执行器报错。"
                    if isinstance(props, dict):
                        msg = props.get("message")
                        if isinstance(msg, str) and msg.strip():
                            reason = f"执行器报错：{msg.strip()[:200]}"
                    return AskOutcome(
                        ok=False, reason=reason,
                        tool_calls=tool_calls,
                        partial=text,
                        evidence=tuple(evidence),
                    )

                if kind == "session.idle":
                    # 权威的「这轮结束」信号（11.8.1 实测）。**不在这里计数** ——
                    # 「一轮结束」不是「一次工具调用」，早先把它当计数点，
                    # 于是预算实际变成了「最多跑几轮」，而那与 11.10.4 写的
                    # 「工具调用次数」不是一回事。
                    #
                    # 这个 ``break`` 同时跳过下面的 ``for...else`` ——
                    # 「看到了结束信号」与「流自己断了」必须能分开。
                    break
            else:
                # ⚠️ **流结束而没等到 ``session.idle``** —— 那不是「答完了」。
                # 服务崩了 / 连接断了 / 输出流被截断，都会走到这里。
                #
                # 原先直接落到下面那句 ``ok=True``，于是用户问了一句话、
                # 拿到一段半截甚至全空的答复、屏幕上还写着「成功」——
                # 这正是本项目最恨的**静默劣化**（对照 12.1.2「禁止静默劣化」
                # 与 9.2 那条「过期只在写入时判一次」的同源纪律）。
                #
                # 刻意**连半截都保留**：用户宁可看到「没答完 + 已拿到的部分」，
                # 也不要一个看起来正常的空答案。
                return AskOutcome(
                    ok=False,
                    reason=(
                        "执行器的输出流结束了，但没答完 —— "
                        "可能是它崩了或连接断了。下面是已拿到的部分。"
                    ),
                    tool_calls=tool_calls,
                    partial=text,
                    evidence=tuple(evidence),
                )
    except Exception as exc:  # noqa: BLE001 - 提问失败不该掀翻终端
        return AskOutcome(
            ok=False,
            reason=f"起执行器失败：{exc!r}",
            tool_calls=tool_calls,
            partial=text,
            evidence=tuple(evidence),
        )

    return AskOutcome(
        ok=True, text=text.strip(), tool_calls=tool_calls,
        evidence=tuple(evidence),
    )


def _abort_quietly(oc: Any, session_id: str) -> None:
    """中止会话。**中止失败不许改变结论**。

    会话随后会被 ``with`` 关掉，而我们要报的是「超预算停下」，
    不是「中止失败」—— 反过来会把一次**成功的保护**说成故障。

    ``session_id`` 由调用方传进来而**不从 oc 上取**：那份 Repl 侧的状态
    本模块并不拥有，而 ``getattr(oc, "_session_id", None)`` 那种兜底会在
    取不到时**静默地什么都不中止** —— 于是「已中止」变成一句谎话。
    """
    try:
        oc.abort(session_id)
    except Exception:  # noqa: BLE001 - 中止是尽力而为，不改变结论
        pass