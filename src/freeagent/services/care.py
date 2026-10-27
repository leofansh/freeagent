"""人生角色关注面（设计文档 11.24 / V1.68）。

**它补的是产品一层缺口**：角色只当「工作脉络 + 事务挂靠」时，回答的是
「这件事属于哪一块」；但用户是多个**人生角色**的复合（爸爸 / 儿子 / 企业家
/ 高管…），每个角色有「我该周期性关心的东西」——「孩子的作业」「父母的身体」
「公司本月现金流」「那个项目的进展」。这些是**持续责任，不是待办**：
不因看一眼而结束，看完推到下个周期再来。

三条纪律（同 11.21 每日总览，刻意守住）：
* **只提醒自己、绝不代发**：到点把它并进每日总览，不做任何发送/代办。
* **关注面不是事务**：不会因做完了而消失，:meth:`done` 只是把 ``next_due``
  推到下个周期。
* **只摆事实**：cadence 用显式标记（``weekly:N`` / ``monthly:N``），到期只报
  「该看看 X」，不替用户判断要关心什么。

``cadence`` 由命令层把「每周五 / 每月 1 号」解析成规范串，本服务只做**格式
校验 + next_due 现算** —— 到期日不落库为「绝对时刻」，每次现算，避免时区 /
闰年手工维护。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from ..domain import CareArea, NotFoundError, ValidationError, new_id

_WEEKLY = "weekly"
_MONTHLY = "monthly"


@dataclass(frozen=True, slots=True)
class CareDue:
    """一条到期关注面 + 它所属角色的名字（给渲染层直接摆事实）。"""

    area: CareArea
    role_name: str


class CareService:
    """人生角色关注面：记 / 列 / 到期 / 看了推到下周期。

    ``cadence`` 形如 ``weekly:5``（每周五，ISO 1=周一 7=周日）或
    ``monthly:1``（每月 1 号）。**只此两种节奏** —— 覆盖「教育 / 健康 /
    财务 / 项目」这类周月关心的绝大多数，不把 cron 的复杂度引进来
    （那是制造选项，不是降低门槛）。
    """

    def __init__(self, care_repo, role_repo, clock) -> None:
        self._repo = care_repo
        self._roles = role_repo
        self._clock = clock

    def add(self, role_id: str, text: str, cadence: str) -> CareArea:
        """记一个关注面。``role_id`` 必须存在（角色是关注面的挂靠点）。"""
        self._validate_cadence(cadence)
        try:
            self._roles.get(role_id)
        except NotFoundError:
            raise NotFoundError("角色", role_id) from None
        if not text.strip():
            raise ValidationError("关注面不能是空的")
        next_due = self._next_due(cadence, self._today(), after=False)
        return self._repo.add(role_id, text.strip(), cadence, next_due, self._now())

    def list(self) -> list[CareArea]:
        """全部关注面，按创建序。"""
        return self._repo.list_all()

    def due(self, day: date) -> list[CareDue]:
        """``next_due`` 落在 ``day`` 的关注面（含角色名，给总览直接摆）。"""
        return [
            CareDue(area=a, role_name=self._role_name(a.role_id))
            for a in self._repo.due_on(day)
        ]

    def done(self, index: int) -> CareArea:
        """「看了」，把 ``next_due`` 推到下个周期。序号 1 起。

        从**当前的 next_due** 推下一个周期，而不是从今天 —— 若从今天算，
        刚记完（next_due 还在未来）就 done 时，会错误地推回到同一个未到期日。
        """
        areas = self.list()
        if index < 1 or index > len(areas):
            raise NotFoundError("关注面", index)
        area = areas[index - 1]
        return self._repo.roll_to_next(
            area.id, self._next_due(area.cadence, area.next_due, after=True), self._now()
        )

    def count(self) -> int:
        return len(self.list())

    # ------------------------------------------------------------------ 内部

    def _validate_cadence(self, cadence: str) -> None:
        kind, _, value = cadence.partition(":")
        if kind == _WEEKLY:
            try:
                n = int(value)
            except ValueError:
                n = 0
            if 1 <= n <= 7:
                return
        elif kind == _MONTHLY:
            try:
                n = int(value)
            except ValueError:
                n = 0
            if 1 <= n <= 31:
                return
        raise ValidationError(
            f"节奏只支持「每周1-7」或「每月1-31 号」这类（收到：{cadence}）"
        )

    def _next_due(self, cadence: str, today: date, *, after: bool) -> date:
        """算下次该看的日期。``after=False``（记时）允许今天就是该日；
        ``after=True``（``/care done`` 后）必须是**严格下一个**周期。
        """
        kind, _, value = cadence.partition(":")
        if kind == _WEEKLY:
            wd = int(value)
            # ``(wd - isoweekday) % 7``：下一个该日的偏移。``after=True`` 时
            # 若今天恰好就是该日（diff==0），必须推到**下个周期**（+7），
            # 否则「看了」之后同一天还会被当成到期。
            diff = (wd - today.isoweekday()) % 7
            if after and diff == 0:
                diff = 7
            return today + timedelta(days=diff)
        # monthly
        day = int(value)
        candidate = self._month_day_date(today.year, today.month, day)
        if candidate < today or (after and candidate == today):
            year, month = self._next_month(today.year, today.month)
            candidate = self._month_day_date(year, month, day)
        return candidate

    @staticmethod
    def _month_day_date(year: int, month: int, day: int) -> date:
        """该月第 ``day`` 天；该月没有这一天（如 2 月 30）就取该月最后一天。
        刻意不报错 —— 「每月 30 号」在二月就顺到月底，比让用户纠结更贴
        真实关心（同 11.21 的「宁可漏不可擅动」，这里宁可顺延不硬报错）。
        """
        while day > 28:
            try:
                return date(year, month, day)
            except ValueError:
                day -= 1
        return date(year, month, day)

    @staticmethod
    def _next_month(year: int, month: int) -> tuple[int, int]:
        return (year, month + 1) if month < 12 else (year + 1, 1)

    def _role_name(self, role_id: str) -> str:
        try:
            return self._roles.get(role_id).name
        except NotFoundError:
            # 角色可能已被删；关注面不该跟着一起报错 —— 摆一个占位，不崩溃。
            return "（角色已删）"

    def _today(self) -> date:
        return self._clock.now().date()

    def _now(self) -> datetime:
        return self._clock.now()
