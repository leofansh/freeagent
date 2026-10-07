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
    reset_cache,
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


# --------------------------------------------------------------------------- #
# 结果缓存
#
# 为什么这组测试重要：``GET /project`` 实测**每次 5.7 秒**（要起一个
# opencode 进程）。而这条路径在**每条飞书消息**上都会被走到 —— 不缓存的话
# 发一句话要等 6 秒才有回音。第一版没缓存，全量测试也因此超时 30 分钟。
# --------------------------------------------------------------------------- #

class _FakeDiscovery:
    """假 discovery 客户端。数着被开了几次。"""

    calls = 0

    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        type(self).calls += 1
        return self

    def __exit__(self, *exc):
        return False

    def list_projects(self):
        return self.rows


@pytest.fixture
def counted(monkeypatch):
    """装上假 discovery，并在每个测试前后清缓存。"""
    reset_cache()
    _FakeDiscovery.calls = 0
    monkeypatch.setattr(
        "freeagent.services.opencode_server.OpenCodeServer.discovery",
        classmethod(lambda cls, **kw: _FakeDiscovery(RAW_ROWS)),
    )
    yield
    reset_cache()


RAW_ROWS = [
    {"worktree": "D:/PycharmProjects/freeagent", "name": "FreeAgent"},
    {"worktree": "D:/PycharmProjects/openmos", "name": "OpenMOS"},
]


def test_second_call_is_served_from_cache(counted):
    assert len(list_projects()) == 2
    assert len(list_projects()) == 2
    assert _FakeDiscovery.calls == 1, "第二次不该再起 opencode"


def test_reset_cache_forces_a_requery(counted):
    list_projects()
    reset_cache()
    list_projects()
    assert _FakeDiscovery.calls == 2, "reset_cache 之后必须真查"


def test_runner_path_never_caches(counted):
    """测试路径每次都要真跑 —— 缓存会让「同参数两次调用」看起来像幂等。"""
    list_projects(runner=_runner(ROWS))
    list_projects(runner=_runner(ROWS))
    assert _FakeDiscovery.calls == 0


def test_failure_is_not_cached(counted):
    """一次偶发失败不该被记住 30 秒。

    症状是「刚才还好的，现在一直说没有项目」而查不出原因 ——
    因为真实原因（那次起不来）已经被缓存覆盖掉了。
    """
    def boom(**kw):
        raise RuntimeError("opencode 没起来")
    monkey = pytest.MonkeyPatch()
    monkey.setattr(
        "freeagent.services.opencode_server.OpenCodeServer.discovery",
        classmethod(lambda cls, **kw: boom()),
    )
    try:
        assert list_projects() == []
        assert list_projects() == []
        assert _FakeDiscovery.calls == 0
    finally:
        monkey.undo()


def test_cached_result_is_a_copy(counted):
    """返回的是**副本**：调用方改了它不该污染缓存。"""
    first = list_projects()
    first.clear()
    assert len(list_projects()) == 2