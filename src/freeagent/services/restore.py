"""恢复契约。

「可以从任何事务进入，恢复上下文」在 V1.0 只是愿望；
这里把它变成**可验证的结构**（设计文档第八章）：
打开事务必须返回 :class:`RestoreView`，缺任何一项都算恢复失败。
"""

from __future__ import annotations

from ..domain import (
    REMINDER_FIRED_WINDOW,
    Artifact,
    RecordType,
    RestoreView,
    Task,
    TaskKind,
    TaskState,
)
from ..storage.repos import RecordRepo
from .artifacts import ArtifactService
from .clock import Clock
from .roles import RoleService
from .tasks import TaskService

__all__ = ["RestoreService"]


class RestoreService:
    def __init__(
        self,
        tasks: TaskService,
        artifacts: ArtifactService,
        records: RecordRepo,
        roles: RoleService,
        clock: Clock,
    ) -> None:
        self._tasks = tasks
        self._artifacts = artifacts
        self._records = records
        self._roles = roles
        self._clock = clock

    # -- 主入口 ------------------------------------------------------------- #
    def open_task(
        self, task_id: str, *, mark_resumed: bool = True, limit: int = REMINDER_FIRED_WINDOW
    ) -> RestoreView:
        """打开事务并返回完整恢复视图。"""
        task = self._tasks.get(task_id)
        if mark_resumed and task.is_open:
            task = self._tasks.mark_resumed(task_id)
        return self.build_view(task, limit=limit)

    def build_view(self, task: Task, *, limit: int = REMINDER_FIRED_WINDOW) -> RestoreView:
        return RestoreView(
            task=task,
            effective_definition_of_done=self.effective_dod(task),
            current_artifact=self._artifacts.current(task.id),
            progress_note=task.progress_note,
            recent_records=self._records.recent(task.id, limit=limit),
            waiting_on=task.waiting_on,
            next_actions=self.next_actions(task),
        )

    def effective_dod(self, task: Task) -> str | None:
        """生效完成标准：事务级 → 角色默认值 → 未声明。"""
        if task.definition_of_done:
            return task.definition_of_done
        for role_id in task.role_ids:
            dod = self._roles.default_definition_of_done_for(role_id)
            if dod:
                return dod
        return None

    # -- 下一步建议 --------------------------------------------------------- #
    def next_actions(self, task: Task) -> tuple[str, ...]:
        """按状态分支生成（设计文档 8.2）。"""
        now = self._clock.now()
        today = self._clock.today()
        actions: list[str] = []

        # 提醒类到点
        if (
            task.kind is TaskKind.REMINDER
            and task.reminder_time is not None
            and task.reminder_time <= now
        ):
            actions.append("确认这条提醒是否还需要；要取消就销账")

        # 等候跟进
        waiting = task.waiting_on
        if waiting is not None and waiting.follow_up_at is not None:
            if waiting.follow_up_at <= now:
                actions.append(f"催一下 {waiting.who_or_what}")
            else:
                actions.append(
                    f"等 {waiting.who_or_what}；{waiting.follow_up_at:%m-%d %H:%M} 该跟进了"
                )

        # 草稿
        artifact = self._artifacts.current(task.id)
        if task.state is TaskState.ACTIVE and artifact is not None:
            label = "草稿" if artifact.status.value == "draft" else "已采纳稿"
            actions.append(f"{label}已到 version {artifact.version}：继续改，还是重写")
        elif task.state is TaskState.ACTIVE:
            actions.append("还没有草稿，可以说「先理一版」让助手起个骨架")
        elif artifact is not None and task.state is TaskState.INBOX:
            # 有草稿却还没开始做 —— 这是最常见的「东西在那儿躺着」状态
            label = "草稿" if artifact.status.value == "draft" else "已采纳稿"
            actions.append(
                f"已经有{label}（version {artifact.version}）但还没开始："
                f"说「开始做」或直接采纳"
            )

        # 意图 / 完成标准缺失
        if task.intent_pending:
            actions.append("先说清楚这件事想做到什么程度")
        if (
            self.effective_dod(task) is None
            and task.kind is TaskKind.ACTION
            and task.state in (TaskState.ACTIVE, TaskState.DONE)
        ):
            actions.append("先说清楚做到什么程度算完")

        # 逾期
        if task.scheduled_for is not None and task.scheduled_for < today:
            actions.append(f"原定 {task.scheduled_for}，已顺延；今天做还是改期")

        if not actions:
            actions.append("没有待处理的下一步，按你自己的节奏来")
        return tuple(actions)

    # -- 派生缓存重建 ------------------------------------------------------- #
    def rebuild(self, task_id: str) -> RestoreView:
        """从只追加日志完全重建恢复视图（``progress_note`` 视为不可信）。"""
        self._tasks.rebuild_progress_note(task_id)
        return self.build_view(self._tasks.get(task_id))

    def export_context(self, view: RestoreView) -> str:
        """把恢复视图渲染成可重新装载进助手的文本块。"""
        task = view.task
        lines = [
            f"事务：{task.title}",
            f"形状：{task.kind.label}｜状态：{task.state.label}",
            f"意图：{task.intent or '（未澄清）'}",
            f"完成标准：{view.effective_definition_of_done or '（未声明）'}",
        ]
        if view.waiting_on is not None:
            lines.append(f"等候：{view.waiting_on.describe()}")
        if view.current_artifact is not None:
            art: Artifact = view.current_artifact
            lines.append(f"当前稿：version {art.version}（{art.status.value}）")
        if view.progress_note:
            lines.append(f"进度：{view.progress_note}")
        if view.recent_records:
            lines.append("最近记录：")
            lines += [f"  - {r.ts:%m-%d %H:%M} {r.content}" for r in view.recent_records]
        lines.append("下一步建议：")
        lines += [f"  · {a}" for a in view.next_actions]
        return "\n".join(lines)

    def note(self, task_id: str, content: str) -> None:
        self._records.append(task_id, RecordType.NOTE, content, self._clock.now())
