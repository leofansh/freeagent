"""多会话（V1.176）的仓储与端点形状守卫。

守的是「一处状态一个真源」在会话这条线上的具体形状：
1. 会话按目录隔离、按最近更新倒序，删会话不删轮次（轮次是历史）；
2. 目录归属只在端点判一次（``_owned``），防跨目录串味；
3. ``hist:`` 前缀的历史会话没有引擎对应物，切过去必须清引擎会话 id；
4. 会话读不到时 fail-closed 给原因，不拖垮 ``agent_state``。
"""

from __future__ import annotations

import threading
import types
from pathlib import Path

import pytest

from freeagent.domain import FreeAgentError
from freeagent.services import engine_host
from freeagent.web import endpoints_agent, endpoints_agent_sessions as sessions
from freeagent.web.endpoints_agent_projects import load_prefs, save_prefs


class _FakeHost:
    """记下端点有没有把会话切下去 —— 不真起引擎（那会变成环境类红点）。"""

    def __init__(self) -> None:
        self.ensured: list[dict] = []
        self.switched: list[str] = []
        self.created = 0

    def ensure(self, **kwargs):
        self.ensured.append(kwargs)
        return "eng-0"

    def new_session(self) -> str:
        self.created += 1
        return f"eng-{self.created}"

    def switch_session(self, sid: str) -> None:
        self.switched.append(sid)


class TestChatSessionRepo:
    def test_create_list_and_directory_isolation(self, app):
        repo = app.chat_session_repo
        a = str(Path(app.config.home) / "a")
        b = str(Path(app.config.home) / "b")
        repo.create(session_id="s1", directory=a, title="甲", now=app.clock.now())
        repo.create(session_id="s2", directory=a, title="乙", now=app.clock.now())
        repo.create(session_id="s3", directory=b, title="丙", now=app.clock.now())

        rows = repo.list_by_directory(a)  # 最近更新倒序；同刻 rowid 定序
        assert [r["id"] for r in rows] == ["s2", "s1"]
        assert [r["title"] for r in rows] == ["乙", "甲"]
        assert [r["id"] for r in repo.list_by_directory(b)] == ["s3"]
        assert repo.get("s1")["directory"] == a
        assert repo.get("missing") is None

    def test_rename_and_delete_leave_turns_alone(self, app):
        repo = app.chat_session_repo
        repo.create(session_id="s1", directory="D:/p", title="旧", now=app.clock.now())
        renamed = repo.rename("s1", "新", app.clock.now())
        assert renamed["title"] == "新"
        assert repo.rename("missing", "x", app.clock.now()) is None

        # 删会话只删档案，轮次行留着（孤儿 session_id 由读侧按历史兜底）。
        app.turn_repo.add(
            turn_id="t1", brief="做点事", directory="D:/p", mode="ask",
            started_at=app.clock.now(), session_id="s1",
        )
        repo.delete("s1")
        assert repo.get("s1") is None
        assert any(r["session_id"] == "s1"
                   for r in app.turn_repo.recent())


class TestSessionEndpoints:
    @staticmethod
    def _app(app, monkeypatch, *, directory: str = "D:/p"):
        """复用真 app 的仓储 / clock / config.home，只把引擎替换成假对象。"""
        save_prefs(app, {"directory": directory})
        fake = _FakeHost()
        monkeypatch.setattr(sessions, "_host", lambda a: fake)
        return app, fake

    def test_actions_registered_in_agent_post(self):
        for name in ("session_new", "session_switch", "session_rename",
                     "session_delete", "session_import"):
            assert name in sessions.ACTIONS
            assert name in endpoints_agent._ACTIONS

    def test_owned_rejects_other_directory(self, app, monkeypatch):
        app, _ = self._app(app, monkeypatch, directory="D:/here")
        app.chat_session_repo.create(
            session_id="s1", directory="D:/there", title="别处", now=app.clock.now())
        with pytest.raises(FreeAgentError, match="另一个目录"):
            sessions._session_switch(app, {"id": "s1"})

    def test_switch_calls_host_and_updates_prefs(self, app, monkeypatch):
        app, fake = self._app(app, monkeypatch)
        app.chat_session_repo.create(
            session_id="eng-9", directory="D:/p", title="甲", now=app.clock.now())
        out = sessions._session_switch(app, {"id": "eng-9"})
        assert out["kind"] == "agent_session_switch"
        assert fake.switched == ["eng-9"]
        assert load_prefs(app)["session_id"] == "eng-9"

    def test_import_creates_history_session(self, app, monkeypatch):
        app, _ = self._app(app, monkeypatch)
        out = sessions._session_import(app, {"title": "历史会话"})
        assert out["kind"] == "agent_session_import"
        sid = out["session"]["id"]
        assert sid.startswith("hist:")
        assert app.chat_session_repo.get(sid)["title"] == "历史会话"

    def test_delete_current_clears_prefs_but_keeps_turns(self, app, monkeypatch):
        app, _ = self._app(app, monkeypatch)
        app.chat_session_repo.create(
            session_id="eng-1", directory="D:/p", title="甲", now=app.clock.now())
        # save_prefs 是全量覆盖：写 session_id 时得把 directory 一并带上。
        save_prefs(app, {"directory": "D:/p", "session_id": "eng-1"})
        out = sessions._session_delete(app, {"id": "eng-1"})
        assert out["kind"] == "agent_session_delete"
        assert "session_id" not in load_prefs(app)

    def test_sessions_for_prefs_fail_closed_without_directory(self, app, monkeypatch):
        save_prefs(app, {})  # 空偏好 —— 没选目录
        out = sessions.sessions_for_prefs(app)
        assert out["available"] is False
        assert "工作目录" in out["reason"]
        assert out["sessions"] == []

    def test_agent_sessions_with_directory_override(self, app, monkeypatch):
        app, _ = self._app(app, monkeypatch, directory="D:/p")
        app.chat_session_repo.create(
            session_id="s1", directory="D:/p", title="甲的会话", now=app.clock.now())
        out = sessions.agent_sessions(app, {"directory": ["D:/p"]})
        assert out["kind"] == "agent_sessions"
        assert out["available"] is True
        assert [s["title"] for s in out["sessions"]] == ["甲的会话"]


class TestEngineHostSessionSwitch:
    def test_history_prefix_clears_engine_session(self):
        """历史会话没有引擎对应物：拿 hist: 假 id 去 prompt 等于打给不存在的会话。"""
        class _Server:
            def use_session(self, sid):
                raise AssertionError("hist: id 不许传给引擎")

        host = engine_host.EngineHost()
        host._server = _Server()
        host._session = "eng-old"
        host._usage_cache = ("x", "eng-old", None)
        host._ledger_cache = ("x", "eng-old", None)
        host.switch_session("hist:abc")
        assert host._session == ""
        assert host._usage_cache is None
        assert host._ledger_cache is None

    def test_real_id_switches_server_instance(self):
        seen = []

        class _Server:
            def use_session(self, sid):
                seen.append(sid)

        host = engine_host.EngineHost()
        host._server = _Server()
        host.switch_session("eng-2")
        assert seen == ["eng-2"]
        assert host._session == "eng-2"
