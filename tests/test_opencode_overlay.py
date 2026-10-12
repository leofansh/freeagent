"""项目级权限覆盖层的守卫（设计文档 11.8.1 的 V1.31 落地）。

## 为什么这条不能省

V1.31 把「审批闸门」从「另起隔离世界 + 复制用户配置」改成「系统 opencode +
项目级 ``opencode.jsonc`` 覆盖层（只写 ``permission`` 一项）」。覆盖层是
**唯一**新增的文件，且 ``close()`` 会原样还原 —— 出错的代价是「闸门没生效」
或「污染了用户项目目录」。

这里锁两条死纪律：

1. **覆盖层不得双层嵌套 ``permission``**。``effective_config()`` 返回的是
   **完整配置**（含 ``$schema`` 与 ``permission`` 两个键），给隔离实例当整份
   配置用；而覆盖层只要压 ``permission`` 一项。早期版本写成
   ``{"permission": effective_config()}``，结果是
   ``{"permission": {"$schema": …, "permission": {…}}}`` —— opencode 把这份
   项目配置判为非法、**整份丢弃并回退内置默认**，于是 7 个插件变 0、权限变
   ``{}``。这条断言专门防它复发。
2. **``close()`` 必须能收口**，无论写没写过覆盖层。``_overlay_written`` 必须在
   ``__init__`` 就初始化，否则无闸门路径（``discovery()``）``close()`` 会
   ``AttributeError`` 崩溃。
"""

from __future__ import annotations

import json
import pathlib
import tempfile

from freeagent.services.opencode_server import OpenCodeServer


def _tmp_project() -> pathlib.Path:
    return pathlib.Path(tempfile.mkdtemp(prefix="ocov_"))


def test_overlay_is_single_level_permission():
    """覆盖层只能是 ``{"permission": {<规则>}}``，绝不可把整份配置再套一层。"""
    proj = _tmp_project()
    srv = OpenCodeServer(project=proj, isolate=False, apply_gate=True)
    try:
        srv._write_overlay()
        obj = json.loads(srv._overlay_path.read_text(encoding="utf-8"))

        # 顶层只有 permission 一个键
        assert set(obj.keys()) == {"permission"}, obj
        perm = obj["permission"]
        # 内层是规则映射，没有混进 $schema，也没有再套一层 permission
        assert isinstance(perm, dict)
        assert "$schema" not in perm
        assert "permission" not in perm
        # 闸门的关键规则确实在
        for key in ("edit", "task", "external_directory", "webfetch", "bash"):
            assert key in perm, f"缺规则 {key}"
    finally:
        srv.close()


def test_overlay_restores_existing_config():
    """项目原本有 opencode.jsonc：覆盖层先备份、close 后原样还原。"""
    proj = _tmp_project()
    original = proj / "opencode.jsonc"
    original.write_text(
        json.dumps({"plugin": ["x"], "permission": {"edit": "allow"}},
                   ensure_ascii=False),
        encoding="utf-8",
    )
    srv = OpenCodeServer(project=proj, isolate=False, apply_gate=True)
    try:
        srv._write_overlay()
        assert json.loads(srv._overlay_path.read_text())["permission"]
    finally:
        srv.close()
    # 原文件内容与覆盖前一致
    assert json.loads(original.read_text()) == {
        "plugin": ["x"],
        "permission": {"edit": "allow"},
    }


def test_overlay_removes_new_file_on_close():
    """项目原本没有配置文件：覆盖层是本次新建的，close 后必须删掉。"""
    proj = _tmp_project()
    srv = OpenCodeServer(project=proj, isolate=False, apply_gate=True)
    srv._write_overlay()
    path = srv._overlay_path
    assert path.exists()
    srv.close()
    assert not path.exists()


def test_close_without_gate_does_not_crash():
    """无闸门路径（如 discovery()）的 close() 不能 AttributeError。"""
    proj = _tmp_project()
    srv = OpenCodeServer(project=proj, isolate=False, apply_gate=False)
    # 不写覆盖层，直接收口 —— 曾经因 _overlay_written 未初始化而崩溃
    srv.close()
    assert srv._overlay_path is None
