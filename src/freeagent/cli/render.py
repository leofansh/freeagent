"""视图渲染。

两条硬性要求（设计文档第七章）：
* 每条排序信号必须把**理由**一起展示；
* 输出必须标注「启发式提示，不是评分」。
"""

from __future__ import annotations

from collections.abc import Iterable

from ..domain import RestoreView, Role, ScoredTask, Task
from ..services.llm import SignalRef, TaskRef
from ..services.reminders import ReminderDigest
from ..services.sorting import DISCLAIMER_MARKER
from ..services.today import TodayView

__all__ = [
    "DISCLAIMER",
    "role_names",
    "render_today",
    "render_task",
    "render_roles",
    "render_role_detail",
    "render_all",
    "render_digest",
    "render_suggestion",
    "render_artifact",
    "render_artifact_list",
    "to_task_ref",
    "to_signal_refs",
]

#: 与排序层共用同一个常量，避免文案漂移。
DISCLAIMER = DISCLAIMER_MARKER

_ARTIFACT_LABELS = {"draft": "草稿", "accepted": "已采纳", "superseded": "已被取代"}


def role_names(roles: Iterable[Role]) -> dict[str, str]:
    """id → 显示名。唯一实现在 ``web.serialize``，这里转出去避免两份实现。"""
    from ..web.serialize import role_names as _impl

    return _impl(roles)


def _role_label(task: Task, names: dict[str, str]) -> str:
    return "【" + "、".join(names.get(rid, "?") for rid in task.role_ids) + "】"


def _signal_text(scored: ScoredTask) -> str:
    if not scored.signals:
        return "无命中信号"
    return "；".join(s.reason for s in scored.signals)


def _one_line(index: int, scored: ScoredTask, names: dict[str, str]) -> str:
    task = scored.task
    head = f"{index}. {_role_label(task, names)}{task.title}"
    meta = f"{task.kind.label} · {task.state.label}"
    if task.scheduled_for is not None:
        meta += f" · 排 {task.scheduled_for}"
    return f"{head}\n     {meta}  [{scored.total_weight} 分]  {_signal_text(scored)}"


def render_today(view: TodayView, names: dict[str, str]) -> str:
    summary = view.rollover.summary()
    rolled = view.rolled_over_ids
    lines: list[str] = []
    if summary:
        lines.append(f"顺延：{summary}")
        lines.append("")
    lines.append(f"今天 · {view.day} · {len(view.items)} 件事（{DISCLAIMER}）")
    if not view.items:
        lines.append("今天没有排进来的事务。直接说一句话就能新建。")
        return "\n".join(lines)

    lines.append("")
    for index, scored in enumerate(view.items, start=1):
        mark = "  ⟲顺延" if scored.task.id in rolled else ""
        lines.append(_one_line(index, scored, names) + mark)
    lines.append("")
    lines.append("助手不替你决定先做哪个。用 /task <id> 打开某一条。")
    return "\n".join(lines)


def render_all(
    scored_list: list[ScoredTask], names: dict[str, str], scope: str = "open"
) -> str:
    titles = {
        "open": "全部未结束事务",
        "closed": "已结束事务",
        "all": "全部事务",
    }
    head = titles.get(scope, "全部事务")
    lines = [f"{head} · {len(scored_list)} 条（{DISCLAIMER}）"]
    if not scored_list:
        lines.append("（空）")
        return "\n".join(lines)
    lines.append("")
    for index, scored in enumerate(scored_list, start=1):
        lines.append(_one_line(index, scored, names))
    return "\n".join(lines)


def render_artifact(artifact) -> str:
    """单个草稿版本。"""
    label = _ARTIFACT_LABELS.get(artifact.status.value, artifact.status.value)
    lines = [f"version {artifact.version}（{label}）· {artifact.title}"]
    if artifact.supersedes:
        lines.append(f"  上一版：{artifact.supersedes[:8]}")
    lines.append("")
    lines += [f"  {line}" for line in artifact.content.splitlines()]
    return "\n".join(lines)


def render_artifact_list(artifacts) -> str:
    """版本一览。旧版永不删除，所以这里能看到完整链条。"""
    lines = [f"草稿版本（{len(artifacts)} 个，旧版全部保留）："]
    for artifact in artifacts:
        label = _ARTIFACT_LABELS.get(artifact.status.value, artifact.status.value)
        first_line = artifact.content.splitlines()[0] if artifact.content else ""
        lines.append(
            f"  v{artifact.version}  [{label}]  {artifact.created_at:%m-%d %H:%M}"
            f"  {first_line[:40]}"
        )
    lines.append("")
    lines.append("  /artifact <id> <版本> 看全文，/accept <id> <版本> 采纳")
    return "\n".join(lines)


def render_roles(roles: list[Role], names: dict[str, str] | None = None) -> str:
    lines = ["角色脉络："]
    if not roles:
        lines.append("（还没有角色。直接说一句话，我会问你是哪个脉络）")
        return "\n".join(lines)
    for role in roles:
        mark = "" if role.active else "  [静置]"
        dod = (
            f"｜默认完成标准：{role.default_definition_of_done}"
            if role.default_definition_of_done
            else ""
        )
        note = f"｜{role.note}" if role.note else ""
        merged = f"｜已合并进 {names[role.merged_into]}" if role.merged_into and names else ""
        lines.append(f"  · {role.name}{mark}{note}{dod}{merged}")
        lines.append(f"    id={role.id}")
    return "\n".join(lines)


def render_role_detail(role: Role, tasks: list[Task], names: dict[str, str]) -> str:
    lines = [f"角色：{role.name}"]
    if role.note:
        lines.append(f"  沉淀：{role.note}")
    if role.default_definition_of_done:
        lines.append(f"  默认完成标准：{role.default_definition_of_done}")
    if role.merged_into:
        lines.append(f"  已合并进：{names.get(role.merged_into, role.merged_into)}")
    lines.append("")
    if not tasks:
        lines.append("（这个角色下还没有事务）")
    else:
        for task in tasks:
            lines.append(
                f"  · {task.title}  [{task.kind.label} · {task.state.label}]  id={task.id}"
            )
    return "\n".join(lines)


def render_task(view: RestoreView, names: dict[str, str]) -> str:
    """恢复契约的人读版本。"""
    task = view.task
    lines = [f"{task.title}  ·  {task.kind.label} · {task.state.label}"]
    lines.append(f"角色：{'、'.join(names.get(rid, '?') for rid in task.role_ids)}")
    lines.append(f"意图：{task.intent or '（未澄清）'}")
    lines.append(f"生效完成标准：{view.effective_definition_of_done or '（未声明）'}")
    if view.waiting_on is not None:
        lines.append(f"等候：{view.waiting_on.describe()}")
    if view.current_artifact is not None:
        art = view.current_artifact
        label = _ARTIFACT_LABELS.get(art.status.value, art.status.value)
        lines.append(f"当前稿：version {art.version}（{label}）")
        for line in art.content.splitlines()[:12]:
            lines.append(f"    {line}")
    if view.progress_note:
        lines.append(f"进度：{view.progress_note}")
    if view.recent_records:
        lines.append("最近记录：")
        for record in view.recent_records[-8:]:
            lines.append(f"  · {record.ts:%m-%d %H:%M} {record.content}")
    lines.append("下一步：")
    for action in view.next_actions:
        lines.append(f"  · {action}")
    return "\n".join(lines)


def render_digest(digest: ReminderDigest) -> str:
    return digest.text() or ""


def render_suggestion(text: str) -> str:
    return f"排期参考（{DISCLAIMER}）：{text}"


def to_task_ref(task: Task) -> TaskRef:
    return TaskRef(
        id=task.id,
        title=task.title,
        intent=task.intent,
        kind=task.kind.value,
        definition_of_done=task.definition_of_done,
    )


def to_signal_refs(scored: ScoredTask) -> list[SignalRef]:
    return [
        SignalRef(code=s.code.value, weight=s.weight, reason=s.reason)
        for s in scored.signals
    ]
