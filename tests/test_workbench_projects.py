"""工作台项目档案的仓储与端点形状守卫（V1.124 / 设计文档 21.6）。

仓储守：按目录幂等、目录唯一、最近使用倒序、改名备注、删除只删记录。
端点守：建档导入先过目录校验、选中 / 清空 / 更新 / 删除的形状与偏好去向。

浏览器里点得到什么，不在这一层；这里守「接口给得出对的形状、坏目录起轮前就挡下」。
"""

from __future__ import annotations

import types
import threading

import pytest

from freeagent.domain import FreeAgentError
from freeagent.web import endpoints_agent, endpoints_agent_projects as ep


class TestProjectRepo:
    def test_register_is_idempotent_by_directory(self, app, tmp_path):
        repo = app.project_repo
        first = repo.register(directory=str(tmp_path), name="项目甲", note="n",
                              now=app.clock.now())
        second = repo.register(directory=str(tmp_path), name="不该改名字",
                                note="", now=app.clock.now())
        assert first["id"] == second["id"]
        # 幂等只刷新最近使用，不长出第二行，也不把已有名字覆盖掉。
        assert len(repo.list_all()) == 1
        assert second["name"] == "项目甲"

    def test_register_preserves_directory_uniqueness(self, app, tmp_path):
        repo = app.project_repo
        repo.register(directory=str(tmp_path), name="甲", note="",
                      now=app.clock.now())
        other = app.project_repo.find_by_directory(str(tmp_path))
        assert other is not None

    def test_list_all_orders_by_recent_use(self, app, tmp_path):
        repo = app.project_repo
        a = repo.register(directory=str(tmp_path / "a"), name="甲", note="",
                          now=app.clock.now())
        app.clock.advance(minutes=1)
        b = repo.register(directory=str(tmp_path / "b"), name="乙", note="",
                          now=app.clock.now())
        app.clock.advance(minutes=1)
        # 再触一次甲：它的最近使用晚于乙，必须排到最前。同刻定序才退 rowid。
        repo.touch(a["id"], app.clock.now())
        names = [r["name"] for r in repo.list_all()]
        assert names[0] == "甲"
        assert b["id"] in [r["id"] for r in repo.list_all()]

    def test_rename_updates_name_and_note_only(self, app, tmp_path):
        repo = app.project_repo
        row = repo.register(directory=str(tmp_path), name="旧名", note="旧备注",
                             now=app.clock.now())
        updated = repo.rename(row["id"], "新名", "新备注")
        assert updated["name"] == "新名"
        assert updated["note"] == "新备注"
        assert updated["directory"] == str(tmp_path)

    def test_delete_removes_record_but_not_the_directory(self, app, tmp_path):
        repo = app.project_repo
        row = repo.register(directory=str(tmp_path), name="将删", note="",
                             now=app.clock.now())
        repo.delete(row["id"])
        assert repo.get(row["id"]) is None
        assert repo.find_by_directory(str(tmp_path)) is None
        assert tmp_path.is_dir(), "档案删了，磁盘目录不能跟着删"


class TestProjectEndpoints:
    def test_empty_list_reports_no_project(self, app):
        data = ep.agent_projects(app)
        assert data["kind"] == "agent_projects"
        assert data["projects"] == []
        assert data["project_id"] == ""

    def test_save_registers_and_writes_prefs(self, app, tmp_path):
        data = ep.agent_project_save(app, {"directory": str(tmp_path)})
        p = data["project"]
        assert p["directory"] == str(tmp_path)
        # 名缺省取目录末段，不给空名。
        assert p["name"]
        prefs = ep.load_prefs(app)
        assert prefs["project_id"] == p["id"]
        assert prefs["directory"] == str(tmp_path)

    def test_save_rejects_relative_and_missing_directory(self, app, tmp_path):
        with pytest.raises(FreeAgentError, match="绝对路径"):
            ep.agent_project_save(app, {"directory": "relative"})
        with pytest.raises(FreeAgentError, match="不存在或不是目录"):
            ep.agent_project_save(app, {"directory": str(tmp_path / "nope")})

    def test_select_sets_prefs_to_project_directory(self, app, tmp_path):
        saved = ep.agent_project_save(app, {"directory": str(tmp_path)})
        ep.agent_project_select(app, {"id": saved["project"]["id"]})
        prefs = ep.load_prefs(app)
        assert prefs["project_id"] == saved["project"]["id"]
        assert prefs["directory"] == str(tmp_path)

    def test_select_empty_id_clears_selection_but_keeps_record(self, app, tmp_path):
        saved = ep.agent_project_save(app, {"directory": str(tmp_path)})
        ep.agent_project_select(app, {"id": saved["project"]["id"]})
        ep.agent_project_select(app, {"id": ""})
        prefs = ep.load_prefs(app)
        assert "project_id" not in prefs
        assert "directory" not in prefs
        # 取消选中不是删档案：历史轮次与记录都还在。
        assert ep.agent_projects(app)["projects"]

    def test_update_name_and_note(self, app, tmp_path):
        saved = ep.agent_project_save(app, {"directory": str(tmp_path)})
        data = ep.agent_project_update(app, {
            "id": saved["project"]["id"], "name": "正式名", "note": "备注",
        })
        assert data["project"]["name"] == "正式名"
        assert data["project"]["note"] == "备注"

    def test_delete_clears_current_selection(self, app, tmp_path):
        saved = ep.agent_project_save(app, {"directory": str(tmp_path)})
        pid = saved["project"]["id"]
        ep.agent_project_select(app, {"id": pid})
        ep.agent_project_delete(app, {"id": pid})
        prefs = ep.load_prefs(app)
        assert "project_id" not in prefs
        assert ep.agent_projects(app)["projects"] == []

    def test_state_payload_carries_projects_and_selected_id(self, app, tmp_path):
        ep.agent_project_save(app, {"directory": str(tmp_path)})
        state = endpoints_agent.agent_state(app)
        assert len(state["projects"]) == 1
        assert state["prefs"]["project_id"]
        assert state["prefs"]["directory"] == str(tmp_path)


class _NoRepoApp:
    """老装配 / 测试替身没挂仓储：建档能力如实缺席，至少把目录记进偏好。"""

    def __init__(self, tmp_path):
        self.lock = threading.RLock()
        self.config = types.SimpleNamespace(home=str(tmp_path))


def test_save_without_repo_falls_back_to_prefs_only(tmp_path):
    app = _NoRepoApp(tmp_path)
    data = ep.agent_project_save(app, {"directory": str(tmp_path)})
    assert data["project"] is None
    assert data["directory"] == str(tmp_path)
    assert ep.load_prefs(app)["directory"] == str(tmp_path)
