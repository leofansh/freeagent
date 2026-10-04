"""授权端点 ``POST /api/delegate/projects`` 的测试。

重点守三件安全相关的事：
1. **授权必须显式** —— 列表里有项目 ≠ 已授权
2. **名字不认识要说清并列出可用**，而不是让用户去猜
3. 重复授权 / 撤销不存在项都要**报错**，不静默成功
"""

import json

import pytest

from freeagent.app import build_app
from freeagent.domain import FreeAgentError
from freeagent.web import endpoints_delegate_projects as mod

ROWS = json.dumps([
    {"worktree": "/", "name": None},                # Default Project
    {"worktree": "D:/PycharmProjects/freeagent", "name": "FreeAgent"},
    {"worktree": "D:/PycharmProjects/openmos", "name": "OpenMOS"},
    {"worktree": "D:/PycharmProjects/xiaoyuan", "name": "XiaoYuan"},
])


def _stub():
    from freeagent.services.opencode_projects import _parse
    return _parse(ROWS)


def _write_config(home, projects, model=""):
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(
        json.dumps({"delegate": {"projects": list(projects),
                                 "model": model}}),
        encoding="utf-8")


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "list_projects", _stub)
    _write_config(tmp_path, [])
    app = build_app(tmp_path / "a.db")
    yield app, tmp_path
    app.close()


def _norm(p: str) -> str:
    """按 ``load_config`` 的同一套规则归一化，用于**比较**。

    刻意不比字面量：写进文件的是反斜杠还是正斜杠取决于平台与读写顺序，
    断言那个字符串等于把实现细节钉死—— 而上一版就是这么把斜杠漂移放过去的。
    """
    import pathlib
    return str(pathlib.Path(p))


def _has(paths, want: str) -> bool:
    return any(_norm(p) == _norm(want) for p in paths)


def _save(app, body):
    return mod.delegate_projects_save(app, body)


def _cfg(app):
    return json.loads(
        (__import__("pathlib").Path(app.config.home) / "config.json")
        .read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# 授权
# --------------------------------------------------------------------------- #
def test_allow_adds_path(env):
    app, _ = env
    out = _save(app, {"allow": "OpenMOS"})
    assert _has(_cfg(app)["delegate"]["projects"],
                "D:/PycharmProjects/openmos")
    by = {i["name"]: i for i in out["items"]}
    assert by["OpenMOS"]["allowed"] is True
    assert by["FreeAgent"]["allowed"] is False, "只该授权被点的那一个"


def test_allow_is_case_insensitive(env):
    app, _ = env
    _save(app, {"allow": "openmos"})
    assert _has(_cfg(app)["delegate"]["projects"],
                "D:/PycharmProjects/openmos")


def test_listing_alone_does_not_authorise(env):
    """**列表里出现 ≠ 已授权** —— 这是本设计的安全前提。"""
    app, _ = env
    out = mod.delegate_projects(app)
    assert len(out["items"]) == 3, "三个项目都该列出来"
    assert all(i["allowed"] is False for i in out["items"])
    assert _cfg(app)["delegate"]["projects"] == []


def test_default_project_cannot_be_authorised(env):
    """/ 不是可委派的目标，名字再像也不行。"""
    app, _ = env
    with pytest.raises(FreeAgentError):
        _save(app, {"allow": "/"})


# --------------------------------------------------------------------------- #
# 报错：让人知道能填什么
# --------------------------------------------------------------------------- #
def test_unknown_name_lists_available(env):
    """名字不认识时**必须列出可用的** —— 只说「不认识」等于让用户猜。"""
    app, _ = env
    with pytest.raises(FreeAgentError) as ei:
        _save(app, {"allow": "nope"})
    msg = str(ei.value)
    for name in ("FreeAgent", "OpenMOS", "XiaoYuan"):
        assert name in msg, f"可用项目没列出来：{msg}"


def test_duplicate_allow_is_refused(env):
    """重复授权**报错**而不是静默成功 —— 静默成功会让人以为改了配置。"""
    app, _ = env
    _save(app, {"allow": "OpenMOS"})
    with pytest.raises(FreeAgentError):
        _save(app, {"allow": "OpenMOS"})
    assert len(_cfg(app)["delegate"]["projects"]) == 1


def test_revoke_removes(env):
    app, _ = env
    _save(app, {"allow": "OpenMOS"})
    out = _save(app, {"revoke": "OpenMOS"})
    assert not _has(_cfg(app)["delegate"]["projects"],
                    "D:/PycharmProjects/openmos")
    by = {i["name"]: i for i in out["items"]}
    assert by["OpenMOS"]["allowed"] is False


def test_revoke_unknown_is_refused(env):
    app, _ = env
    with pytest.raises(FreeAgentError):
        _save(app, {"revoke": "OpenMOS"})


def test_revoke_stale_entry_whose_project_is_gone(env, monkeypatch):
    """项目已从 OpenCode 删掉、但白名单里那条还留着 —— **必须还能撤**。

    这是最容易积灰的死配置：界面列不出它（项目没了），用户点不到撤，
    而它一直在授权列表里。不按 basename 兜底的话它就永远撤不掉。
    """
    app, tmp = env
    _write_config(tmp, ["D:/PycharmProjects/deleted-repo"])

    # 现在 OpenCode 里已经没有 deleted-repo 了
    monkeypatch.setattr(mod, "list_projects", lambda: [])

    out = _save(app, {"revoke": "deleted-repo"})
    assert _cfg(app)["delegate"]["projects"] == []
    assert out["items"] == []


def test_revoke_does_not_match_a_different_project(env):
    """兜底必须**精确**匹配 basename，不能把同前缀的兄弟一起删掉。

    这条第一版写错了：断言「撤销 openmos-fork 会报错」，而正确行为恰恰是
    **应该删掉它** —— 报错才是 bug（用户明明授权了却撤不掉）。
    真正要守的性质是「只删这一个、``openmos`` 留下」。
    """
    app, tmp = env
    _write_config(tmp, ["D:/PycharmProjects/openmos",
                        "D:/PycharmProjects/openmos-fork"])

    out = _save(app, {"revoke": "openmos-fork"})

    left = _cfg(app)["delegate"]["projects"]
    assert len(left) == 1 and _norm(left[0]) == _norm(
        "D:/PycharmProjects/openmos"), f"只该删掉被点的那一个，却剩 {left}"
    by = {i["name"]: i["allowed"] for i in out["items"]}
    assert by["OpenMOS"] is True, "兄弟项目不该被顺手删掉"


# --------------------------------------------------------------------------- #
# 参数校验
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("body", [
    {},                                        # 空
    {"allow": "x", "revoke": "y"},             # 同时给两个
    {"nope": "x"},                             # 不认识的字段
    {"allow": 123},                            # 类型错
    {"revoke": []},                            # 类型错
])
def test_bad_requests_are_refused(env, body):
    app, _ = env
    with pytest.raises(FreeAgentError):
        _save(app, body)


def test_refused_request_writes_nothing(env):
    """被拒的请求**不许留下半截配置**。"""
    app, tmp = env
    with pytest.raises(FreeAgentError):
        _save(app, {"allow": "nope"})
    assert _cfg(app)["delegate"]["projects"] == []


# --------------------------------------------------------------------------- #
# 不破坏其他配置项
# --------------------------------------------------------------------------- #
def test_other_settings_survive(env):
    app, tmp = env
    _write_config(tmp, [], model="opencode/big-pickle")
    _save(app, {"allow": "OpenMOS"})
    assert _cfg(app)["delegate"]["model"] == "opencode/big-pickle", (
        "改白名单顺手把 model 抹掉了"
    )


# --------------------------------------------------------------------------- #
# 幂等：连续保存不产生无意义 diff
# --------------------------------------------------------------------------- #
def test_saved_paths_use_one_separator_style(env):
    """写入的路径必须与 ``load_config`` 读出来的一致（斜杠方向统一）。

    踩过的坑：``load_config`` 在 Windows 上经 ``str(Path(...))`` 把 ``/``
    转成 ``\\``，而从 OpenCode 新加的那条还是原样 —— 于是两次授权之后文件里
    就成了「一条反斜杠、一条正斜杠」。功能无害，但它是要被人手改和 review 的
    文件，每授权一次就全体换一次方向，纯噪音。
    """
    app, _ = env
    _save(app, {"allow": "OpenMOS"})
    _save(app, {"allow": "XiaoYuan"})
    paths = _cfg(app)["delegate"]["projects"]
    assert len({p.count("/") for p in paths}) == 1 or \
        len({("\\" in p) for p in paths}) == 1, (
        f"斜杠方向不一致：{paths}"
    )


def test_repeated_writes_are_idempotent(env):
    """读→写是幂等的：再保存一次，文件不该变。"""
    app, _ = env
    _save(app, {"allow": "OpenMOS"})
    first = _cfg(app)["delegate"]["projects"]
    _save(app, {"allow": "XiaoYuan"})
    second = _cfg(app)["delegate"]["projects"]
    # 第一条不许被第二次保存顺手改写（那正是斜杠漂移的机制）
    assert second[:1] == first[:1], (
        f"先写的那条被后续保存改了：{first} -> {second}"
    )