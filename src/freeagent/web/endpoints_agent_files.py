"""``GET /api/agent/files`` —— 这个工作区里有什么（只读、一层、不读内容）。

## 与 ``/api/agent/dirs`` 的分工

``dirs`` 是**目录选择器**：只列目录、任何盘符都能往下点，回答「该选哪个
文件夹」。这里回答「这个工作区里本来有哪些文件」—— 连文件一起列，因此
可读范围从「目录名」推到「文件名」，根也就必须收紧。

## 三条约束

1. **根只能是一个工作区目录**：客户端传 ``project``（工作区档案 id），根由
   服务端从档案里取；没传时退回偏好里选的那个目录。**不接受任意绝对
   路径** —— 那等于把整个磁盘的文件名清单开成一个 GET。
2. **一层、不递归、有上限**：不往下翻（下钻由前端把相对路径再送一次），
   超过上限如实标 ``truncated``，跳过的重解析点也如实计数。
3. **一个字节都不读**：只 ``scandir`` 拿名字、``stat`` 拿大小。预览文件
   内容属于另一件事（编码判断、超大文件、二进制），这一版不做。

失败一律 fail-closed：路径解不进去就报「不在工作区目录内」，目录读不出
就报读不出，**不退回空列表** —— 「列不出来」写成「这里什么都没有」是把
故障当结果（12.7.2 那条纪律的镜像）。
"""

from __future__ import annotations

import os
import pathlib
import sys
from typing import Any

from ..app import App
from ..domain import FreeAgentError
from . import endpoints_agent_workspace as workspace

__all__ = ["agent_files"]

#: 一层最多返回多少项。与目录选择器同一档量级：``node_modules`` 这种目录
#: 一次 GET 就能拖成几秒，而人只需要知道「这里大概有什么」。
_MAX_FILES = 300


def _root(app: App, project_id: str) -> str:
    """工作区目录：档案里那一项优先，没传 id 时退回偏好里的目录。"""
    if project_id:
        repo = workspace._project_repo(app)
        if repo is None:
            raise FreeAgentError(
                f"读不到工作区档案（{project_id}）—— 这一版的项目列表还没接上仓储"
            )
        for row in repo.list_all():
            if str(row.get("id")) == project_id:
                directory = str(row.get("directory") or "")
                if directory:
                    return directory
                raise FreeAgentError(
                    f"这个工作区没有记目录（{row.get('name') or project_id}）—— "
                    "它建档时目录就是空的，重新选一个文件夹再建档一次"
                )
        raise FreeAgentError(f"没有这个工作区：{project_id} —— 列表可能已经变了，刷新一次")
    return str(workspace.load_prefs(app).get("directory") or "")


def _inside(root: str, rel: str) -> str:
    """根内相对路径 → 绝对路径。绝对路径与越界一律拒绝。"""
    if not rel:
        return root
    candidate = pathlib.Path(rel)
    if candidate.is_absolute() or (sys.platform == "win32" and candidate.drive):
        raise FreeAgentError(f"这里只收工作区内的相对路径，不收绝对路径：{rel}")
    try:
        resolved = os.path.realpath(str(pathlib.Path(root) / candidate))
    except OSError as exc:
        raise FreeAgentError(f"路径解析失败：{rel}（{exc}）") from exc
    try:
        pathlib.Path(resolved).relative_to(root)
    except ValueError:
        raise FreeAgentError(f"这个路径不在工作区目录内：{rel}") from None
    return resolved


def _rel(root: str, path: str) -> str:
    """绝对路径 → 相对 root 的 posix 串（下钻时原样送回 ``path``）。"""
    return pathlib.Path(path).relative_to(root).as_posix()


def agent_files(app: App, q: dict[str, list[str]]) -> dict[str, Any]:
    """列一个工作区目录下的一层条目（目录 + 文件），只读。"""
    project_id = (q.get("project") or [""])[0].strip()
    raw_rel = (q.get("path") or [""])[0].strip()
    root = _root(app, project_id)
    if not root:
        # 没选工作区不是故障，是「还没开始」—— 但它同样不能显示成一个空列表。
        return {
            "kind": "agent_files",
            "state": "no_dir",
            "reason": "还没选工作区 —— 选中之后这里会列出它目录里有什么。",
        }
    try:
        root_abs = os.path.realpath(root)
    except OSError as exc:
        raise FreeAgentError(f"工作区目录解析失败：{root}（{exc}）") from exc
    target = _inside(root_abs, raw_rel)
    if not os.path.isdir(target):
        return {
            "kind": "agent_files",
            "state": "gone",
            "root": root_abs,
            "path": raw_rel,
            "reason": f"这个目录现在读不到：{raw_rel or root_abs} —— "
                      "可能被删了、改名了，或者挪到了别的盘",
        }

    entries: list[dict[str, Any]] = []
    skipped = 0
    total = 0
    truncated = False
    try:
        with os.scandir(target) as it:
            for entry in it:
                path = pathlib.Path(entry.path)
                if workspace._is_reparse(path):
                    # junction / 符号链接不列：它会把工作区边界引到别处去
                    # （与目录选择器同一条判据，只此一份，见 workspace）。
                    skipped += 1
                    continue
                total += 1
                if len(entries) >= _MAX_FILES:
                    truncated = True
                    continue
                is_dir = entry.is_dir(follow_symlinks=False)
                try:
                    size = None if is_dir else int(
                        entry.stat(follow_symlinks=False).st_size)
                except OSError:
                    # 单个文件读不到属性，只说这一项的大小读不到 —— 不假装是 0。
                    size = None
                entries.append({
                    "name": entry.name,
                    "dir": is_dir,
                    "size": size,
                    "rel": _rel(root_abs, entry.path),
                    "abs": str(path),
                })
    except OSError as exc:
        raise FreeAgentError(f"读不出这个目录：{target}（{exc}）") from exc

    # 目录在前、点开头的在后、组内大小写不敏感 —— 与目录选择器同一套排法。
    entries.sort(key=lambda e: (not e["dir"], e["name"].startswith("."),
                                e["name"].lower()))
    rel = "" if target == root_abs else _rel(root_abs, target)
    parent = pathlib.PurePosixPath(rel).parent.as_posix() if rel else ""
    if parent == ".":
        parent = ""
    return {
        "kind": "agent_files",
        "state": "ok",
        "root": root_abs,
        "path": rel,
        "parent": parent,
        "entries": entries,
        "total": total,
        "truncated": truncated,
        "skipped_links": skipped,
    }
