"""可授权根浏览的**服务层**测试（设计文档 12.7.1.1 约束 1–4）。

HTTP 层只做参数搬运，安全边界全在这一层，所以这里逐条验拒绝方向：
只列目录、不列符号链接、越根就拒、绝对路径当非法、索引判不清就拒。
这些用例的价值在「挡住」——成功路径只留两三条确认没写死。
"""

import os
import pathlib

import pytest

from freeagent.domain import ValidationError
from freeagent.services.delegate_browse import (
    enabled,
    list_dirs,
    pick_from_body,
    pick_path,
)


def _tree(root: pathlib.Path) -> list[str]:
    """a/b、c、一个文件 —— 够验「只列目录」与「只列直接子级」。"""
    (root / "a" / "b").mkdir(parents=True)
    (root / "c").mkdir()
    (root / "note.txt").write_text("x", encoding="utf-8")
    return [str(root)]


def _symlink(src: pathlib.Path, dst: pathlib.Path) -> None:
    try:
        os.symlink(src, dst, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("这个环境建不了符号链接（Windows 需开发者模式/管理员）")
    if not dst.is_symlink():
        # 有些沙箱把 os.symlink 实现成「调用成功但落地成普通目录」：
        # 不抛错，可 is_symlink() 永远是 False，readlink 报「不是重
        # 分析点」。那种环境里这条用例测的是「真实目录不该被列出」，
        # 与用例本意（链接不该出现）无关 —— 跳过，真机上照常跑。
        pytest.skip("这个环境的 os.symlink 落地的不是真链接（沙箱假件）")


# --------------------------------------------------------------------------- #
# 开关
# --------------------------------------------------------------------------- #
def test_disabled_by_default(tmp_path):
    """空 = 关。默认朝关是规范原文，不是实现偷懒（12.7.1.1）。"""
    assert enabled(()) is False
    assert enabled([str(tmp_path)]) is True


# --------------------------------------------------------------------------- #
# 列目录：约束 1（只列目录）+ 约束 4（不列符号链接）
# --------------------------------------------------------------------------- #
def test_lists_only_directories(tmp_path):
    roots = _tree(tmp_path)
    assert list_dirs(roots, 0, "") == ["a", "c"], "文件或深层目录漏进来了"


def test_lists_nested_relative_path(tmp_path):
    roots = _tree(tmp_path)
    assert list_dirs(roots, 0, "a") == ["b"]


def test_skips_symlinks(tmp_path):
    """链接可能指向根外：列出来用户一点就会被拒，文案还答非所问。"""
    roots = _tree(tmp_path)
    _symlink(tmp_path / "a" / "b", tmp_path / "link-in")
    assert "link-in" not in list_dirs(roots, 0, "")
    with pytest.raises(ValidationError):
        pick_path(roots, 0, "link-in")


def test_unreadable_subdir_is_relative_error(tmp_path):
    with pytest.raises(ValidationError, match="不在可授权根内"):
        list_dirs(_tree(tmp_path), 0, "../nope")


# --------------------------------------------------------------------------- #
# 约束 2：绝对路径当非法；约束 3：.. 逃逸被 resolve+relative_to 挡住
# --------------------------------------------------------------------------- #
def test_rejects_absolute_path(tmp_path):
    roots = _tree(tmp_path)
    with pytest.raises(ValidationError, match="绝对路径"):
        list_dirs(roots, 0, str(tmp_path / "a"))


def test_rejects_windows_drive_path(tmp_path):
    """``C:\\x`` 在 POSIX 的 Path 里不算绝对路径 —— 也要挡。"""
    with pytest.raises(ValidationError, match="绝对路径"):
        list_dirs(_tree(tmp_path), 0, "C:\\evil")


def test_rejects_parent_escape(tmp_path):
    roots = _tree(tmp_path)
    with pytest.raises(ValidationError, match="不在可授权根内"):
        list_dirs(roots, 0, "..")
    with pytest.raises(ValidationError, match="不在可授权根内"):
        pick_path(roots, 0, "a/../../outside")


# --------------------------------------------------------------------------- #
# 索引/根校验：判不清就拒，不退到兜底
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("index", [1, -1, "0", True, None])
def test_rejects_bad_index(tmp_path, index):
    roots = _tree(tmp_path)
    with pytest.raises(ValidationError):
        list_dirs(roots, index, "")


def test_rejects_missing_root(tmp_path):
    with pytest.raises(ValidationError, match="不存在或不是目录"):
        list_dirs([str(tmp_path / "ghost")], 0, "")


# --------------------------------------------------------------------------- #
# 点选 → 规范绝对路径
# --------------------------------------------------------------------------- #
def test_pick_returns_canonical_absolute_path(tmp_path):
    roots = _tree(tmp_path)
    got = pick_path(roots, 0, "a")
    assert pathlib.Path(got) == (tmp_path / "a").resolve()
    assert pathlib.Path(got).is_absolute()


def test_pick_root_itself_is_allowed(tmp_path):
    """根本身也是「范围内的目录」——圈范围时本来就含它。"""
    roots = _tree(tmp_path)
    assert pathlib.Path(pick_path(roots, 0, "")) == tmp_path.resolve()


def test_pick_rejects_missing_dir(tmp_path):
    with pytest.raises(ValidationError, match="不是目录或已不存在"):
        pick_path(_tree(tmp_path), 0, "nope")


def test_pick_rejects_file(tmp_path):
    with pytest.raises(ValidationError, match="不是目录或已不存在"):
        pick_path(_tree(tmp_path), 0, "note.txt")


# --------------------------------------------------------------------------- #
# POST 载荷入口：形状校验与路径校验走同一道边界
# --------------------------------------------------------------------------- #
def test_pick_from_body_happy_path(tmp_path):
    roots = _tree(tmp_path)
    got = pick_from_body(roots, {"root": 0, "path": "a"})
    assert pathlib.Path(got) == (tmp_path / "a").resolve()


def test_pick_from_body_defaults_root_zero(tmp_path):
    roots = _tree(tmp_path)
    got = pick_from_body(roots, {"path": "c"})
    assert pathlib.Path(got) == (tmp_path / "c").resolve()


@pytest.mark.parametrize("body", ["a", 1, None, {"root": 0, "path": "x", "junk": 1}])
def test_pick_from_body_rejects_bad_shape(tmp_path, body):
    with pytest.raises(ValidationError):
        pick_from_body(_tree(tmp_path), body)


def test_pick_from_body_requires_roots(tmp_path):
    with pytest.raises(ValidationError, match="未配置"):
        pick_from_body((), {"root": 0, "path": "a"})


def test_pick_from_body_rejects_non_string_path(tmp_path):
    with pytest.raises(ValidationError, match="字符串"):
        pick_from_body(_tree(tmp_path), {"root": 0, "path": {"x": 1}})
