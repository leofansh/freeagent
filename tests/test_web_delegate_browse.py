"""``GET /api/delegate/browse`` 与 ``POST allow_pick`` 的接线测试。

服务层已在 ``test_delegate_browse.py`` 逐条验过拒绝方向；这里验的是
**接线**：路由登记、未配置时给 enabled=False（不是报错）、查询参数怎么
落进服务层、以及 POST ``allow_pick`` 是不是真的按同一道校验写进
``config.json`` —— 这一条必须走 ``dispatch_post``，只测端点函数会漏掉
「路径没接上（404 不报错）」。
"""

import json
import pathlib

import pytest

from freeagent.app import build_app
from freeagent.domain import FreeAgentError, ValidationError
from freeagent.web import endpoints_delegate_browse as browse_mod
from freeagent.web import endpoints_delegate_projects as proj_mod
from freeagent.web.routes import feishu_routes
from freeagent.web.routes_write import dispatch_post


def _tree(root: pathlib.Path) -> pathlib.Path:
    (root / "proj-a").mkdir(parents=True)
    (root / "proj-b").mkdir()
    (root / "file.txt").write_text("x", encoding="utf-8")
    return root


def _write_config(home, **delegate):
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(
        json.dumps({"delegate": dict(delegate)}), encoding="utf-8")


@pytest.fixture
def env(monkeypatch, tmp_path):
    """注册表置空：这两个端点不该偷偷依赖 OpenCode 在不在。"""
    monkeypatch.setattr(proj_mod, "list_projects", lambda: [])
    root = _tree(tmp_path / "roots" / "sandbox")
    _write_config(tmp_path, browse_roots=[str(root)])
    app = build_app(tmp_path / "a.db")
    yield app, tmp_path, root
    app.close()


def _get(app, **q):
    return browse_mod.delegate_browse_list(
        app, {k: [str(v)] for k, v in q.items()})


def _cfg(app):
    return json.loads(
        (pathlib.Path(app.config.home) / "config.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# 路由与开关
# --------------------------------------------------------------------------- #
def test_route_registered():
    assert "/api/delegate/browse" in feishu_routes()


def test_disabled_when_roots_empty(monkeypatch, tmp_path):
    """没配置不是错误：浏览默认关，界面据此整段不显示。"""
    monkeypatch.setattr(proj_mod, "list_projects", lambda: [])
    _write_config(tmp_path, projects=[])
    app = build_app(tmp_path / "a.db")
    try:
        out = _get(app)
    finally:
        app.close()
    assert out["enabled"] is False
    assert "delegate.browse_roots" in out["reason"]


def test_lists_dirs_in_root(env):
    app, _, root = env
    out = _get(app, root=0)
    assert out["enabled"] is True
    assert out["dirs"] == ["proj-a", "proj-b"]
    assert out["root"] == 0 and out["path"] == ""


def test_lists_subdir_and_echoes_path(env):
    app, _, root = env
    (root / "proj-a" / "inner").mkdir()
    out = _get(app, root=0, path="proj-a")
    assert out["dirs"] == ["inner"]
    assert out["path"] == "proj-a"


def test_bad_root_query_is_400(env):
    app, _, _ = env
    with pytest.raises(ValidationError):
        _get(app, root="abc")


def test_escape_is_rejected(env):
    app, _, _ = env
    with pytest.raises(ValidationError):
        _get(app, root=0, path="..")


# --------------------------------------------------------------------------- #
# POST allow_pick：与 GET 同一道校验，真写 config.json
# --------------------------------------------------------------------------- #
def test_allow_pick_via_dispatch_post_writes_config(env):
    app, _, root = env
    status, out = dispatch_post(
        app, ["api", "delegate", "projects"],
        lambda **kw: {"allow_pick": {"root": 0, "path": "proj-a"}},
    )
    assert status and out is not None, "POST 路径没接上（返回 None = 404）"
    saved = _cfg(app)["delegate"]["projects"]
    assert any(
        pathlib.Path(p) == (root / "proj-a").resolve() for p in saved
    ), f"allow_pick 没写进白名单：{saved}"
    row = [i for i in out["items"] if i["source"] == "whitelist"]
    assert row and row[0]["allowed"] is True


def test_allow_pick_requires_configured_roots(monkeypatch, tmp_path):
    monkeypatch.setattr(proj_mod, "list_projects", lambda: [])
    _write_config(tmp_path, projects=[])
    app = build_app(tmp_path / "a.db")
    try:
        with pytest.raises(FreeAgentError, match="未配置"):
            proj_mod.delegate_projects_save(
                app, {"allow_pick": {"root": 0, "path": "x"}})
    finally:
        app.close()


def test_allow_pick_rejects_absolute_path(env):
    app, _, root = env
    with pytest.raises(FreeAgentError):
        proj_mod.delegate_projects_save(
            app, {"allow_pick": {"root": 0, "path": str(root / "proj-a")}})


def test_allow_pick_is_exclusive_with_allow(env):
    app, _, _ = env
    with pytest.raises(FreeAgentError, match="只能给一个"):
        proj_mod.delegate_projects_save(
            app, {"allow_pick": {"root": 0, "path": "proj-a"},
                  "allow": "Whatever"})


def test_allow_pick_twice_reports_already_authorized(env):
    app, _, _ = env
    proj_mod.delegate_projects_save(
        app, {"allow_pick": {"root": 0, "path": "proj-a"}})
    with pytest.raises(FreeAgentError, match="已经在白名单里"):
        proj_mod.delegate_projects_save(
            app, {"allow_pick": {"root": 0, "path": "proj-a"}})
