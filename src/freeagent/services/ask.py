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
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

__all__ = ["AskOutcome", "ask_executor", "DEFAULT_ASK_TIMEOUT"]

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
) -> AskOutcome:
    """问执行器一句，把答复拿回来。

    :param make_server: 起服务的工厂。**注入**是为了能测 ——
        真起一个 opencode 进程不是单该做的事（设计文档 11.8.1 同一个理由：
        「版本探测要能被测，就得先把解析和调用拆开」）。
    :param max_tool_calls: 轮次预算（11.10.4）。超了就**停下并报告**。
    :param max_seconds: 时长上限。用 ``time.monotonic`` —— 墙钟会被 NTP
        或用户改时间影响，于是「已经跑了 3 分钟」可能突然变成负数。
    """
    question = (question or "").strip()
    if not question:
        return AskOutcome(ok=False, reason="没问什么。")

    from .opencode_server import read_only_permission_config

    deadline = now() + max_seconds
    text = ""
    tool_calls = 0

    try:
        with make_server(
            Path(project), command,
            permission_config=read_only_permission_config(),
        ) as oc:
            session_id = oc.create_session()
            oc.prompt_async(session_id, question, model=model)

            for kind, props in oc.events():
                if now() > deadline:
                    _abort_quietly(oc, session_id)
                    return AskOutcome(
                        ok=False,
                        reason=f"超过单次提问的时长上限（{max_seconds:g}s）—— 已中止。",
                        tool_calls=tool_calls,
                        partial=text,
                        notes=["它可能已经读了一堆文件；重试前先看上面这段。"],
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
                    )

                if kind == "session.error":
                    _abort_quietly(oc, session_id)
                    return AskOutcome(
                        ok=False, reason="执行器报错。",
                        tool_calls=tool_calls,
                        partial=text,
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
                )
    except Exception as exc:  # noqa: BLE001 - 提问失败不该掀翻终端
        return AskOutcome(
            ok=False,
            reason=f"起执行器失败：{exc!r}",
            tool_calls=tool_calls,
            partial=text,
        )

    return AskOutcome(ok=True, text=text.strip(), tool_calls=tool_calls)


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