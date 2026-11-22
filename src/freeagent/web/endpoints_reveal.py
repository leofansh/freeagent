"""工作台「在系统文件管理器中定位」端点（V1.135，借鉴 WorkBuddy）。

## 为什么单独一个模块

这是一个**会在本机启动进程**的入口（Windows 资源管理器 / macOS Finder /
Linux 文件管理器）。`endpoints_agent` 已 230 行、`endpoints_agent_workspace`
已 238 行，都贴着 250 行上限；更重要的是「启动本地进程」这件事该和
「起一轮引擎」分开记，免得将来有人往同一张派发表里顺手加一个危险动作。

## 三条纪律

1. **只认授权目录内的路径**：`realpath` + `relative_to` 验根内（同
   `delegate_browse`）。字符串前缀比对会被 `C:\\allowed_evil` 这种同前缀
   不同目录的路径骗过。根目录取引擎当前目录（起引擎时已过 `resolve_workdir`
   校验），没起引擎时回退到偏好里那一个（同样是绝对路径）。
2. **不拼 shell**：`subprocess.Popen` 用参数数组、`shell=False`。路径来自
   引擎，拼进命令行字符串就是一次命令注入（分号 / 引号 / `&`）。
3. **失败说得清**：根没配、路径越界、系统不支持，都给下一步出路；绝不
   静默什么都不做（12.7.2「沉默 ≠ 同意」的镜像：沉默 ≠ 已定位）。
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
from typing import Any

from ..app import App
from ..domain import FreeAgentError
from . import endpoints_agent_workspace as workspace

__all__ = ["agent_reveal"]


def _root(app: App) -> str:
    """授权根：引擎当前目录优先，回退到偏好里选的目录。

    两者都是绝对路径且起引擎时过过 `resolve_workdir`，这里只再 `realpath`
    一次（解析符号链接、8.3 短名），保证与待定位路径在同一套归一化下比较。
    """
    engine = getattr(app, "engine", None)
    directory = ""
    if engine is not None:
        directory = getattr(engine.state(), "directory", "") or ""
    if not directory:
        directory = workspace.load_prefs(app).get("directory", "")
    if not directory:
        raise FreeAgentError("还没选目录，没法定 —— 请在工作台先选一个工作目录")
    try:
        return os.path.realpath(directory)
    except OSError as exc:
        raise FreeAgentError(f"目录解析失败：{directory}（{exc}）") from exc


def _inside(root: str, raw: object) -> str:
    """把客户端给的路径验在根内，返回绝对路径；判不清就 fail-closed。"""
    if not isinstance(raw, str) or not raw.strip():
        raise FreeAgentError("没说要定位哪个文件")
    candidate = pathlib.Path(raw.strip()).expanduser()
    if not candidate.is_absolute():
        candidate = pathlib.Path(root) / candidate
    try:
        resolved = os.path.realpath(str(candidate))
    except OSError as exc:
        raise FreeAgentError(f"路径解析失败：{raw}（{exc}）") from exc
    try:
        pathlib.Path(resolved).relative_to(root)
    except ValueError:
        raise FreeAgentError(
            f"这个路径不在当前工作目录内：{raw} —— 只能定位授权目录里的文件"
        ) from None
    return resolved


def agent_reveal(app: App, body: dict[str, Any]) -> dict[str, Any]:
    """``POST /api/agent/reveal`` —— 在系统文件管理器里选中该文件。"""
    root = _root(app)
    path = _inside(root, body.get("path", ""))
    if sys.platform == "win32":
        args = ["explorer", "/select," + path]
    elif sys.platform == "darwin":
        args = ["open", "-R", path]
    else:
        args = ["xdg-open", os.path.dirname(path)]
    try:
        # 参数数组 + shell=False：路径里有空格 / 引号 / 分号都不会被当命令解析。
        subprocess.Popen(args, shell=False)
    except OSError as exc:
        raise FreeAgentError(f"打不开文件管理器：{exc}（系统可能不支持定位）") from exc
    return {"kind": "agent_reveal", "path": path}
