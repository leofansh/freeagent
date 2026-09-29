"""角色合并重定向、草稿版本化、恢复契约。"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from freeagent.app import App
from freeagent.domain import (
    ArtifactStatus,
    ConflictError,
    TaskKind,
    TaskState,
    ValidationError,
)


# ----------------------------------------------------------------- role merge
def test_merge_repoints_tasks_atomically(app: App, roles):
    t = app.tasks.create("两边的事", [roles["work"].id, roles["family"].id])
    report = app.roles.merge(roles["family"].id, roles["work"].id)
    assert report.source_name == "家庭"
    assert t.id in report.moved_tasks
    assert app.task_repo.get(t.id).role_ids == (roles["work"].id,)


def test_merge_dedups_when_target_already_present(app: App, roles):
    t = app.tasks.create("只有家庭", [roles["family"].id])
    app.tasks.create("已有工作", [roles["work"].id, roles["family"].id])
    app.roles.merge(roles["family"].id, roles["work"].id)
    assert app.task_repo.get(t.id).role_ids == (roles["work"].id,)


def test_merge_preserves_order(app: App, roles):
    t = app.tasks.create("顺序", [roles["family"].id, roles["work"].id])
    app.roles.merge(roles["family"].id, roles["work"].id)
    assert app.task_repo.get(t.id).role_ids == (roles["work"].id,), "源在前时应被目标吸收到原位置"


def test_merge_sets_merged_into_and_resolves(app: App, roles):
    app.roles.merge(roles["family"].id, roles["work"].id)
    assert app.roles.get(roles["family"].id).merged_into == roles["work"].id
    assert app.roles.resolve(roles["family"].id).id == roles["work"].id


def test_merge_hides_source_from_listing(app: App, roles):
    app.roles.merge(roles["family"].id, roles["work"].id)
    names = [r.name for r in app.roles.list_roles()]
    assert "家庭" not in names
    assert "工作项目A" in names
    assert len(app.roles.list_roles(include_merged=True)) == 3


def test_merge_appends_role_change_record(app: App, roles):
    t = app.tasks.create("要合并的", [roles["family"].id])
    app.roles.merge(roles["family"].id, roles["work"].id)
    contents = [r.content for r in app.record_repo.list_for_task(t.id)]
    assert any("角色合并" in c and "家庭" in c and "工作项目A" in c for c in contents), contents


def test_merge_into_self_rejected(app: App, roles):
    with pytest.raises(ValidationError):
        app.roles.merge(roles["work"].id, roles["work"].id)


def test_merge_reversing_direction_is_a_noop(app: App, roles):
    """A→B 之后再 merge(B, A)：两端解析到同一个根，不应成环也不应搬数据。"""
    t = app.tasks.create("还挂着", [roles["family"].id])
    app.roles.merge(roles["family"].id, roles["work"].id)
    report = app.roles.merge(roles["work"].id, roles["family"].id)
    assert report.absorbed
    assert report.moved_tasks == ()
    assert app.task_repo.get(t.id).role_ids == (roles["work"].id,)


def test_resolve_detects_corrupt_cycle(app: App, roles):
    """成环的防线在 resolve（防脏数据），而不是在 merge。"""
    app.conn.execute(
        "UPDATE roles SET merged_into=? WHERE id=?", (roles["work"].id, roles["family"].id)
    )
    app.conn.execute(
        "UPDATE roles SET merged_into=? WHERE id=?", (roles["family"].id, roles["work"].id)
    )
    with pytest.raises(ValidationError):
        app.roles.resolve(roles["work"].id)


def test_merge_twice_is_idempotent(app: App, roles):
    app.roles.merge(roles["family"].id, roles["work"].id)
    second = app.roles.merge(roles["family"].id, roles["work"].id)
    assert second.absorbed
    assert second.moved_tasks == ()


def test_tasks_in_includes_merged_children(app: App, roles):
    app.tasks.create("家里的", [roles["family"].id])
    app.tasks.create("工作上的", [roles["work"].id])
    app.roles.merge(roles["family"].id, roles["work"].id)
    titles = {t.title for t in app.roles.tasks_in(roles["work"].id)}
    assert titles == {"家里的", "工作上的"}


def test_delete_merged_role_conflicts(app: App, roles):
    app.roles.merge(roles["family"].id, roles["work"].id)
    with pytest.raises(ConflictError):
        app.roles.delete(roles["family"].id)


def test_delete_role_with_tasks_conflicts(app: App, roles):
    app.tasks.create("还挂着", [roles["family"].id])
    with pytest.raises(ConflictError):
        app.roles.delete(roles["family"].id)


def test_delete_unused_role_ok(app: App, roles):
    app.roles.delete(roles["errand"].id)
    assert "跑腿杂项" not in [r.name for r in app.roles.list_roles()]


def test_role_name_unique(app: App, roles):
    with pytest.raises(ValidationError):
        app.roles.create("工作项目A")


def test_role_default_dod_used_as_fallback(app: App, roles):
    t = app.tasks.create("x", [roles["work"].id])
    view = app.restore.build_view(t)
    assert view.effective_definition_of_done == "先给我能用的就行"


def test_task_level_dod_overrides_role_default(app: App, roles):
    t = app.tasks.create("x", [roles["work"].id])
    t = app.tasks.set_definition_of_done(t.id, "能给领导看")
    assert app.restore.build_view(t).effective_definition_of_done == "能给领导看"


# ----------------------------------------------------------------- artifacts
def test_draft_starts_at_version_one(app: App, roles):
    t = app.tasks.create("周报", [roles["work"].id])
    art = app.artifacts.create_draft(t.id, "初稿", "内容")
    assert art.version == 1
    assert art.status is ArtifactStatus.DRAFT
    assert app.task_repo.get(t.id).current_artifact_id == art.id


def test_revise_creates_new_version_never_overwrites(app: App, roles):
    t = app.tasks.create("周报", [roles["work"].id])
    v1 = app.artifacts.create_draft(t.id, "初稿", "内容1")
    v2 = app.artifacts.revise(t.id, "内容2")
    assert v2.version == 2
    assert app.artifact_repo.get(v1.id).content == "内容1", "旧版必须保留"
    assert v2.supersedes is None or v2.supersedes == v1.id


def test_new_draft_supersedes_accepted(app: App, roles):
    t = app.tasks.create("周报", [roles["work"].id])
    v1 = app.artifacts.create_draft(t.id, "初稿", "内容1")
    app.artifacts.accept(v1.id)
    v2 = app.artifacts.revise(t.id, "内容2")
    assert v2.supersedes == v1.id
    assert app.artifact_repo.get(v1.id).status is ArtifactStatus.SUPERSEDED


def test_at_most_one_accepted(app: App, roles):
    t = app.tasks.create("周报", [roles["work"].id])
    v1 = app.artifacts.create_draft(t.id, "a", "1")
    v2 = app.artifacts.create_draft(t.id, "b", "2")
    app.artifacts.accept(v1.id)
    app.artifacts.accept(v2.id)
    accepted = [a for a in app.artifacts.list_for_task(t.id)
                if a.status is ArtifactStatus.ACCEPTED]
    assert len(accepted) == 1 and accepted[0].id == v2.id


def test_superseded_versions_are_never_deleted(app: App, roles):
    t = app.tasks.create("周报", [roles["work"].id])
    for i in range(4):
        app.artifacts.revise(t.id, f"内容{i}") if i else app.artifacts.create_draft(
            t.id, "t", "内容0"
        )
    assert [a.version for a in app.artifacts.list_for_task(t.id)] == [1, 2, 3, 4]


def test_reopen_draft_from_accepted(app: App, roles):
    t = app.tasks.create("周报", [roles["work"].id])
    v1 = app.artifacts.create_draft(t.id, "初稿", "内容1")
    app.artifacts.accept(v1.id)
    v2 = app.artifacts.reopen_draft(t.id)
    assert v2.version == 2 and v2.content == "内容1"


# ----------------------------------------------------------------- restore
def test_restore_view_is_complete(app: App, roles):
    t = app.tasks.create("周报", [roles["work"].id], intent="先理一版")
    app.tasks.start(t.id)
    app.artifacts.create_draft(t.id, "初稿", "# 框架")
    app.tasks.note(t.id, "有框架了")
    view = app.restore.open_task(t.id)
    assert view.task.id == t.id
    assert view.effective_definition_of_done == "先给我能用的就行"
    assert view.current_artifact is not None
    assert view.progress_note and "有框架了" in view.progress_note
    assert view.recent_records
    assert view.next_actions
    assert view.waiting_on is None


def test_restore_marks_resumed(app: App, roles):
    t = app.tasks.create("周报", [roles["work"].id])
    app.tasks.start(t.id)
    assert app.restore.open_task(t.id).task.last_resumed_at is not None


def test_restore_next_actions_mention_artifact_version(app: App, roles):
    t = app.tasks.create("周报", [roles["work"].id], intent="做出来")
    app.tasks.start(t.id)
    app.artifacts.create_draft(t.id, "初稿", "x")
    actions = app.restore.open_task(t.id).next_actions
    assert any("version 1" in a for a in actions), actions


def test_restore_next_actions_for_waiting(app: App, roles):
    from freeagent.domain import WaitingKind, WaitingOn

    now = app.clock.now()
    t = app.tasks.create(
        "等客户", [roles["work"].id], kind=TaskKind.WAIT,
        waiting_on=WaitingOn(WaitingKind.PERSON, "客户", now, follow_up_at=now),
    )
    actions = app.restore.open_task(t.id).next_actions
    assert any("催一下 客户" in a for a in actions), actions


def test_restore_next_actions_flag_artifact_without_starting(app: App, roles):
    """有草稿但还没开始做 —— 「东西在那儿躺着」是最常见的中断形态。"""
    t = app.tasks.create("周报", [roles["work"].id], intent="先理一版")
    app.artifacts.create_draft(t.id, "初稿", "x")
    assert t.state is TaskState.INBOX
    actions = app.restore.open_task(t.id).next_actions
    assert any("version 1" in a and "还没开始" in a for a in actions), actions


def test_restore_next_actions_for_accepted_draft_without_starting(app: App, roles):
    t = app.tasks.create("周报", [roles["work"].id], intent="先理一版")
    art = app.artifacts.create_draft(t.id, "初稿", "x")
    app.artifacts.accept(art.id)
    actions = app.restore.open_task(t.id).next_actions
    assert any("已采纳稿" in a for a in actions), actions


def test_restore_next_actions_flag_missing_intent(app: App, roles):
    t = app.tasks.create("周报", [roles["work"].id])
    actions = app.restore.open_task(t.id).next_actions
    assert any("想做到什么程度" in a for a in actions), actions


def test_restore_next_actions_flag_overdue(app: App, roles):
    t = app.tasks.create("周报", [roles["work"].id], intent="做出来")
    app.tasks.schedule(t.id, date(2026, 9, 20))
    app.today.rollover()
    t2 = app.tasks.get(t.id)
    t3 = app.tasks.schedule(t.id, date(2026, 9, 20))  # 再排回过去
    actions = app.restore.open_task(t3.id).next_actions
    assert any("已顺延" in a for a in actions), actions


def test_export_context_is_loadable_text(app: App, roles):
    t = app.tasks.create("周报", [roles["work"].id], intent="先理一版")
    app.tasks.start(t.id)
    app.artifacts.create_draft(t.id, "初稿", "内容")
    text = app.restore.export_context(app.restore.open_task(t.id))
    assert "事务：周报" in text
    assert "下一步建议" in text
    assert "完成标准" in text


def test_rebuild_ignores_corrupted_progress_note(app: App, roles):
    t = app.tasks.create("周报", [roles["work"].id], intent="做出来")
    app.tasks.start(t.id)
    app.tasks.note(t.id, "真实进度")
    app.task_repo.set_progress_note(t.id, "假的")
    view = app.restore.rebuild(t.id)
    assert "假的" not in (view.progress_note or "")
    assert "真实进度" in (view.progress_note or "")
