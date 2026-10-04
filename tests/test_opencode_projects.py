"""``services.opencode_projects`` 的测试。

重点覆盖三件容易错的事：
1. ``worktree='/'``（Default Project）必须被过滤掉
2. 引用键必须**大小写不敏感**
3. ``name`` 为空退回目录名 basename
4. **任何异常都退化成空列表**，绝不抛——纯观测功能不该掀翻界面
"""

import pytest

from freeagent.services.opencode_projects import (
    OpenCodeProject,
    _basename,
    _parse,
    list_projects,
    resolve,
)

ROWS = """
[
  {"worktree": "/", "name": null},
  {"worktree": "D:/PycharmProjects/freeagent", "name": "FreeAgent"},
  {"worktree": "D:/PycharmProjects/openmos",   "name": "OpenMOS"},
  {"worktree": "D:/PycharmProjects/xiaoyuan",  "name": "XiaoYuan"}
]
"""


class _Proc:
    def __init__(self, out="", code=0):
        self.stdout = out
        self.stderr = ""
        self.returncode = code


def _runner(out="", code=0):
    def run(argv, **kw):
        # 断言 argv 是固定列表、不带 shell
        assert argv[0] == "opencode" and argv[1] == "db"
        assert "shell" not in kw or kw["shell"] is False
        return _Proc(out, code)
    return run


# --------------------------------------------------------------------------- #
# 过滤 Default Project
# --------------------------------------------------------------------------- #
def test_default_project_is_filtered():
    out = _parse(ROWS)
    assert [p.worktree for p in out] == [
        "D:/PycharmProjects/freeagent",
        "D:/PycharmProjects/openmos",
        "D:/PycharmProjects/xiaoyuan",
    ], "worktree='/' 是 Default Project，不是真实目录，必须过滤"


def test_root_alone_yields_nothing():
    assert _parse('[{"worktree": "/", "name": "x"}]') == []


# --------------------------------------------------------------------------- #
# 引用键
# --------------------------------------------------------------------------- #
def test_name_is_used_as_display():
    p = {x.display: x for x in _parse(ROWS)}["FreeAgent"]
    assert p.worktree == "D:/PycharmProjects/freeagent"
    assert p.key == "freeagent", "key 存 casefold 后的形式，供匹配用"


def test_missing_name_falls_back_to_basename():
    rows = '[{"worktree": "D:/PycharmProjects/xiaoyuan", "name": null}]'
    got = _parse(rows)
    assert len(got) == 1
    assert got[0].display == "xiaoyuan"
    assert got[0].key == "xiaoyuan"


def test_blank_name_falls_back_to_basename():
    rows = '[{"worktree": "D:/PycharmProjects/openmos", "name": "   "}]'
    assert _parse(rows)[0].display == "openmos"


@pytest.mark.parametrize("typed", ["freeagent", "FreeAgent", "FREEAGENT",
                                   "FreeAgent "])
def test_resolve_is_case_insensitive(typed):
    """三个项目的显示名与目录名大小写全不同，不敏感匹配是**必需**。"""
    got = resolve(typed, _parse(ROWS))
    assert got is not None, f"{typed!r} 应该能解析"
    assert got.worktree == "D:/PycharmProjects/freeagent"


def test_resolve_unknown_returns_none_not_guess():
    assert resolve("nope", _parse(ROWS)) is None


def test_resolve_empty_returns_none():
    assert resolve("   ", _parse(ROWS)) is None


def test_resolve_does_not_fall_back_to_path_guess():
    """拼错的名字**不能**被「当路径」解释——那等于一次意外授权。"""
    assert resolve("freeagent/src", _parse(ROWS)) is None


# --------------------------------------------------------------------------- #
# 路径 basename
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw,base", [
    ("D:/PycharmProjects/freeagent", "freeagent"),
    ("D:\\PycharmProjects\\freeagent", "freeagent"),
    ("D:/a/b/c/", "c"),
    ("freeagent", "freeagent"),
])
def test_basename_handles_both_separators(raw, base):
    assert _basename(raw) == base


# --------------------------------------------------------------------------- #
# 任何异常都退化
# --------------------------------------------------------------------------- #
def test_bad_json_is_empty_list():
    assert _parse("{ 半截") == []


def test_non_list_payload_is_empty_list():
    assert _parse('{"worktree": "/"}') == []


def test_rows_are_skipped_individually():
    """一行坏**不能**让整份清单丢失——global 那条 name 为空是正常状态。"""
    rows = '[{"worktree": "/", "name": null}, "垃圾", ' \
           '{"worktree": "D:/x/y", "name": "Y"}]'
    got = _parse(rows)
    assert [p.display for p in got] == ["Y"]


def test_missing_opencode_returns_empty_not_raise():
    def boom(argv, **kw):
        raise FileNotFoundError("opencode 没装")
    assert list_projects(runner=boom) == []


def test_timeout_returns_empty_not_raise():
    import subprocess as sp

    def slow(argv, **kw):
        raise sp.TimeoutExpired(argv, 10)
    assert list_projects(runner=slow) == []


def test_nonzero_exit_returns_empty():
    assert list_projects(runner=_runner("{}", 1)) == []


def test_happy_path_via_runner():
    got = list_projects(runner=_runner(ROWS))
    assert len(got) == 3
    assert {p.display for p in got} == {"FreeAgent", "OpenMOS", "XiaoYuan"}