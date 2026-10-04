"""``dispatch_post`` 的路由覆盖：路径写错就是 404，而 404 不报错。

第一版我把授权端点**直接测函数**，从没走过 ``dispatch_post``。于是
「路径拼错」「忘了 import」这类错会一路绿灯 —— 而界面上表现为
**点了没反应**，排查起来最难的那种。
"""

import json

import pytest

from freeagent.app import build_app
from freeagent.web import endpoints_delegate_projects as mod
from freeagent.web.routes import feishu_routes
from freeagent.web.routes_write import dispatch_post

ROWS = json.dumps([
    {"worktree": "/", "name": None},
    {"worktree": "D:/PycharmProjects/openmos", "name": "OpenMOS"},
])


@pytest.fixture
def env(monkeypatch, tmp_path):
    from freeagent.services.opencode_projects import _parse
    monkeypatch.setattr(mod, "list_projects", lambda: _parse(ROWS))
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "config.json").write_text(
        json.dumps({"delegate": {"projects": [], "model": ""}}),
        encoding="utf-8")
    app = build_app(tmp_path / "a.db")
    yield app
    app.close()


def _post(app, body):
    return dispatch_post(app, ["api", "delegate", "projects"],
                         lambda **kw: dict(body))


def test_get_route_is_registered():
    assert "/api/delegate/projects" in feishu_routes(), (
        "GET 没登记 —— 界面拉不到列表"
    )


def test_post_route_reaches_the_endpoint(env):
    got = _post(env, {"allow": "OpenMOS"})
    assert got is not None, "POST 路径没接上（dispatch_post 返回 None = 404）"
    status, payload = got
    assert status == 200
    by = {i["name"]: i["allowed"] for i in payload["items"]}
    assert by["OpenMOS"] is True


def test_post_path_typo_would_be_404(env):
    """对照：错一个字母就是 None。证明上面那条真的在走路由表。"""
    from freeagent.web.routes_write import dispatch_post as dp
    assert dp(env, ["api", "delegate", "project"], lambda **kw: {}) is None


def test_allow_really_writes_config(env):
    _post(env, {"allow": "OpenMOS"})
    cfg = json.loads(
        (__import__("pathlib").Path(env.config.home) / "config.json")
        .read_text(encoding="utf-8"))
    from pathlib import Path
    got = cfg["delegate"]["projects"]
    # 比归一化后的形式，不比字面量：斜杠方向由平台与读写顺序决定，
    # 断言字符串等于把实现细节钉死（上一版就是这么把斜杠漂移放过去的）。
    assert [str(Path(p)) for p in got] == [str(Path("D:/PycharmProjects/openmos"))], \
        f"授权没写进 config.json：{got}"