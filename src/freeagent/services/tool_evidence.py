"""工具事件的**形状识别**：用了什么工具、指向哪里。

这个模块只有一个理由：同一个形状被两处需要，而两处需要的**用途**不同 ——

* 委派进度卡要一句人话「正在执行 read：D:/a.py」（``delegate.tool_event_label``）
* ``/ask`` 的知识边界要一份**可核的证据清单**（11.10.9 / 12.10.13）

形状识别写两份的后果是双向的：一份认得 V1 的 ``part.state.input``、另一份
只认 V2 的 ``state.input``，于是「进度卡显示得出来、边界判不出来」——
而这类不一致**不会报错**，只会让一边静默变差。

## 为什么认形状而不是认事件名

事件名随版本变（``tool.execute.before`` / ``message.part.updated`` /
``session.tool.called`` 都见过），而「载荷里有个 ``part.type == 'tool'``」
比事件名稳定。这与 ``tool_event_label`` 原来的判据是同一条，不是新发明。

## 认不出就返回 ``None``，绝不猜

宁可「这次没记到工具」也不要记错一个：知识边界说「查过本机」而实际没查，
比不说更坏（12.1.2 禁止静默劣化）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["ToolEvidence", "tool_evidence", "FILE_TOOLS"]


#: 会**碰到本机文件系统**的工具名（小写）。
#:
#: 只有这些的 ``target`` 才拿去核「在本机存不存在」。刻意排除两类：
#: * ``bash`` —— target 是一条命令，不是路径；拿命令去 ``exists()`` 必然假，
#:   于是「跑过命令」会被误判成「凭据落空」
#: * ``webfetch`` / ``task`` —— 一个根本不在本机，一个的 target 是子任务描述
#:
#: 名字是小写比较：opencode 的 V1/V2 都实测为小写，但没有契约保证。
FILE_TOOLS = frozenset({"read", "edit", "write", "list", "glob", "grep"})


@dataclass(frozen=True, slots=True)
class ToolEvidence:
    """一次工具调用里我们能认出来的部分。

    ``target`` 是**原文**：可能是绝对路径、相对路径、glob 模式或一条命令。
    本模块**不判断它是哪种** —— 那是调用方的事（``/ask`` 只对文件类工具
    做存在性检查，进度卡只把它原样显示出来）。
    """

    tool: str
    target: str = ""

    @property
    def is_file_tool(self) -> bool:
        return self.tool.strip().lower() in FILE_TOOLS


def _as_dict(value: Any) -> dict[str, Any]:
    """取出 dict，不是 dict 就当空的。

    与 ``delegate._as_dict`` 同一手法（收窄与取值在同一次调用里完成，
    避免类型检查器把 ``.get()`` 报成「可能为 None 的属性」）。刻意不跨模块
    复用它：那份是委派进程的内部小工具，这个模块不该为一个三行函数
    反向依赖执行层。
    """
    return value if isinstance(value, dict) else {}


def tool_evidence(kind: str, props: Any) -> ToolEvidence | None:
    """从一条事件里挖出 ``(工具名, 目标)``；不是工具事件就返回 ``None``。

    ``None`` 的三种情形都必须返回 ``None`` 而不是空证据：
    ``props`` 不是 dict / 挖不到工具名 / 形状不像工具事件。
    """
    if not isinstance(props, dict):
        return None
    part = _as_dict(props.get("part"))
    name = (props.get("tool") or props.get("toolName")
            or part.get("tool") or part.get("name") or "")
    if not isinstance(name, str) or not name:
        return None
    # 事件名里带 tool、或工具名就摆在顶层，才算工具事件。两者至少命中一个 ——
    # 否则一条 `message.part.updated`（纯文本）会被当成工具调用，
    # 而「记错」比「没记」更坏。
    looks_like_tool = (
        part.get("type") == "tool"
        or "tool" in kind.lower()
        or props.get("tool") is not None
        or props.get("toolName") is not None
    )
    if not looks_like_tool:
        return None
    # 参数在 ``part.state.input``（V1 实测）、V2 的 ``state.input`` 或
    # ``args`` 里。**三处都找** —— 找错了就丢掉目标，而目标正是
    # 「这次查过什么」的唯一凭据。
    state = _as_dict(part.get("state")) or _as_dict(props.get("state"))
    args = _as_dict(state.get("input")) or _as_dict(state.get("args"))
    arg = (args.get("filePath") or args.get("path")
           or args.get("pattern") or args.get("command") or "")
    return ToolEvidence(
        tool=name,
        target=arg if isinstance(arg, str) else "",
    )
