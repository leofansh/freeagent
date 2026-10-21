"""待处理收集器（设计文档 11.18 / V1.50）。

## 它解决什么

GTD 五环里，``capture``（捕获）与 ``clarify``（澄清）之间原本是断的：
V1.48 让飞书把图片/文件**收下**而非丢弃，但收下之后就是终点 —— 字节躺在
``attachments/`` 里，没有队列、没有提醒、没有转换成事务的路。对忙碌的人，
「捕获了忘了」和「没捕获」一样常见，而它更隐蔽：没有任何报错会提醒你
「上周收的那张截图还没处理」。

本服务补上断掉的那环：捕获落成一条 :class:`~freeagent.domain.CapturedItem`，
``/pending`` 摆出来，``/pending N`` **由用户显式**澄清成事务。

## 三条纪律（本模块的存在理由）

1. **捕获不是事务**。收集器里的东西没有 title/role/state，不进
   ``/today``、不参与排序、不接顺延收口。它是什么，只有用户知道 ——
   所以「变成事务」这一步永远是一次显式动作，没有任何自动迁移。
   这与 11.9.10「非文字消息不建事务」是同一条线：捕获侧只收，澄清侧才建，
   而澄清的扳机在用户手里。
2. **不猜内容**。服务不解析文件名、不调 LLM 认图、不从字节里抽文字 ——
   「这张截图是什么」是用户的知识，不是程序的。provenance（来源注记）
   只记事实：谁收的、什么类型、原件在哪。
3. **FIFO 且可数**。``/today`` 只露一条有界提醒（「待处理 N 条」），
   细节去 ``/pending`` 看。收集器是**工作台**，不是又一块要盯的屏幕。

## 为什么澄清后进「默认脉络」

事务至少属于一个角色（``validate_task``），而捕获里没有角色信息。
猜一个角色是替用户归档；每次都问一句又太吵。折中：进
:data:`~freeagent.domain.DEFAULT_ROLE_NAME`（不存在则建），并在澄清成功的
回话里**说清落在哪**，附 ``/move`` 归位 —— 与 chat 路径「没有任何角色时
第一条进默认脉络」同一条惯例，可预期优先于聪明。
"""

from __future__ import annotations

from typing import Sequence

from ..domain import (
    DEFAULT_ROLE_NAME,
    CapturedItem,
    CapturedStatus,
    NotFoundError,
    Task,
    TaskKind,
    ValidationError,
)
from .clock import Clock
from .roles import RoleService
from .tasks import TaskService
from ..storage.repos import CapturedRepo

__all__ = ["PendingService"]

#: 自动标题的长度上限。文件名可能很长（浏览器下载的名字尤其），
#: 而标题要出现在 /today 的一行里 —— 超长会撑破版面。
_MAX_AUTO_TITLE = 60


class PendingService:
    """待处理收集器的读写与澄清。

    构造期注入四样东西：捕获仓储、事务服务、角色服务、时钟。
    **刻意不持锁**：锁在 ``App.lock``（进程级，SQLite 连接不能被两个线程
    同时用），由调用方按通道惯例持有 —— 飞书桥接在 ``_capture_unsupported``
    里持，CLI 单线程本来就不需要。服务自己拿锁会造成「锁住在服务里、
    调用方不知道」的假象。
    """

    def __init__(
        self,
        captured: CapturedRepo,
        tasks: TaskService,
        roles: RoleService,
        clock: Clock,
    ) -> None:
        self._captured = captured
        self._tasks = tasks
        self._roles = roles
        self._clock = clock

    # -- 捕获侧（飞书等入口调用） ------------------------------------------ #
    def add(
        self,
        *,
        source: str,
        kind: str,
        media_key: str,
        name: str,
        size: int,
        path: str,
    ) -> CapturedItem:
        """收下一条捕获。**只收，不建事务** —— 见模块文档纪律 1。

        下载失败的调用方把 ``path`` 指向它写的 ``.meta.json`` 引用文件；
        ``media_key`` 照记，凭它还能重新下载（这是引用不丢的关键）。
        """
        return self._captured.add(
            source=source,
            kind=kind,
            media_key=media_key,
            name=name,
            size=size,
            path=path,
            now=self._clock.now(),
        )

    # -- 视图 --------------------------------------------------------------- #
    def pending(self) -> tuple[CapturedItem, ...]:
        """待处理的全部，先进先出。"""
        return tuple(self._captured.list_pending())

    def count(self) -> int:
        """待处理条数。``/today`` 的「待处理 N 条」只用这一个数。"""
        return self._captured.count_pending()

    # -- 澄清侧（用户显式动作） -------------------------------------------- #
    def clarify(
        self,
        item_id: str,
        *,
        title: str | None = None,
        role_ids: Sequence[str] = (),
    ) -> Task:
        """把一条捕获**澄清成事务**。唯一建事务的入口，且必须由用户触发。

        重复澄清同一扇门**关死**：第二条会拿到 ValidationError 并被告知
        它早已变成哪条事务 —— 否则用户以为「又建了一条」，去 /today 看到
        两条一模一样的东西，查都不知从何查起。
        """
        item = self._captured.get(item_id)
        if item.status is not CapturedStatus.PENDING:
            if item.status is CapturedStatus.CLARIFIED and item.task_id:
                raise ValidationError(
                    f"这条待处理项已经澄清过了（事务 {item.task_id[:8]}）"
                )
            raise ValidationError("这条待处理项已经忽略过了，不能澄清")

        target_title = (title or "").strip() or self._auto_title(item)
        roles = tuple(role_ids) or self._default_role_ids()
        task = self._tasks.create(
            title=target_title,
            role_ids=roles,
            kind=TaskKind.ACTION,
            intent=self._provenance(item),
        )
        self._captured.resolve(
            item.id, CapturedStatus.CLARIFIED, self._clock.now(), task_id=task.id
        )
        return task

    def dismiss(self, item_id: str) -> CapturedItem:
        """明确说这条不要了。**可逆的对偶是「重新收下」—— 本版不提供**：

        忽略一条捕获的代价是「再想要得回飞书翻」，而翻得到；做「取消忽略」
        则要给收集器加一个回收站视图，那是另一块要维护的屏幕。先不做，
        并把代价写在这里（第十五章：决定不做连代价一起写）。
        """
        item = self._captured.get(item_id)
        if item.status is not CapturedStatus.PENDING:
            raise ValidationError("这条待处理项已经处理过了，不能重复忽略")
        return self._captured.resolve(
            item.id, CapturedStatus.DISMISSED, self._clock.now()
        )

    # -- 内部 --------------------------------------------------------------- #
    @staticmethod
    def _auto_title(item: CapturedItem) -> str:
        """没有显式标题时，从捕获本身推一个**诚实**的标题。

        文件有真名（``file_name``）就用真名；图片在飞书侧统一叫
        ``image``，那就如实说「一张图片」加时间戳 —— 不编名字。
        """
        name = (item.name or "").strip()
        if name and name != item.kind:
            return name[:_MAX_AUTO_TITLE]
        label = "图片" if item.kind == "image" else "文件"
        stamp = f"{item.created_at:%m-%d %H:%M}"
        return f"{label}（{stamp}）"

    @staticmethod
    def _provenance(item: CapturedItem) -> str:
        """来源注记：只记事实，不解读内容。

        它会成为事务的 ``intent`` 与 CREATED 记录的正文 —— 半年后回头看
        「这条事务哪来的」，答案必须还能找得到。
        """
        label = "图片" if item.kind == "image" else "文件"
        note = f"来自待处理收集器（{item.source} · {label}）"
        if item.path:
            note += f"；本地文件：{item.path}"
        else:
            note += f"；原件未下载，飞书引用 {item.media_key}"
        return note

    def _default_role_ids(self) -> tuple[str, ...]:
        """没指定角色时进「默认脉络」，不存在则建。

        与 chat 路径同惯例（见 ``ChatService._record``）：可预期 > 聪明。
        用户要归位，一条 ``/move <id> <角色>`` 的事。
        """
        for role in self._roles.list_roles():
            if role.name == DEFAULT_ROLE_NAME:
                return (role.id,)
        role = self._roles.create(DEFAULT_ROLE_NAME)
        return (role.id,)
