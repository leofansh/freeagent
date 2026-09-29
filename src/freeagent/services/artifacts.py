"""草稿产物服务：版本化 + 采纳语义。

规则见设计文档 5.2：
1. ``version`` 从 1 严格递增；
2. **不做原地覆写** —— 圈改/局部重做一律产生新版本；
3. 旧版永不删除；
4. 一个任务最多一个 ``ACCEPTED``。
"""

from __future__ import annotations

from ..domain import Artifact, ArtifactStatus, RecordType
from ..storage.repos import ArtifactRepo, RecordRepo, TaskRepo
from .clock import Clock

__all__ = ["ArtifactService"]


class ArtifactService:
    def __init__(
        self,
        artifacts: ArtifactRepo,
        tasks: TaskRepo,
        records: RecordRepo,
        clock: Clock,
    ) -> None:
        self._artifacts = artifacts
        self._tasks = tasks
        self._records = records
        self._clock = clock

    # -- 读取 --------------------------------------------------------------- #
    def list_for_task(self, task_id: str) -> list[Artifact]:
        return self._artifacts.list_for_task(task_id)

    def current(self, task_id: str) -> Artifact | None:
        """当前应呈现给用户的那一版。"""
        task = self._tasks.get(task_id)
        if task.current_artifact_id:
            found = self._artifacts.find(task.current_artifact_id)
            if found is not None:
                return found
        return self._artifacts.latest(task_id)

    def accepted(self, task_id: str) -> Artifact | None:
        return self._artifacts.current_accepted(task_id)

    # -- 写入 --------------------------------------------------------------- #
    def create_draft(self, task_id: str, title: str, content: str) -> Artifact:
        """新建一版草稿。

        ``supersedes`` 指向**上一版**（不论它是否已采纳），使版本链完整可回溯。
        若上一版已采纳，按规则 2 额外把它降为 ``SUPERSEDED``。
        """
        now = self._clock.now()
        previous_accepted = self._artifacts.current_accepted(task_id)
        previous_any = previous_accepted or self._artifacts.latest(task_id)
        supersedes = previous_any.id if previous_any is not None else None

        artifact = self._artifacts.add(
            task_id, title, content, now, supersedes=supersedes
        )
        if previous_accepted is not None:
            self._artifacts.set_status(
                previous_accepted.id, ArtifactStatus.SUPERSEDED, now
            )
            self._records.append(
                task_id,
                RecordType.ARTIFACT_SUPERSEDED,
                f"v{previous_accepted.version} 被 v{artifact.version} 取代",
                now,
            )
        self._records.append(
            task_id,
            RecordType.ARTIFACT_CREATED,
            f"草稿 v{artifact.version}：{title}",
            now,
        )
        self._tasks.set_current_artifact(task_id, artifact.id, now)
        return artifact

    def revise(
        self, task_id: str, content: str, title: str | None = None
    ) -> Artifact:
        """圈改 / 局部重做。**永远产生新版本**，不原地覆盖。"""
        latest = self._artifacts.latest(task_id)
        if latest is None:
            raise ValueError("还没有草稿可改，请先 create_draft")
        return self.create_draft(task_id, title or latest.title, content)

    def accept(self, artifact_id: str) -> Artifact:
        """采纳某一版，并把该任务下其它 ACCEPTED 降为 SUPERSEDED。"""
        now = self._clock.now()
        artifact = self._artifacts.get(artifact_id)
        for other in self._artifacts.list_for_task(artifact.task_id):
            if other.id == artifact.id:
                continue
            if other.status is ArtifactStatus.ACCEPTED:
                self._artifacts.set_status(other.id, ArtifactStatus.SUPERSEDED, now)
                self._records.append(
                    artifact.task_id,
                    RecordType.ARTIFACT_SUPERSEDED,
                    f"v{other.version} 被 v{artifact.version} 取代",
                    now,
                )
        accepted = self._artifacts.set_status(artifact_id, ArtifactStatus.ACCEPTED, now)
        self._records.append(
            artifact.task_id,
            RecordType.ARTIFACT_ACCEPTED,
            f"采纳 v{accepted.version}",
            now,
        )
        return accepted

    def reopen_draft(self, task_id: str) -> Artifact:
        """以当前 ACCEPTED 版为基准开新版本（保留旧版可回看）。"""
        accepted = self._artifacts.current_accepted(task_id)
        if accepted is None:
            raise ValueError("没有已采纳的版本可作为基准")
        return self.create_draft(task_id, accepted.title, accepted.content)
