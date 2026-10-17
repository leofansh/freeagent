"""产物验收的**写**端点：把「已验收」这件事从终端搬到两个界面。

## 为什么要有这个文件（2026-10-17）

委派闭环本来是断的，而断点很隐蔽：执行器交回产出后落成 ``DRAFT``，
:mod:`freeagent.delegate` 在任务上写「执行器已产出结果，待你验收」，
飞书也照这句话推卡 —— **但 ``ArtifactService.accept`` 的唯一调用点在
终端**（``cli/app.py::_cmd_accept``）。于是承诺在飞书、兑现要去另一个
终端，任务永远悬在「待验收」。

它同时撞上两条自己的规矩：

* **12.1.1 一份能力一份实现** —— 验收这件事在三个界面各做一遍，
  迟早漂移；而漂移的方向是「网页上按钮��� 404」。
* **本仓最贵的���条验证纪律**（``4657ac0`` 记的那条）：**HTTP 成功
  ≠ 用户能看到**。一个只有终端能点的按钮，等于没有。

## 为什么不直接复用 ``/api/task/<id>/<action>``

因为 ``STATE_ACTIONS`` 是**状态迁移**白名单，而采纳不改 ``state`` ——
它改的是 ``artifacts.status``。把 ``accept`` 塞进那个白名单会让
``ACTION_FOR_STATE`` 之类的映射含义变浑（「合法迁入」里混进一个
不迁入的动作）。分文件、分开命名空间，是为了让两套语义各自成立。

## 动作只有采纳，不做「拒绝」

产出不合意时，用户要的是**改**，不是「标记为不合格」——
而「改」在委派语境下的载体是圈改重做（``revise``，产生新版本），
不是把这一版判死。所以本端点只提供 ``accept`` 与 ``reopen``：

* ``accept``：采纳当前版（终端 ``/accept`` 的同一条路）
* ``reopen``：以已采纳版为基准开新版本（终端 ``/draft`` 的同一条路），
  「再来一版」比「标记失败」更贴近用户的真实动作

真要判死产出，用 ``/drop`` 把任务放下 —— 那是任务层的事，
不该在产物层另造一个「不合格」状态。
"""

from __future__ import annotations

from typing import Any

from ..app import App
from ..domain import ArtifactStatus, FreeAgentError
from .endpoints_read import resolve_task

__all__ = ["artifact_accept", "artifact_reopen"]


def _require_draft(app: App, task_id: str):
    """取当前该验收的那一版，并判它**现在能不能被验收**。

    三种不能，各给一句不同的话（理由同
    :func:`~freeagent.web.endpoints_delegate_action._require_decidable`）：
    「没有产出」「已经采纳过了」混成一句「没写进去」，
    用户看到的就是「这个按钮坏了」。
    """
    task_id = resolve_task(app, task_id)
    artifact = app.artifacts.current(task_id)
    if artifact is None:
        raise FreeAgentError("这条还没有产出可验收 —— 先派一次委派。")
    if artifact.status is ArtifactStatus.ACCEPTED:
        raise FreeAgentError(
            f"v{artifact.version} 已经验收过了 —— 已采纳的版本不需要再采纳一次。"
        )
    return task_id, artifact


def artifact_accept(app: App, task_id: str) -> dict[str, Any]:
    """``POST /api/task/<id>/accept`` —— 采纳当前版产出。

    刻意**直接调服务层**、不合成 ``/accept`` 文本命令：网页有 id，
    走命令反而要再解析一次前缀，且命令路径的白名单与本端点无关 ——
    两处各判一次，迟早漂移（这正是 ``endpoints_delegate_action``
    开头警告的那件事的反面：那里是**必须**复用，这里是**不该**绕）。
    """
    task_id, artifact = _require_draft(app, task_id)
    accepted = app.artifacts.accept(artifact.id)
    return {
        "ok": True,
        "task_id": task_id,
        "artifact_id": accepted.id,
        "version": accepted.version,
        "status": accepted.status.value,
        "message": f"已验收 v{accepted.version}",
    }


def artifact_reopen(app: App, task_id: str) -> dict[str, Any]:
    """``POST /api/task/<id>/reopen`` —— 以已采纳版为基准开新版本。

    「再来一版」的载体。刻意**不是**「标记这版不合格」：产物层
    没有那个状态，凭空加一个会让 ``ArtifactStatus`` 多出一个
    永远没人消费的值。
    """
    task_id = resolve_task(app, task_id)
    accepted = app.artifacts.accepted(task_id)
    if accepted is None:
        raise FreeAgentError(
            "还没有已采纳的版本可作基准 —— 先验收一版，再出新版本。"
        )
    draft = app.artifacts.reopen_draft(task_id)
    return {
        "ok": True,
        "task_id": task_id,
        "artifact_id": draft.id,
        "version": draft.version,
        "status": draft.status.value,
        "message": f"开了新版本 v{draft.version}，以 v{accepted.version} 为基准",
    }