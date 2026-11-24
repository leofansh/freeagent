"""工作台项目档案的仓储与端点形状守卫（V1.124 / 设计文档 21.6）。

仓储守：按目录幂等、目录唯一、最近使用倒序、改名备注、删除只删记录。
端点守：建档导入先过目录校验、选中 / 清空 / 更新 / 删除的形状与偏好去向。

浏览器里点得到什么，不在这一层；这里守「接口给得出对的形状、坏目录起轮前就挡下」。
"""

from __future__ import annotations

import inspect
import types
import threading

import pytest

from freeagent.config import load_config
from freeagent.domain import FreeAgentError
from freeagent.web import endpoints_agent, endpoints_agent_projects as ep
from freeagent.web.endpoints_delegate_projects import _norm
from freeagent.web.endpoints_feishu import state_home_for


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


class TestSelectionAuthorizes:
    """选项目即授权（V1.169）：界面选中的项目，能力路由当场认、重启后仍在。

    起因是用户截图里项目按钮显示已选「freeagent 本体」，对话却回「没有授权
    任何项目」。根因是两套"项目"—— 工作台档案与委派白名单 —— 不一致。
    这里守住选 / 建档都写白名单（唯一真源）并即时刷新运行时能力视图。
    """

    def _authorized(self, app, directory: str) -> bool:
        cfg = load_config(state_home_for(app))
        return any(_norm(p) == _norm(directory) for p in cfg.delegate_projects)

    def test_save_authorizes_and_refreshes(self, app, tmp_path):
        ep.agent_project_save(app, {"directory": str(tmp_path)})
        # 白名单是重启后的真源：写进 config.json，不只是刷运行时内存。
        assert self._authorized(app, str(tmp_path))
        caps = app.chat.capabilities
        assert caps is not None
        assert any(_norm(p) == _norm(str(tmp_path)) for p in caps.projects)

    def test_select_authorizes_and_refreshes(self, app, tmp_path):
        # 直接建档案（绕过 save 的授权），单独验 select 这一条路径。
        row = app.project_repo.register(directory=str(tmp_path), name="甲",
                                        note="", now=app.clock.now())
        assert not self._authorized(app, str(tmp_path))
        ep.agent_project_select(app, {"id": row["id"]})
        assert self._authorized(app, str(tmp_path))
        caps = app.chat.capabilities
        assert caps is not None
        assert any(_norm(p) == _norm(str(tmp_path)) for p in caps.projects)

    def test_deselect_keeps_authorization(self, app, tmp_path):
        saved = ep.agent_project_save(app, {"directory": str(tmp_path)})
        ep.agent_project_select(app, {"id": saved["project"]["id"]})
        ep.agent_project_select(app, {"id": ""})
        # 黏性：取消选中只清工作目录，不撤销白名单 —— 那条可能是飞书页
        # 单独授权的，顺手删会误伤；撤销仍走飞书页显式动作。
        assert self._authorized(app, str(tmp_path))

    def test_helper_persists_and_refreshes(self):
        src = inspect.getsource(ep._authorize_and_refresh)
        assert "save_config(cfg, home)" in src, "白名单必须落盘，重启后仍在"
        assert "_capability_view(cfg)" in src, "用更新后的 cfg 重建视图"
        assert "chat._capabilities" in src, "当场刷新运行时视图，不必重启"


class TestProjectCounts:
    """左栏的「跑过几轮」（V1.139）—— 现算，读不到回 None 而不是 0。

    「还没跑过」和「读不出来」是两句话：把后者写成 0，人就会以为这个
    工作区是空白的，而实际上只是轮次表没挂上。
    """

    def _turn(self, app, directory: str, i: int) -> None:
        app.turn_repo.add(
            turn_id=f"t{i}", brief="一句话", directory=directory,
            mode="ask", started_at=app.clock.now(),
        )

    def test_counts_only_this_directory(self, app, tmp_path):
        for i in range(2):
            self._turn(app, str(tmp_path), i)
        self._turn(app, str(tmp_path / "other"), 9)
        ep.agent_project_save(app, {"directory": str(tmp_path)})
        assert ep.agent_projects(app)["projects"][0]["turns"] == 2

    def test_zero_turns_is_zero_not_missing(self, app, tmp_path):
        ep.agent_project_save(app, {"directory": str(tmp_path)})
        assert ep.agent_projects(app)["projects"][0]["turns"] == 0

    def test_no_turn_repo_reads_as_none(self, app, tmp_path, monkeypatch):
        ep.agent_project_save(app, {"directory": str(tmp_path)})
        monkeypatch.setattr(app, "turn_repo", None)
        assert ep.agent_projects(app)["projects"][0]["turns"] is None


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
