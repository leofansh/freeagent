"""这个工作区有什么（V1.142）+ 安静模式与后台运行那句的接线守卫。

## 后端守什么

``/api/agent/files`` 是本仓**第一个把文件名清单摆到界面上**的端点 ——
比目录选择器又往前走了一步。所以断言集中在「越界不可能」与「不读内容」：

1. 根只能来自工作区档案或偏好目录，**传绝对路径一律拒绝**；
2. ``../`` 解出根外路径也拒绝（fail-closed，不退回空列表）；
3. 条目形状里没有内容字段 —— 只有名字、是不是目录、大小；
4. 「读不出来」与「真的空」分开说（``state`` 字段）。

## 前端守什么

右栏拆了上下两块（``#wb-side-turn`` / ``#wb-files``），安静模式靠
``.wb-view.is-zen`` 收外壳。两者都是纯前端，跑不起真浏览器时用源码钉形状：
清空目标必须换成上块（否则每轮把文件清单也抹掉）、zen 必须有可见出口
（Esc + 窄条上的按钮），不能只剩一条「按了就没」的路。
"""

from __future__ import annotations

import types

import pytest

from freeagent.domain import FreeAgentError
from freeagent.web import endpoints_agent_files


class _Repo:
    def __init__(self, rows):
        self._rows = rows

    def list_all(self):
        return list(self._rows)


def _app(tmp_path, rows=()):
    """假 app：只要 ``config.home``（偏好落点）与 ``project_repo``。"""
    return types.SimpleNamespace(
        config=types.SimpleNamespace(home=str(tmp_path)),
        project_repo=_Repo(rows),
    )


def _workspace(tmp_path, name="ws", *, rows_extra=()):
    (tmp_path / name).mkdir(parents=True, exist_ok=True)
    rows = ({"id": name, "name": name, "directory": str(tmp_path / name)},) + tuple(rows_extra)
    return _app(tmp_path, rows)


class TestRoot:
    def test_no_workspace_is_a_state_not_an_empty_list(self, tmp_path):
        out = endpoints_agent_files.agent_files(_app(tmp_path), {})
        assert out["state"] == "no_dir"
        assert "还没选工作区" in out["reason"]
        assert "entries" not in out, "没选工作区时给一个空列表等于说「这里什么都没有」"

    def test_unknown_project_id_is_reported(self, tmp_path):
        app = _app(tmp_path, rows=[{"id": "p1", "name": "一号", "directory": "/nowhere"}])
        with pytest.raises(FreeAgentError, match="没有这个工作区"):
            endpoints_agent_files.agent_files(app, {"project": ["p2"]})

    def test_project_without_directory_says_so(self, tmp_path):
        app = _app(tmp_path, rows=[{"id": "p1", "name": "一号", "directory": ""}])
        with pytest.raises(FreeAgentError, match="没有记目录"):
            endpoints_agent_files.agent_files(app, {"project": ["p1"]})

    def test_falls_back_to_the_selected_directory(self, tmp_path):
        (tmp_path / "solo").mkdir()
        app = _app(tmp_path)
        (tmp_path / "workbench.json").write_text(
            '{"directory": "%s"}' % (tmp_path / "solo").as_posix(), encoding="utf-8")
        out = endpoints_agent_files.agent_files(app, {})
        assert out["state"] == "ok"
        assert out["entries"] == []


class TestBoundary:
    def test_absolute_path_is_refused(self, tmp_path):
        app = _workspace(tmp_path)
        with pytest.raises(FreeAgentError, match="只收工作区内的相对路径"):
            endpoints_agent_files.agent_files(
                app, {"project": ["ws"], "path": [str(tmp_path)]})

    def test_parent_escape_is_refused(self, tmp_path):
        app = _workspace(tmp_path)
        with pytest.raises(FreeAgentError, match="不在工作区目录内"):
            endpoints_agent_files.agent_files(
                app, {"project": ["ws"], "path": ["../"]})

    def test_missing_subdirectory_is_a_state_not_a_crash(self, tmp_path):
        app = _workspace(tmp_path)
        out = endpoints_agent_files.agent_files(
            app, {"project": ["ws"], "path": ["gone"]})
        assert out["state"] == "gone"
        assert "读不到" in out["reason"]


class TestListing:
    def test_directories_first_then_files(self, tmp_path):
        root = tmp_path / "ws"
        root.mkdir()
        (root / "b.txt").write_text("x", encoding="utf-8")
        (root / "sub").mkdir()
        (root / "a.txt").write_text("yy", encoding="utf-8")
        out = endpoints_agent_files.agent_files(_workspace(tmp_path), {"project": ["ws"]})
        names = [e["name"] for e in out["entries"]]
        assert names[0] == "sub", "目录排前面 —— 优先要能往下走"
        assert set(names) == {"a.txt", "b.txt", "sub"}

    def test_entry_shape_has_no_content_field(self, tmp_path):
        root = tmp_path / "ws"
        root.mkdir()
        (root / "a.txt").write_text("hello", encoding="utf-8")
        out = endpoints_agent_files.agent_files(_workspace(tmp_path), {"project": ["ws"]})
        one = out["entries"][0]
        assert set(one) == {"name", "dir", "size", "rel", "abs"}
        assert one["size"] == 5, "只 stat 大小，不读内容"
        assert one["dir"] is False

    def test_subdirectory_reports_rel_and_parent(self, tmp_path):
        root = tmp_path / "ws"
        (root / "a" / "b").mkdir(parents=True)
        out = endpoints_agent_files.agent_files(
            _workspace(tmp_path), {"project": ["ws"], "path": ["a/b"]})
        assert out["path"] == "a/b"
        assert out["parent"] == "a"

    def test_root_has_empty_parent(self, tmp_path):
        root = tmp_path / "ws"
        root.mkdir()
        out = endpoints_agent_files.agent_files(_workspace(tmp_path), {"project": ["ws"]})
        assert out["path"] == ""
        assert out["parent"] == ""

    def test_limit_is_reported_as_truncated(self, tmp_path):
        root = tmp_path / "ws"
        root.mkdir()
        for i in range(endpoints_agent_files._MAX_FILES + 5):
            (root / f"f{i:03d}.txt").write_text("", encoding="utf-8")
        out = endpoints_agent_files.agent_files(_workspace(tmp_path), {"project": ["ws"]})
        assert len(out["entries"]) == endpoints_agent_files._MAX_FILES
        assert out["truncated"] is True
        assert out["total"] == endpoints_agent_files._MAX_FILES + 5, (
            "总数目要如实给 —— 只说「前 300 项」而不说一共多少，人会以为就这么点"
        )


class TestFrontEndWiring:
    def test_page_mounts_both_right_column_blocks(self):
        from freeagent.web.page import INDEX_HTML

        assert '#wb-side-turn' in INDEX_HTML or 'wb-side-turn' in INDEX_HTML
        assert 'wbSideMount(side)' in INDEX_HTML, "装配里没挂，右栏就只有产物"
        assert '/api/agent/files' in INDEX_HTML
        assert 'wbZenBoot()' in INDEX_HTML
        assert 'wbRunNote()' in INDEX_HTML

    def test_side_paint_clears_the_upper_block_only(self):
        from freeagent.web.js_workbench_side import JS_WORKBENCH_SIDE

        assert '$("#wb-side-turn")' in JS_WORKBENCH_SIDE, (
            "清空目标若是 #wb-side，每轮都会把「这个工作区有什么」一起抹掉"
        )

    def test_zen_mode_has_a_visible_way_out(self):
        from freeagent.web.js_workbench_shell import JS_WORKBENCH_SHELL

        assert '"Escape"' in JS_WORKBENCH_SHELL, "Esc 是最快的出口"
        assert "退出（Esc）" in JS_WORKBENCH_SHELL, "窄条上还要有一枚看得见的按钮"
        assert "wbZenOn = false" in JS_WORKBENCH_SHELL, (
            "localStorage 存不下时必须按「没开」，不能抛在那条 catch 里"
        )

    def test_files_paint_takes_the_level_from_the_caller(self):
        from freeagent.web.js_workbench_files import JS_WORKBENCH_FILES

        assert "async function wbFilesPaint(at)" in JS_WORKBENCH_FILES
        assert "wbFilesPaint(rel)" in JS_WORKBENCH_FILES, "「进去」必须把目标层传下去"
        # 走查第一遍的坑：重置曾写在 paint 开头，wbFilesOpen 刚设好的路径当场被
        # 抹掉，点目录永远原地不动。回根是**调用点**的事（切 / 建 / 删工作区传空），
        # 不是每次重画都回根。
        body = JS_WORKBENCH_FILES.split("async function wbFilesPaint(at)", 1)[1]
        assert 'wbFilesPath = at || "";' in body, "画的层必须来自调用点"
        assert 'wbFilesPath = "";' not in body, (
            "paint 里不许重置路径 —— 那会把「进去」设的层抹掉"
        )

    def test_css_hides_the_shell_under_is_zen(self):
        from freeagent.web.style_workbench import STYLE_WORKBENCH_CSS

        assert ".wb-view.is-zen #wb-bar" in STYLE_WORKBENCH_CSS
        assert ".wb-view.is-zen .wb-col-rail" in STYLE_WORKBENCH_CSS
        assert ".wb-view.is-zen .wb-col-side" in STYLE_WORKBENCH_CSS
