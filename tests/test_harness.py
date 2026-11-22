"""Harness 一页（V1.143）的守卫。

## 后端守什么

这一页全是「已有事实的回显」，所以断言集中在**不许替人下结论**：

1. 计数读不出来时**不给键**（不是给 0）—— 0 会被读成「你没有」；
2. 记忆条数只能精确时才精确，等于上限时写「至少」，并进 ``notes``；
3. ``AGENTS.md`` 只报文件事实（在不在 / 行数 / 大小 / 何时改过），
   **不解析内容、不调 ``today.view()``**（那个会顺延并往 ``task_records`` 留记录）。

## 前端守什么

``/api/harness`` 进页面、导航有 ``data-view="harness"``、``MENU_VIEWS`` 收它、
``boot`` 有分发分支 —— 缺一处，这个入口就点不开。
"""

from __future__ import annotations

import types

import pytest

from freeagent.services import harness


class _Tasks:
    def __init__(self, n_open=0, n_closed=0, explode=False):
        self._rows = [types.SimpleNamespace(is_open=True)] * n_open + [
            types.SimpleNamespace(is_open=False)] * n_closed
        self._explode = explode

    def list_all(self):
        if self._explode:
            raise RuntimeError("库锁住了")
        return list(self._rows)


class _Memory:
    def __init__(self, entries=(), explode=False):
        self._entries = list(entries)
        self._explode = explode

    def list_all(self, *, limit=100, scope=None):
        if self._explode:
            raise RuntimeError("记忆表损坏")
        return self._entries[:limit]


def _entry(kind="偏好", content="先看今天", created="2026-11-01T09:00:00"):
    return types.SimpleNamespace(kind=kind, scope="global",
                                 content=content, created_at=created)


class _Projects:
    def __init__(self, rows):
        self._rows = rows

    def list_all(self):
        return list(self._rows)


class _Turns:
    def __init__(self, n=0):
        self._n = n

    def count_by_directory(self, directory):
        return self._n


def _app(tmp_path, *, projects=(), memory=None, tasks=None, promises=0,
         pending=0, provider="RuleBasedProvider"):
    return types.SimpleNamespace(
        config=types.SimpleNamespace(home=str(tmp_path), has_credentials=False),
        llm_name=provider,
        project_repo=_Projects(projects),
        turn_repo=_Turns(3),
        memory=memory or _Memory(),
        task_repo=tasks or _Tasks(),
        promise=types.SimpleNamespace(open_count=lambda: promises),
        pending=types.SimpleNamespace(count=lambda: pending),
    )


class TestRuleFact:
    def test_counts_lines_and_reports_facts(self, tmp_path):
        (tmp_path / "ws").mkdir()
        (tmp_path / "ws" / "AGENTS.md").write_text("一行\n二行\n三行\n", encoding="utf-8")
        app = _app(tmp_path, projects=[{
            "id": "ws", "name": "ws", "directory": str(tmp_path / "ws"), "note": ""}])
        rule = harness.view(app).projects[0].rule
        assert rule.exists is True
        assert rule.lines == 3
        assert rule.size > 0
        assert rule.updated_at, "最后改于必须报，否则「这规矩还作数吗」没法判断"
        assert rule.note == ""

    def test_big_file_reports_none_not_a_made_up_number(self, tmp_path):
        (tmp_path / "ws").mkdir()
        (tmp_path / "ws" / "AGENTS.md").write_bytes(b"x\n" * 5000)
        app = _app(tmp_path, projects=[{
            "id": "ws", "name": "ws", "directory": str(tmp_path / "ws"), "note": ""}])
        rule = harness.view(app).projects[0].rule
        assert rule.lines is None, "只数了前 8KB 就不能报一个像全体行数的数"
        assert "只读了前" in rule.note

    def test_missing_file_is_a_fact_not_absence(self, tmp_path):
        (tmp_path / "ws").mkdir()
        app = _app(tmp_path, projects=[{
            "id": "ws", "name": "ws", "directory": str(tmp_path / "ws"), "note": ""}])
        rule = harness.view(app).projects[0].rule
        assert rule.exists is False
        assert "没有这个文件" in rule.note

    def test_project_without_directory_has_no_rule(self, tmp_path):
        app = _app(tmp_path, projects=[{
            "id": "p", "name": "p", "directory": "", "note": ""}])
        assert harness.view(app).projects[0].rule is None


class TestMemoryBlock:
    def test_exact_total_when_under_limit(self, tmp_path):
        app = _app(tmp_path, memory=_Memory([_entry(), _entry()]))
        view = harness.view(app)
        assert view.memory_total == 2
        assert len(view.memory) == 2

    def test_total_is_none_at_the_limit(self, tmp_path):
        entries = [_entry() for _ in range(harness.MEMORY_LIST_LIMIT)]
        view = harness.view(_app(tmp_path, memory=_Memory(entries)))
        assert view.memory_total is None
        assert any("比这更多" in n for n in view.notes)

    def test_failure_is_reported_not_zero(self, tmp_path):
        view = harness.view(_app(tmp_path, memory=_Memory(explode=True)))
        assert view.memory == []
        assert view.memory_total is None
        assert any("记忆读不出来" in n for n in view.notes)


class TestWorkload:
    def test_counts(self, tmp_path):
        view = harness.view(_app(tmp_path, tasks=_Tasks(n_open=3, n_closed=2),
                                promises=1, pending=4))
        assert view.workload == {"open_tasks": 3, "open_promises": 1, "pending": 4}

    def test_missing_key_is_not_zero(self, tmp_path):
        view = harness.view(_app(tmp_path, tasks=_Tasks(explode=True), promises=2))
        assert "open_tasks" not in view.workload, "读不出来不许给 0 —— 那会被读成「没有」"
        assert view.workload["open_promises"] == 2
        assert any("未结束事务数读不出来" in n for n in view.notes)


class TestSkillsAndSettings:
    def test_skills_says_not_available(self, tmp_path):
        skills = harness.view(_app(tmp_path)).skills
        assert skills["available"] is False
        assert "Skills" in skills["reason"]

    def test_settings_reflect_provider_key_and_mode(self, tmp_path):
        app = _app(tmp_path)
        settings = harness.view(app, mode="auto_dir").settings
        assert settings["provider"] == "RuleBasedProvider"
        assert settings["key_configured"] is False
        assert settings["mode"] == "auto_dir"
        assert settings["data_dir"] == str(tmp_path)

    def test_no_config_is_unknown_not_false(self):
        app = types.SimpleNamespace(llm_name="RuleBasedProvider", config=None,
                                    project_repo=_Projects([]),
                                    memory=_Memory(), task_repo=_Tasks(),
                                    promise=types.SimpleNamespace(open_count=lambda: 0),
                                    pending=types.SimpleNamespace(count=lambda: 0))
        assert harness.view(app).settings["key_configured"] is None


class TestReadOnlyDiscipline:
    def test_never_calls_today_view(self):
        """``today.view()`` 会顺延并在 ``task_records`` 留记录（V1.115 的坑）。

        模块 docstring 里**说明**了这件事，所以只查代码体，不查文档。
        """
        from pathlib import Path

        from freeagent.services import harness as module

        code = Path(module.__file__).read_text(encoding="utf-8")
        body = code.split('"""', 2)[2]   # 跳过模块 docstring
        assert "today.view" not in body

    def test_endpoint_is_registered(self):
        from freeagent.web.routes import READ_ROUTES

        assert "/api/harness" in READ_ROUTES


class TestFrontEndWiring:
    def test_assembled_into_page(self):
        from freeagent.web.page import INDEX_HTML

        assert "async function renderHarness" in INDEX_HTML
        assert "/api/harness" in INDEX_HTML

    def test_nav_entry_exists(self):
        from freeagent.web.js_nav import JS_NAV
        from freeagent.web.markup import PAGE_MARKUP

        assert 'data-view="harness"' in PAGE_MARKUP
        assert '"harness"' in JS_NAV

    def test_boot_dispatches_it(self):
        from freeagent.web.js_boot import JS_BOOT

        assert 'view === "harness"' in JS_BOOT
        assert "renderHarness(root)" in JS_BOOT

    def test_absent_count_is_rendered_as_unreadable(self):
        from freeagent.web.js_harness import JS_HARNESS

        assert "读不到" in JS_HARNESS
        assert "harnessNum" in JS_HARNESS
