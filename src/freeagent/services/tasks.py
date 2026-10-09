"""事务服务：创建、状态迁移、调度、角色归属。

承载设计文档第四章的全部规则：正交三字段、不变式、合法迁移表。
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from datetime import date, datetime
from typing import TypeVar, cast

from ..domain import (
    InvariantViolation,
    RecordType,
    Task,
    TaskKind,
    TaskState,
    ValidationError,
    WaitingOn,
    new_id,
)
from ..storage.repos import RecordRepo, TaskRepo
from .clock import Clock
from .recurrence import RecurrenceError, RecurrenceRule, parse_rule

__all__ = ["TaskService", "ALLOWED_TRANSITIONS", "validate_task"]

_T = TypeVar("_T")


def _replace(obj: _T, **changes: object) -> _T:
    return cast(_T, dataclasses.replace(obj, **changes))


#: 合法状态迁移表（设计文档 4.4）。刻意宽松，不做死板流转。
ALLOWED_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.INBOX: frozenset(
        {TaskState.ACTIVE, TaskState.BLOCKED, TaskState.DONE, TaskState.DROPPED}
    ),
    TaskState.ACTIVE: frozenset(
        {TaskState.INBOX, TaskState.BLOCKED, TaskState.DONE, TaskState.DROPPED}
    ),
    TaskState.BLOCKED: frozenset(
        {TaskState.ACTIVE, TaskState.INBOX, TaskState.DONE, TaskState.DROPPED}
    ),
    TaskState.DONE: frozenset({TaskState.ACTIVE, TaskState.INBOX}),
    TaskState.DROPPED: frozenset({TaskState.INBOX, TaskState.ACTIVE}),
}

#: 终态不受「等候类必须 BLOCKED」约束。
_TERMINAL: frozenset[TaskState] = frozenset({TaskState.DONE, TaskState.DROPPED})

#: 会出现在进度摘要里的记录类型。
_NOTE_WORTHY: frozenset[RecordType] = frozenset(
    {
        RecordType.NOTE,
        RecordType.STATUS_CHANGE,
        RecordType.KIND_CHANGE,
        RecordType.ROLE_CHANGE,
        RecordType.SCHEDULE_CHANGE,
        RecordType.ARTIFACT_CREATED,
        RecordType.ARTIFACT_ACCEPTED,
        RecordType.ROLLOVER,
        RecordType.PAUSE,
        RecordType.RESUME,
        RecordType.DELEGATION_RESET,
    }
)

_PROGRESS_TAIL = 5


def validate_task(task: Task) -> None:
    """校验设计文档 4.3 的全部不变式。"""
    if not task.title.strip():
        raise ValidationError("事务标题不能为空")
    if not task.role_ids:
        raise InvariantViolation("事务至少属于一个角色")
    if len(set(task.role_ids)) != len(task.role_ids):
        raise InvariantViolation(f"事务角色不可重复: {task.role_ids}")
    if task.state not in _TERMINAL and task.kind is TaskKind.WAIT:
        if task.state is not TaskState.BLOCKED:
            raise InvariantViolation("未结束的等候类事务必须处于 BLOCKED")
        if task.waiting_on is None:
            raise InvariantViolation("等候类事务必须提供 waiting_on")
    if task.state is TaskState.BLOCKED and task.waiting_on is None:
        raise InvariantViolation("BLOCKED 事务必须提供 waiting_on")
    if task.state in _TERMINAL and task.waiting_on is not None:
        raise InvariantViolation("已结束的事务不应保留 waiting_on")
    if task.state is TaskState.DONE and task.completed_at is None:
        raise InvariantViolation("DONE 事务必须有 completed_at")
    if task.state is TaskState.DROPPED and task.dropped_at is None:
        raise InvariantViolation("DROPPED 事务必须有 dropped_at")
    if task.state is not TaskState.DONE and task.completed_at is not None:
        raise InvariantViolation("非 DONE 事务不应有 completed_at")
    if task.state is not TaskState.DROPPED and task.dropped_at is not None:
        raise InvariantViolation("非 DROPPED 事务不应有 dropped_at")


class TaskService:
    def __init__(self, tasks: TaskRepo, records: RecordRepo, clock: Clock) -> None:
        self._tasks = tasks
        self._records = records
        self._clock = clock

    # -- 创建 --------------------------------------------------------------- #
    def create(
        self,
        title: str,
        role_ids: Sequence[str],
        *,
        kind: TaskKind = TaskKind.ACTION,
        intent: str | None = None,
        definition_of_done: str | None = None,
        scheduled_for: date | None = None,
        due_time: datetime | None = None,
        reminder_time: datetime | None = None,
        waiting_on: WaitingOn | None = None,
        blocked_by: Sequence[str] = (),
        task_id: str | None = None,
        project_path: str | None = None,
        delegate_chat_id: str | None = None,
        delegate_requested_by: str | None = None,
    ) -> Task:
        now = self._clock.now()
        state = TaskState.BLOCKED if kind is TaskKind.WAIT else TaskState.INBOX
        task = Task(
            id=task_id or new_id(),
            title=title.strip(),
            role_ids=tuple(role_ids),
            state=state,
            kind=kind,
            intent=intent,
            definition_of_done=definition_of_done,
            scheduled_for=scheduled_for,
            due_time=due_time,
            reminder_time=reminder_time,
            waiting_on=waiting_on,
            blocked_by=tuple(blocked_by),
            entered_at=now,
            created_at=now,
            updated_at=now,
            project_path=project_path,
            delegate_chat_id=delegate_chat_id,
            delegate_requested_by=delegate_requested_by,
        )
        validate_task(task)
        saved = self._tasks.add(task)
        detail = f"[{kind.label}] " + (intent or "（意图待澄清）")
        if project_path:
            detail += f"｜委派到 {project_path}"
            # 委派**必然**从 inbox 起步。闸门是「你 /start 过才派」，
            # 所以创建时就必须是未开始状态，不能一上来就能被派出去。
            if state is not TaskState.INBOX:  # pragma: no cover - 内部不变量
                raise InvariantViolation("委派事务必须以待办状态创建")
        self._records.append(saved.id, RecordType.CREATED, detail, now)
        return self._tasks.get(saved.id)

    # -- 状态迁移 ----------------------------------------------------------- #
    def set_state(
        self,
        task_id: str,
        new_state: TaskState,
        *,
        waiting_on: WaitingOn | None = None,
        reason: str | None = None,
    ) -> Task:
        current = self._tasks.get(task_id)
        if new_state is current.state:
            return current
        if new_state not in ALLOWED_TRANSITIONS[current.state]:
            raise ValidationError(
                f"不允许从「{current.state.label}」直接迁到「{new_state.label}」"
                f"（可迁往：{self._allowed_text(current.state)}）"
            )
        now = self._clock.now()

        target_waiting = waiting_on
        if new_state is TaskState.BLOCKED and target_waiting is None:
            target_waiting = current.waiting_on
        if new_state is not TaskState.BLOCKED:
            target_waiting = None

        # 离开 BLOCKED：等候类自动转动作类（设计文档 4.4）
        kind = current.kind
        if current.state is TaskState.BLOCKED and kind is TaskKind.WAIT:
            if new_state not in _TERMINAL:
                kind = TaskKind.ACTION

        candidate = _replace(
            current,
            state=new_state,
            kind=kind,
            waiting_on=target_waiting,
            completed_at=now if new_state is TaskState.DONE else None,
            dropped_at=now if new_state is TaskState.DROPPED else None,
        )
        validate_task(candidate)

        saved = self._tasks.update(candidate, now)

        detail = f"{current.state.label} → {new_state.label}"
        if reason:
            detail += f"：{reason}"
        if target_waiting is not None:
            detail += f"（等 {target_waiting.who_or_what}）"
        if current.state is TaskState.BLOCKED and current.waiting_on is not None:
            detail += f"；原等候：{current.waiting_on.describe()}"
        self._records.append(task_id, RecordType.STATUS_CHANGE, detail, now)

        if kind is not current.kind:
            self._records.append(
                task_id,
                RecordType.KIND_CHANGE,
                f"{current.kind.label} → {kind.label}（不再等，转为要做的动作）",
                now,
            )
        if new_state is TaskState.ACTIVE:
            self._records.append(task_id, RecordType.RESUME, "开始处理", now)

        self.rebuild_progress_note(task_id)
        return saved

    def start(self, task_id: str, reason: str | None = None) -> Task:
        return self.set_state(task_id, TaskState.ACTIVE, reason=reason)

    def pause(self, task_id: str, reason: str | None = None) -> Task:
        return self.set_state(task_id, TaskState.INBOX, reason=reason)

    def block(
        self, task_id: str, waiting_on: WaitingOn, reason: str | None = None
    ) -> Task:
        return self.set_state(
            task_id, TaskState.BLOCKED, waiting_on=waiting_on, reason=reason
        )

    def complete(self, task_id: str, reason: str | None = None) -> Task:
        return self.set_state(task_id, TaskState.DONE, reason=reason)

    def drop(self, task_id: str, reason: str | None = None) -> Task:
        return self.set_state(task_id, TaskState.DROPPED, reason=reason)

    def reopen(self, task_id: str) -> Task:
        return self.set_state(task_id, TaskState.INBOX, reason="重新打开")

    def _allowed_text(self, state: TaskState) -> str:
        targets = sorted(ALLOWED_TRANSITIONS[state], key=lambda s: s.value)
        return "、".join(s.label for s in targets)

    # -- 字段编辑 ----------------------------------------------------------- #
    def set_kind(
        self,
        task_id: str,
        kind: TaskKind | str,
        *,
        waiting_on: WaitingOn | None = None,
        reason: str | None = None,
    ) -> Task:
        """改事务形状。改成等候类必须同时给出在等什么。

        ``kind`` 收字符串（``TaskKind`` 是 ``str`` 基枚举，所以
        ``TaskKind("action")`` 天然可行）。**刻意在边界转换**，因为不转换
        会静默丢掉一条校验：

        ``kind is TaskKind.WAIT`` 对普通字符串 ``"wait"`` 是 **False**，
        于是「等候类必须给 waiting_on」那条不变量被**跳过**、``target`` 被置
        ``None``，然后一路带到 ``storage/repos.py`` 的 ``task.kind.value``
        才炸出 ``AttributeError: 'str' object has no attribute 'value'``。

        症状离病因隔了两层，而**类型注解当时说这里是 ``TaskKind``**，于是
        排查时被引向「枚举用错了」而不是「这函数没校验输入」。先转换，两件事
        一起消失：拿到的一定是枚举，非法值立刻在门口报出**能看懂**的话。

        顺带 ``strip().lower()``：CLI 那侧有 ``args[1].lower()``，但
        **服务层不该依赖调用方已经归一化过** —— 下一个 Web 入口不会记得做，
        而 ``"Action"`` 撞上 ``ValueError`` 的报错对用户毫无信息量。
        """
        if not isinstance(kind, TaskKind):
            kind = TaskKind(str(kind).strip().lower())  # 非法值在这里就抛
        current = self._tasks.get(task_id)
        now = self._clock.now()
        if kind is TaskKind.WAIT:
            target = waiting_on or current.waiting_on
            if target is None:
                raise InvariantViolation("改成等候类必须同时提供 waiting_on")
            state = (
                current.state if current.state in _TERMINAL else TaskState.BLOCKED
            )
        else:
            target = None
            state = current.state
            if state is TaskState.BLOCKED:
                state = TaskState.INBOX

        candidate = _replace(
            current, kind=kind, state=state, waiting_on=target
        )
        validate_task(candidate)
        saved = self._tasks.update(candidate, now)
        detail = f"{current.kind.label} → {kind.label}"
        if target is not None:
            detail += f"（等 {target.who_or_what}）"
        if reason:
            detail += f"：{reason}"
        self._records.append(task_id, RecordType.KIND_CHANGE, detail, now)
        self.rebuild_progress_note(task_id)
        return saved

    def set_intent(self, task_id: str, intent: str) -> Task:
        now = self._clock.now()
        current = self._tasks.get(task_id)
        saved = self._tasks.update(_replace(current, intent=intent), now)
        self._records.append(task_id, RecordType.NOTE, f"意图：{intent}", now)
        self.rebuild_progress_note(task_id)
        return saved

    def set_definition_of_done(self, task_id: str, dod: str | None) -> Task:
        now = self._clock.now()
        current = self._tasks.get(task_id)
        saved = self._tasks.update(_replace(current, definition_of_done=dod), now)
        self._records.append(task_id, RecordType.NOTE, f"完成标准：{dod or '（清空）'}", now)
        self.rebuild_progress_note(task_id)
        return saved

    def set_due_time(self, task_id: str, due_time: datetime | None) -> Task:
        now = self._clock.now()
        current = self._tasks.get(task_id)
        return self._tasks.update(_replace(current, due_time=due_time), now)

    def set_reminder_time(self, task_id: str, reminder_time: datetime | None) -> Task:
        now = self._clock.now()
        current = self._tasks.get(task_id)
        return self._tasks.update(_replace(current, reminder_time=reminder_time), now)

    def set_reminder_rule(
        self, task_id: str, rule: RecurrenceRule | None
    ) -> Task:
        """装/卸重复提醒规则，并把 ``reminder_time`` 对齐到下一次。

        规则存**原始 JSON 文本**（见 :class:`~freeagent.domain.models.Task`
        上 ``reminder_rule`` 的说明：domain 层零 I/O、零业务判断，解释留给
        services）。这里负责三件事：

        1. 规则为 ``None`` → **只清规则，不动时间**。留着时间当一次性提醒
           是合理的降级（用户可能只是暂时不重复了），而清掉时间会静默
           取消提醒 —— 那是另一种后果，得让用户显式说。
        2. 规则非空 → **立刻把 ``reminder_time`` 推到下一次**。
           不推的话，这条事务要么永远不响（时间在过去的空档），
           要么立刻响一次（时间还没到）—— 两种都是错的。
        3. 规则的时区**必填**，由 :class:`RecurrenceRule` 在构造时就校验。

        ``RecurrenceError`` 直接往上抛：规则配错是**配置错误**，
        该在设置时就说出来，不该表现成「提醒没响」。
        """
        now = self._clock.now()
        current = self._tasks.get(task_id)
        if rule is None:
            return self._tasks.update(
                _replace(current, reminder_rule=None), now
            )
        nxt = rule.next_after(now)
        return self._tasks.update(
            _replace(current, reminder_rule=rule.to_json(), reminder_time=nxt), now
        )

    def reminder_rule_of(self, task_id: str) -> RecurrenceRule | None:
        """读出规则并**校验**。规则坏了就抛，不返回半合法的东西。"""
        return parse_rule(self._tasks.get(task_id).reminder_rule)

    def set_blocked_by(self, task_id: str, blocked_by: Sequence[str]) -> Task:
        return self._tasks.set_dependencies(task_id, blocked_by)

    def set_followup(self, task_id: str, follow_up_at: datetime | None) -> Task:
        """更新等候件的跟进时间（驱动 OVERDUE_WAIT 信号，文档 9.5）。

        必须是 BLOCKED 事务才有意义；非等候件会先要求提供 ``waiting_on``。
        """
        now = self._clock.now()
        current = self._tasks.get(task_id)
        if current.waiting_on is None:
            raise InvariantViolation(
                f"「{current.title}」没有在等什么，无法设跟进时间（先 /wait <id> <等谁>）"
            )
        updated = _replace(
            current,
            waiting_on=_replace(current.waiting_on, follow_up_at=follow_up_at),
        )
        saved = self._tasks.update(updated, now)
        when = f"{follow_up_at:%Y-%m-%d %H:%M}" if follow_up_at else "（取消）"
        self._records.append(
            task_id, RecordType.NOTE, f"跟进 {current.waiting_on.who_or_what}：{when}", now
        )
        self.rebuild_progress_note(task_id)
        return saved

    def note(self, task_id: str, content: str) -> Task:
        now = self._clock.now()
        self._records.append(task_id, RecordType.NOTE, content, now)
        self.rebuild_progress_note(task_id)
        return self._tasks.get(task_id)

    # -- 委派：恢复入口 -------------------------------------------------------- #
    def reset_delegation(self, task_id: str, reason: str | None = None) -> Task:
        """落一条 :attr:`RecordType.DELEGATION_RESET`，让这条委派**可以重派**。

        ## 为什么需要这个方法（而不是让用户去 ``/note``）

        「派发前就被拒」（白名单不符 / 没有闸门）被判定侧**刻意不自动重派** ——
        改配置前重试多少次都是同一个结果，而 ``--watch`` 实测过这个坑：
        180 秒扫 60 轮、写 60 个产物版本，而真正的委派一条没干成。

        但那是「没改配置」的判定。**用户改好配置之后总得有办法让它重来**，
        而落库那句「可以 /note 记下原因后重派」是**空头承诺**：``note`` 不在
        terminal 集合里，对判定完全不可见，什么也重置不了。

        刻意**不复用** :meth:`note`：它没有任何校验，于是 ``/note`` 会变成
        一个隐形后门—— 而「用错入口就静默无效」正是这句话原本的病根。

        ## 校验：三条不许重派的情形，各给一句准确的话

        拒绝的理由必须**互相可区分**，否则用户不知道自己该做什么
        （同一个「不能重派」会让人以为是 bug）。

        函数内 import :func:`~freeagent.services.delegate.attempt_state` ——
        形状推导必须与判定侧**同一份实现**，否则「校验说能重派」与
        「判定说别派」会在真跑起来时才互相打架。
        """
        from .delegate import attempt_state

        current = self._tasks.get(task_id)
        if not current.project_path:
            raise ValidationError(
                f"「{current.title}」不是委派事务，没有东西可重派"
                "（委派是有项目路径的事务）"
            )

        in_flight, last = attempt_state(self._records.list_for_task(task_id))
        if in_flight:
            raise ValidationError(
                f"「{current.title}」正在跑，不能重派 —— 派第二个进程去改同一个"
                "项目目录会互相覆盖。等它跑完，或者先 /pause 把它移出来。"
            )
        if last == "succeeded":
            raise ValidationError(
                f"「{current.title}」已经做完了，重派没有意义"
                "（要再做一遍的话，这是件新事，建条新事务）"
            )
        if last is None:
            raise ValidationError(
                f"「{current.title}」还没派出去过，不需要重派"
                "（执行器下一次扫到它就会派）"
            )

        now = self._clock.now()
        why = reason.strip() if reason and reason.strip() else "未说明原因"
        self._records.append(
            task_id,
            RecordType.DELEGATION_RESET,
            f"要求重新派发（上次结局：{last}）：{why}",
            now,
        )
        self.rebuild_progress_note(task_id)
        return self._tasks.get(task_id)

    # -- 调度 --------------------------------------------------------------- #
    def schedule(self, task_id: str, day: date) -> Task:
        now = self._clock.now()
        current = self._tasks.get(task_id)
        previous = current.scheduled_for
        saved = self._tasks.set_schedule(task_id, day, now)
        self._records.append(
            task_id,
            RecordType.SCHEDULE_CHANGE,
            f"排期：{previous or '未排'} → {day}",
            now,
        )
        self.rebuild_progress_note(task_id)
        return saved

    def unschedule(self, task_id: str) -> Task:
        """移出今天视图。**不改变 state** —— 进行中的事务也允许被移出。"""
        now = self._clock.now()
        current = self._tasks.get(task_id)
        if current.scheduled_for is None:
            return current
        saved = self._tasks.set_schedule(task_id, None, now)
        self._records.append(
            task_id,
            RecordType.SCHEDULE_CHANGE,
            f"移出排期（原 {current.scheduled_for}）",
            now,
        )
        self.rebuild_progress_note(task_id)
        return saved

    # -- 角色归属 ----------------------------------------------------------- #
    def set_roles(self, task_id: str, role_ids: Sequence[str]) -> Task:
        now = self._clock.now()
        current = self._tasks.get(task_id)
        before = set(current.role_ids)
        saved = self._tasks.replace_roles(task_id, role_ids, now)
        after = set(saved.role_ids)
        added = sorted(after - before)
        removed = sorted(before - after)
        if added or removed:
            self._records.append(
                task_id,
                RecordType.ROLE_CHANGE,
                f"角色：+{added or '无'} / -{removed or '无'}",
                now,
            )
            self.rebuild_progress_note(task_id)
        return saved

    def add_role(self, task_id: str, role_id: str) -> Task:
        current = self._tasks.get(task_id)
        if role_id in current.role_ids:
            return current
        return self.set_roles(task_id, (*current.role_ids, role_id))

    def remove_role(self, task_id: str, role_id: str) -> Task:
        current = self._tasks.get(task_id)
        remaining = tuple(r for r in current.role_ids if r != role_id)
        if not remaining:
            raise InvariantViolation("事务至少属于一个角色，不能移除最后一个")
        return self.set_roles(task_id, remaining)

    def move_to_role(self, task_id: str, role_id: str) -> Task:
        """快捷操作：只属于目标角色。"""
        return self.set_roles(task_id, (role_id,))

    # -- 恢复点 ------------------------------------------------------------- #
    def mark_resumed(self, task_id: str) -> Task:
        now = self._clock.now()
        self._records.append(task_id, RecordType.RESUME, "恢复上下文", now)
        updated = self._tasks.touch_resumed(task_id, now)
        self.rebuild_progress_note(task_id)
        return updated

    def rebuild_progress_note(self, task_id: str) -> str | None:
        """从只追加日志重建 ``progress_note``。

        ``progress_note`` 是**派生缓存**，不是真源（设计文档 5.1）。
        重建不更新 ``updated_at`` —— 见 ``TaskRepo.set_progress_note`` 的说明。
        """
        records = self._records.list_for_task(task_id)
        notable = [r for r in records if r.type in _NOTE_WORTHY]
        if not notable:
            self._tasks.set_progress_note(task_id, None)
            return None
        tail = notable[-_PROGRESS_TAIL:]
        note = "；".join(f"{r.ts:%m-%d %H:%M} {r.content}" for r in tail)
        self._tasks.set_progress_note(task_id, note)
        return note

    # -- 查询 --------------------------------------------------------------- #
    def get(self, task_id: str) -> Task:
        return self._tasks.get(task_id)

    def list_all(self) -> list[Task]:
        return self._tasks.list_all()

    def list_open(self) -> list[Task]:
        return [t for t in self._tasks.list_all() if t.is_open]
