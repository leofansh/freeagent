"""三源并集（12.7.1.1）：白名单行必须进列表、按路径撤销，且不重复。

守的两件事：
1. 白名单里 OpenCode 不知道的行**以前只是段纯文本**——看得见、撤不掉。
   并集后它必须是带 ``source=whitelist`` 的正式行。
2. 注册表行与白名单行同路径时**只出现一条**（否则界面出现两行同路径，
   一个能撤一个不能，用户会以为有两条配置）。
"""

import json
import pathlib

import pytest

from freeagent.app import build_app
from freeagent.domain import FreeAgentError
from freeagent.web import endpoints_delegate_projects as mod
from freeagent.web.routes import feishu_routes

ROWS = json.dumps([
    {"worktree": "/", "name": None},                       # Default Project
    {"worktree": "D:/PycharmProjects/freeagent", "name": "FreeAgent"},
    {"worktree": "D:/PycharmProjects/openmos", "name": "OpenMOS"},
])


def _stub():
    from freeagent.services.opencode_projects import _parse
    return _parse(ROWS)


def _write_config(home, projects, browse_roots=()):
    home.mkdir(parents=True, exist_ok=True)
    payload = {"delegate": {"projects": list(projects)}}
    if browse_roots:
        payload["delegate"]["browse_roots"] = list(browse_roots)
    (home / "config.json").write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "list_projects", _stub)
    _write_config(tmp_path, [])
    app = build_app(tmp_path / "a.db")
    yield app, tmp_path
    app.close()


def _projects(app):
    return feishu_routes()["/api/delegate/projects"](app)


def _cfg_projects(app):
    return json.loads(
        (pathlib.Path(app.config.home) / "config.json")
        .read_text(encoding="utf-8"))["delegate"]["projects"]


# --------------------------------------------------------------------------- #
# 列表：三源各自带 source
# --------------------------------------------------------------------------- #
def test_registry_rows_are_marked_opencode(env):
    app, _ = env
    items = _projects(app)["items"]
    assert items and all(i["source"] == "opencode" for i in items)


def test_whitelist_only_row_becomes_listable_and_revocable(env, tmp_path):
    app, _ = env
    _write_config(tmp_path, ["D:/manual/manual1"])
    out = _projects(app)
    rows = [i for i in out["items"] if i["source"] == "whitelist"]
    assert len(rows) == 1, "白名单里 OpenCode 不知道的行必须出现在列表里"
    assert rows[0]["name"] == "manual1"
    assert rows[0]["allowed"] is True, "它在白名单里 —— 按定义就是已授权"
    assert rows[0]["path"].replace("\\", "/").rstrip("/") == "D:/manual/manual1"


def test_no_duplicate_when_project_is_both_whitelisted_and_registered(env, tmp_path):
    app, _ = env
    _write_config(tmp_path, ["D:/PycharmProjects/openmos"])
    items = _projects(app)["items"]
    openmos = [i for i in items if i["path"].casefold().endswith("openmos")]
    assert len(openmos) == 1, "同一路径不能出现两行（一个能撤一个不能）"
    assert openmos[0]["source"] == "opencode"


def test_registry_unavailable_still_lists_whitelist_rows(monkeypatch, tmp_path):
    """注册表查不到 ≠ 没东西可显示：白名单行与浏览不依赖 OpenCode。"""
    monkeypatch.setattr(mod, "list_projects", lambda: [])
    _write_config(tmp_path, ["D:/manual/manual1"])
    app = build_app(tmp_path / "a.db")
    try:
        out = _projects(app)
    finally:
        app.close()
    assert out["opencode_found"] is False
    assert [i["name"] for i in out["items"]] == ["manual1"]


def test_browse_roots_echoed_for_picker(env, tmp_path):
    app, _ = env
    _write_config(tmp_path, [], browse_roots=["D:/work"])
    assert _projects(app)["browse_roots"] == ["D:/work"]


# --------------------------------------------------------------------------- #
# 撤销：路径精确匹配（不止 basename）
# --------------------------------------------------------------------------- #
def test_revoke_whitelist_row_by_path(env, tmp_path):
    app, _ = env
    _write_config(tmp_path, ["D:/manual/manual1", "D:/manual/manual2"])
    out = mod.delegate_projects_save(app, {"revoke": "D:/manual/manual1"})
    assert out["items"], "同白名单里的另一条不该被误删"
    left = _cfg_projects(app)
    assert len(left) == 1
    assert pathlib.Path(left[0]).name == "manual2"


def test_revoke_by_path_does_not_hit_same_basename(env, tmp_path):
    """两条同末段名的目录：按路径撤只删被点的那条（旧版按名字会删错）。"""
    app, _ = env
    _write_config(tmp_path, ["D:/x/proj", "D:/y/proj"])
    mod.delegate_projects_save(app, {"revoke": "D:/x/proj"})
    left = _cfg_projects(app)
    assert [pathlib.Path(p).parent.name for p in left] == ["y"], left


def test_revoke_registry_row_by_name_still_works(env, tmp_path):
    """兼容旧入口：终端/脚本仍可能按项目名撤销。"""
    app, _ = env
    _write_config(tmp_path, ["D:/PycharmProjects/openmos"])
    mod.delegate_projects_save(app, {"revoke": "OpenMOS"})
    assert _cfg_projects(app) == []


def test_revoke_unknown_path_reports_nothing_to_revoke(env):
    app, _ = env
    with pytest.raises(FreeAgentError, match="没什么可撤"):
        mod.delegate_projects_save(app, {"revoke": "D:/not/there"})
