"""``/api/delegate/projects``（只读）的测试。

重点：**未授权的项目要显式标出来**，而不是混在列表里让用户以为能用。
"""

import json

import json

from freeagent.app import build_app
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


def _write_config(home, projects):
    """把白名单写进 ``config.json``，让 ``load_config`` 真的读到它。

    刻意走**真的配置文件**而不是改 ``app.config``：端点读的是
    ``load_config(state_home_for(app))``，只改内存对象会让测试和真机分道扬镳
    —— 那正是「断言落在错的通道上」。
    """
    import json as _json
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(
        _json.dumps({"delegate": {"projects": list(projects)}}),
        encoding="utf-8")


def _call(monkeypatch, tmp_path, projects, projects_fn=None):
    # ``projects_fn`` 显式传，不在这里默认 —— 上一版默认了 ``_stub`` 之后，
    # 有一处想测「查不到」的用例顺手写成 ``monkeypatch.setattr`` 却在
    # 调``_call`` 时被默认值盖回去，于是那条测试验的是有数据的情况。
    # 依赖越少越不容易错位。
    monkeypatch.setattr(mod, "list_projects",
                        _stub if projects_fn is None else projects_fn)
    _write_config(tmp_path, projects)
    # 显式给 db_path 时配置目录取它的父目录（build_app 自己的语义），
    # 所以 config.json 必须写在同一个 tmp_path 里才会被 load_config 读到。
    app = build_app(tmp_path / "a.db")
    try:
        return feishu_routes()["/api/delegate/projects"](app)
    finally:
        app.close()


# --------------------------------------------------------------------------- #
def test_default_project_never_appears(monkeypatch, tmp_path):
    out = _call(monkeypatch, tmp_path, [])
    paths = [i["path"] for i in out["items"]]
    assert "/" not in paths, f"Default Project 漏进来了：{paths}"


def test_two_projects_listed(monkeypatch, tmp_path):
    out = _call(monkeypatch, tmp_path, [])
    assert [i["name"] for i in out["items"]] == ["FreeAgent", "OpenMOS"]
    assert [i["key"] for i in out["items"]] == ["freeagent", "openmos"]


def test_allowed_flag_marks_whitelisted(monkeypatch, tmp_path):
    out = _call(monkeypatch, tmp_path, ["D:/PycharmProjects/openmos"])
    by = {i["name"]: i for i in out["items"]}
    assert by["OpenMOS"]["allowed"] is True
    assert by["FreeAgent"]["allowed"] is False, (
        "未授权的必须显式标出来 —— 否则界面上看着能用、点下去被拒"
    )


def test_path_comparison_is_normalised(monkeypatch, tmp_path):
    """配置里写反斜杠 / 带尾斜杠，也要认成同一个项目。"""
    out = _call(monkeypatch, tmp_path, ["D:\\PycharmProjects\\openmos\\"])
    by = {i["name"]: i for i in out["items"]}
    assert by["OpenMOS"]["allowed"] is True, (
        "路径写法不同就判成未授权 = 用户明明授权过"
    )


def test_empty_list_says_so_explicitly(monkeypatch, tmp_path):
    """查不到时必须能区分「你没项目」和「opencode 没找到」。

    上一版这里 monkeypatch 成了 ``list``（内建函数），于是返回的是
    **函数本身**而不是空列表 —— ``bool(函数)`` 恒为真，那条断言什么都没验到。
    这就是「断言落在错的通道上」：测试绿了，但它验的不是它声称的东西。
    """
    out = _call(monkeypatch, tmp_path, [], projects_fn=lambda: [])
    assert out["items"] == []
    assert out["opencode_found"] is False
    assert out["items"] == []


def test_whitelist_echoed_for_display(monkeypatch, tmp_path):
    out = _call(monkeypatch, tmp_path, ["D:/PycharmProjects/openmos"])
    assert out["whitelist"] == ["D:/PycharmProjects/openmos"]