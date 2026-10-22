"""对外承诺与关系对象（设计文档 11.19 / 19.5.3 / 20.1.5 / V1.51）。

## 它解决什么

9.1 的 ``WaitingOn`` 只记了**一半**：**我等别人**。反向那一半整个缺 ——
**我答应别人**。

缺它有三重代价：

1. **违约有社会代价，而自己的截止日没有**。我把自己的活儿往后挪一天，
   没人因此受损；我答应客户周三交报价，改到周五，客户会受损 ——
   关系受损、信任受损。所以它的排序权重必须**高于** ``DEADLINE_RISK``（11.19.4）。
2. **它很可能根本不在库里**。答应别人的事往往只存在于**对方脑子里**
   （「周三给你」这句话说完就散了）。系统不记，它就等于没发生过 ——
   而系统恰恰是最该记住「我说过什么」的地方。
3. **关系不可检索**。``WaitingOn.who_or_what`` 是自由文本，
   「在等张总」与「在等张总回合同」都是它；你想问「我跟张总往来过几次」，
   得人工翻所有事务的文本再认。关系对象把这一维变成**可检索的**。

## 四条纪律（本模块的存在理由）

1. **只显式触发，绝不静默推断**。从对话里自动抽「你答应了什么」是**弱信号**
   （不可复核、代价不对称），与 11.10.5「绝不自动触发委派」同型。所以
   承诺**只**由用户显式动作产生 —— ``/promise`` 或一次明确确认。
   **宁可漏，不可擅自动手**（第十五章）。
2. **承诺必须有兑现物**。一条承诺对应一个兑现物（事务），一对一。
   没有兑现物的承诺是空头承诺 —— 记下来也无处可查、无从兑现。
3. **过了日期不等于违约**。系统只摆事实：到期了就摆「已过期」，
   **判「违约」是替用户下结论**。三个终态（已兑现 / 已违约 / 已收回）
   全部只由用户显式动作产生（11.19.5）。
4. **关系与脉络是两个维度**。角色回答「这件事属于哪一块」（工作 / 家庭），
   关系对象回答「我跟谁之间有这笔账」。两者正交：一条「给张总交付报价」
   既属于「工作」角色，也指向「张总」。把关系塞进角色会毁掉角色的语义。

## 为什么不自动建事务

承诺**可以**顺手建一条事务（兑现物），但那是 ``/promise`` 命令的**显式**
副作用 —— 用户敲了命令就是要「记一条承诺」，而承诺得有兑现物。
**捕获侧绝不建事务**那条纪律（11.9.10 / 11.18.3）说的是「收到东西别
自动变成事务」，与这里不冲突：这里是用户明确要求记一笔。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from ..domain import (
    Contact,
    ContactKind,
    NotFoundError,
    Promise,
    PromiseStatus,
    Task,
    TaskKind,
    ValidationError,
    WaitingOn,
)
from ..storage.repos import ContactRepo, PromiseRepo
from .clock import Clock
from .roles import RoleService
from .tasks import TaskService

__all__ = [
    "PromiseService",
    "ContactLedger",
    "LedgerPromise",
    "LedgerWaiting",
]


@dataclass(frozen=True, slots=True)
class LedgerPromise:
    """「我欠对方」这一侧：一条**未兑现**承诺 + 它挂的兑现物。"""

    promise: Promise
    task_title: str
    task_id: str


@dataclass(frozen=True, slots=True)
class LedgerWaiting:
    """「我在等对方」这一侧：一条**未结束**等候 + 它挂的事务。"""

    task: Task
    waiting: WaitingOn


@dataclass(frozen=True, slots=True)
class ContactLedger:
    """一个关系对象名下的「一页账」（20.1.5 的最后一步）。

    两半合起来才是完整的账：``promises``（**我欠对方**）与 ``waitings``
    （**我在等对方**）。缺一半就还得靠人眼翻事务文本 —— 那正是
    关系一等化要消灭的认知成本（11.19.3）。
    """

    contact: Contact
    promises: list[LedgerPromise]
    waitings: list[LedgerWaiting]

    @property
    def is_empty(self) -> bool:
        """这笔账**清**了：既不欠对方、也不在等对方。"""
        return not self.promises and not self.waitings

#: 承诺陈述的长度上限。它要出现在 ``/today`` 的一行里 —— 超长会撑破版面。
_MAX_STATEMENT = 80


class PromiseService:
    """关系对象与对外承诺的读写。

    构造期注入五样东西：关系仓储、承诺仓储、事务服务、角色服务、时钟。
    **刻意不持锁**：锁在 ``App.lock``（进程级），由调用方按通道惯例持有
    —— 与 :class:`~freeagent.services.pending.PendingService` 同一条理由。
    """

    def __init__(
        self,
        conn,
        contacts: ContactRepo,
        promises: PromiseRepo,
        tasks: TaskService,
        roles: RoleService,
        clock: Clock,
    ) -> None:
        self._conn = conn
        self._contacts = contacts
        self._promises = promises
        self._tasks = tasks
        self._roles = roles
        self._clock = clock

    # -- 关系对象 ----------------------------------------------------------- #

    def contact(
        self,
        name: str,
        *,
        kind: ContactKind = ContactKind.PERSON,
        note: str | None = None,
    ) -> Contact:
        """按名字取关系对象，**没有就建一个**。

        「没有就建」是对的：关系对象是**用户不必操心**的记账层 ——
        他说「我答应张总」，系统该知道张总是谁，而不该反问一句
        「张总是谁」。名字重复时**复用**同一个（同名即同人），
        这是本仓一贯的「可预期优先于聪明」。
        """
        clean = name.strip()
        if not clean:
            raise ValidationError("关系对象的名字不能为空")
        existing = self._contacts.get_by_name(clean)
        if existing is not None:
            return existing
        return self._contacts.add(clean, self._clock.now(), kind=kind, note=note)

    def contacts(self) -> list[Contact]:
        """全部关系对象（创建序）。"""
        return self._contacts.list_all()

    def contact_count(self) -> int:
        return self._contacts.count()

    def find_contact(self, name: str) -> Contact | None:
        """按名字找，**找不到返回 None**（不建）。给「查一下」用。"""
        return self._contacts.get_by_name(name.strip())

    def set_contact_note(self, contact_id: str, note: str | None) -> Contact:
        """给关系对象写自由文本（「怎么找他」「答应了什么」）。

        结构化不进来的部分老实放在这里 —— 不假装它能进字段
        （与 11.19.2 同一条纪律）。
        """
        return self._contacts.set_note(contact_id, note, self._clock.now())

    def ledger(self, contact: Contact) -> ContactLedger:
        """这个关系对象名下**未结清的账**（设计文档 20.1.5 的最后一步）。

        「一页账」的两半：**我欠对方的**（未兑现承诺）与**我在等对方的**
        （未结束等候）。这是「见这个人之前」最该先看到的一页 ——
        它正面攻击 1.1 定义的核心痛点：在**人**之间切换时重新装载上下文。

        ``promises`` 只收未兑现的：已结清的账不该再占一页账的版面
        （那是「不重复打扰」，不是「删掉历史」—— 历史仍在库里可回放）。
        只摆事实，不判违约、不催 —— 见模块纪律 3 / 4。
        """
        promises = [
            LedgerPromise(
                promise=p,
                task_title=self._tasks.get(p.task_id).title,
                task_id=p.task_id,
            )
            for p in self._promises.list_for_contact(contact.id)
            if p.is_open
        ]
        waitings = [
            LedgerWaiting(task=t, waiting=t.waiting_on)
            for t in self._tasks.list_open()
            if t.waiting_on is not None and t.waiting_on.contact_id == contact.id
        ]
        return ContactLedger(contact=contact, promises=promises, waitings=waitings)

    # -- 承诺 --------------------------------------------------------------- #

    def promise(
        self,
        statement: str,
        *,
        contact_name: str | None = None,
        promised_for: datetime | None = None,
        role_ids: Sequence[str] = (),
        task_id: str | None = None,
        note: str | None = None,
    ) -> Promise:
        """记一条对外承诺。

        **必须给兑现物**：要么给 ``task_id``（挂到已有事务上），
        要么给 ``title`` 建一条新的。这里收的是 ``task_id``，
        建事务由调用方（CLI 命令）显式做 —— 服务层不替用户决定
        「这条承诺要不要变成一条待办」。

        ``role_ids`` 只在**新建兑现物**时用；挂已有事务时忽略
        （那条事务已经有脉络了）。
        """
        clean = statement.strip()
        if not clean:
            raise ValidationError("承诺不能是空的一句话")
        if len(clean) > _MAX_STATEMENT:
            raise ValidationError(
                f"承诺太长（{len(clean)} 字），限 {_MAX_STATEMENT} 字 —— "
                "它是给列表看的一行，不是一段话"
            )

        if task_id is None:
            raise ValidationError("承诺要有兑现物：给一条事务 id，或先建一条事务")

        # 兑现物必须真的存在 —— 承诺指向一条不存在的事务，等于指向虚空
        self._tasks.get(task_id)

        contact: Contact | None = None
        if contact_name:
            contact = self.contact(contact_name)

        return self._promises.add(
            task_id=task_id,
            statement=clean,
            now=self._clock.now(),
            contact_id=contact.id if contact else None,
            promised_for=promised_for,
            note=note,
        )

    def open_promises(self) -> list[Promise]:
        """未兑现的承诺，按承诺日期升序（最早到期的前面）。"""
        return self._promises.list_open()

    def promises_for_task(self, task_id: str) -> list[Promise]:
        return self._promises.list_for_task(task_id)

    def promises_for_contact(self, contact_id: str) -> list[Promise]:
        """这个关系名下的承诺 —— 关系一等化的意义所在。"""
        return self._promises.list_for_contact(contact_id)

    def open_count(self) -> int:
        return self._promises.count_open()

    def resolve(
        self,
        promise_id: str,
        status: PromiseStatus,
        *,
        note: str | None = None,
    ) -> Promise:
        """把承诺落到终态。**只由用户显式动作调用**（11.19.5）。

        重复 resolve **拒绝**：一条承诺兑现两次说明状态搞乱了，
        静默改写会把用户的账目搞糊 —— 那比报错更糟。
        """
        current = self._promises.get(promise_id)
        if not current.is_open:
            raise ValidationError(
                f"这条承诺已经是「{current.status.value}」了，"
                "不能再改一次 —— 状态搞乱了会比你看到的更糟"
            )
        if status is PromiseStatus.OPEN:
            raise ValidationError("落终态要给一个具体的状态")
        return self._promises.resolve(promise_id, status, self._clock.now(), note=note)

    def keep(self, promise_id: str) -> Promise:
        """兑现了。"""
        return self.resolve(promise_id, PromiseStatus.KEPT)

    def break_(self, promise_id: str) -> Promise:
        """违约了。**只能由用户说**（系统不判违约，11.19.5）。"""
        return self.resolve(promise_id, PromiseStatus.BROKEN)

    def withdraw(self, promise_id: str) -> Promise:
        """收回了（对方取消 / 我改主意）。"""
        return self.resolve(promise_id, PromiseStatus.WITHDRAWN)

    def postpone(self, promise_id: str, promised_for: datetime | None) -> Promise:
        """延期。**延期不算违约** —— 那是用户的正当权利。"""
        current = self._promises.get(promise_id)
        if not current.is_open:
            raise ValidationError(
                f"这条承诺已经是「{current.status.value}」了，改不了日期"
            )
        return self._promises.set_promised_for(
            promise_id, promised_for, self._clock.now()
        )

    # -- 给排序与视图用的只读查询 -------------------------------------------- #

    def promise_of(self, task_id: str) -> Promise | None:
        """这条事务上的那条承诺（一条事务最多一条，见 11.19.2）。"""
        items = self._promises.list_for_task(task_id)
        return items[0] if items else None

    def is_overdue(self, promise: Promise, now: datetime) -> bool:
        """过了承诺日期吗？**只是事实判断，不是违约判定**。

        刻意分开两个问题：「过了日子」是算得出来的；「违约了」是用户的
        判断 —— 可能延期了、可能对方改主意了。系统只报前者
        （11.19.5）。
        """
        return promise.promised_for is not None and promise.promised_for < now
