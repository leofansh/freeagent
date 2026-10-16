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

from freeagent.services.opencode_server import (
    OpenCodeServer,
    _OVERLAY_MARKER,
    stale_gate_note,
)


def _tmp_project() -> pathlib.Path:
    return pathlib.Path(tempfile.mkdtemp(prefix="ocov_"))


def _read_rules(path: pathlib.Path) -> dict:
    """读覆盖层里的**规则**。

    ⚠️ 必须先剥掉标记注释：覆盖层是 ``.jsonc``，顶部那几行 ``// …`` 是
    给人看的（opencode 读得动，因为它用 ``jsonc-parser``），而 ``json.loads``
    读不动。早期这里直接 ``json.loads``，加上标记后这个测试就红了 ——
    那是测试没跟上，不是实现坏了。
    """
    text = "\n".join(
        line for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("//")
    )
    return json.loads(text)["permission"]


def test_overlay_is_single_level_permission():
    """覆盖层只能是 ``{"permission": {<规则>}}``，绝不可把整份配置再套一层。"""
    proj = _tmp_project()
    srv = OpenCodeServer(project=proj, isolate=False, apply_gate=True)
    try:
        srv._write_overlay()
        perm = _read_rules(srv._overlay_path)
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
        assert _read_rules(srv._overlay_path)
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


# =============================================================================
# 残留（执行器被强杀 ⇒ 收尾没跑到）
#
# 实测踩到：``D:/PycharmProjects/xiaoyuan`` 里留着一份，是写残留时兜底还是
# ``*: allow`` 的那份。它是**项目级配置**，所以对该项目的任何 opencode 使用
# （含手动跑、Desktop）都生效 —— 用户多半不知道它在。
#
# 处置刻意**不做「启动时静默删掉」**（2026-10-14 与用户确认）：那份文件真的在
# 生效，静默删是替用户做了一个他没要求的决定；而「长得像闸门就当残留」这种
# 启发式还会在用户自己写了相似配置时**删掉他的文件**。所以：**只认我们的标记，
# 认出来先告知，清理由下一次正常收尾完成。**
# =============================================================================
class TestLeftoverOverlay:
    def test_overlay_carries_the_marker(self):
        """写进去的必须带标记 —— 否则残留永远认不出来。"""
        proj = _tmp_project()
        srv = OpenCodeServer(project=proj, isolate=False, apply_gate=True)
        try:
            srv._write_overlay()
            assert _OVERLAY_MARKER in srv._overlay_path.read_text(encoding="utf-8")
        finally:
            srv.close()

    def test_marker_explains_itself_to_the_reader(self):
        """标记要写成**给项目主人看的话**：这是什么、为什么残留、怎么办。"""
        proj = _tmp_project()
        srv = OpenCodeServer(project=proj, isolate=False, apply_gate=True)
        try:
            srv._write_overlay()
            head = srv._overlay_path.read_text(encoding="utf-8")[:400]
        finally:
            srv.close()
        assert head.lstrip().startswith("//"), "标记必须是文件开头的注释"
        assert "删掉" in head, "得告诉人怎么办"

    def test_leftover_is_reported(self):
        """认出来了就要**说**，且说清路径。"""
        proj = _tmp_project()
        srv = OpenCodeServer(project=proj, isolate=False, apply_gate=True)
        srv._write_overlay()
        path = srv._overlay_path
        # 模拟「收尾没跑到」：文件留在原地
        note = stale_gate_note(proj)
        assert note is not None
        assert str(path) in note, "要说清是哪个文件"
        assert "删掉" in note
        srv._restore_overlay()

    def test_leftover_is_deleted_not_restored(self):
        """残留那份收尾时**删掉**，不是「还原」。

        还原等于把那份失效方向的旧闸门再放回去 —— 而它本来就该消失。

        残留怎么造：**不调 :meth:`close`**。真实场景就是执行器被强杀 /
        机器重启，收尾根本没跑到（``close()`` 是唯一会删掉它的路径）。
        """
        proj = _tmp_project()
        killed = OpenCodeServer(project=proj, isolate=False, apply_gate=True)
        killed._write_overlay()          # 刻意不 close —— 模拟被强杀
        path = killed._overlay_path
        assert path.exists(), "被强杀 ⇒ 文件留在原地"
        assert killed._overlay_was_our_leftover is False   # 第一次：项目是干净的

        # 下一次委派：认成残留
        srv2 = OpenCodeServer(project=proj, isolate=False, apply_gate=True)
        srv2._write_overlay()
        assert srv2._overlay_was_our_leftover is True, "没认出上次残留"
        srv2.close()
        assert not path.exists(), "残留必须消失，而不是被还原回来"

    def test_users_own_config_is_never_flagged(self):
        """⚠️ **没有标记的一律不认** —— 宁可漏认，绝不误删用户的文件。"""
        proj = _tmp_project()
        (proj / "opencode.jsonc").write_text(
            json.dumps({"permission": {"*": "allow", "edit": "ask"}}),
            encoding="utf-8",
        )
        assert stale_gate_note(proj) is None, "用户自己的配置被误认成残留了"
        srv = OpenCodeServer(project=proj, isolate=False, apply_gate=True)
        try:
            srv._write_overlay()
            assert srv._overlay_was_our_leftover is False
        finally:
            srv.close()
        # 收尾后原样还原（这是用户自己的文件）
        assert json.loads((proj / "opencode.jsonc").read_text(encoding="utf-8")) == {
            "permission": {"*": "allow", "edit": "ask"},
        }

    def test_plain_json_gets_no_comment(self):
        """``.json`` 是严格 JSON，**绝不能**写注释进去。

        写进去的后果是整份配置作废 ⇒ **闸门静默失效** —— 那是最坏的一种坏法。
        """
        proj = _tmp_project()
        (proj / "opencode.json").write_text("{}", encoding="utf-8")
        srv = OpenCodeServer(project=proj, isolate=False, apply_gate=True)
        try:
            srv._write_overlay()
            raw = srv._overlay_path.read_text(encoding="utf-8")
            assert "//" not in raw
            json.loads(raw)   # 仍是合法 JSON
        finally:
            srv.close()

    def test_clean_project_reports_nothing(self):
        """没有残留就**闭嘴** —— 每次委派都报一遍「你也许有残留」是噪声。"""
        assert stale_gate_note(_tmp_project()) is None
