"""重复提醒规则：显式时区 + 墙钟时间。

## 为什么不直接存「下一次」的瞬时

一次性的提醒存一个瞬时就够（``tasks.reminder_time``）。但「每天早上八点」
**不是**一个瞬时 —— 它是一条规则，自动产出的瞬时会随夏令时漂移：
不存时区的话，用户本地时间从 UTC+8 换到 UTC+9，「八点」会跟着漂一小时。

所以这里的分工是：

- ``tasks.reminder_time`` 始终是**下一次触发的瞬时**（指向）
- ``tasks.reminder_rule`` 是**生成器**（规则），可空

确认送达后，引擎问规则「下一个在哪」，把结果写回 ``reminder_time``。
于是 :mod:`freeagent.services.reminders` 的到期查询、合并摘要、错过窗口
**全部不用改** —— 它们只认「下一次」这个指针。

## 夏令时：两种非法与一种歧义

规则存的是**墙钟时间**（``08:00``）+ **显式 IANA 时区**。换算成瞬时
会遇到三种情况：

1. **正常** —— 唯一瞬时。
2. **空洞**（春季跳表，``02:30`` 那天不存在）—— **跳过那天**。
   语义来自 dsh 的 ``schedule``：*"A nonexistent local time or entire date
   is skipped"*。另一种选择是顺延到 03:00，但那会让「八点」在某天变成九点，
   反而更不像用户要的。
3. **重叠**（秋季回拨，``02:30`` 那天出现两次）—— **取更早的那个**。
   同样对齐 dsh：*"A daylight-saving overlap chooses its first, earlier
   instant"*。取更早是因为「到点了」应该尽早发生；取更晚会让提醒晚一次。

判定技巧：把 ``fold=0`` 与 ``fold=1`` 各造一次，换算到 UTC 再换回来 ——
**回不去**就是空洞，**回得去但两个 UTC 偏移不同**就是重叠。

## 只支持 daily / weekly

``cron`` 与 ``every_N_seconds`` 刻意不做：

- ``every_seconds`` 量的是**流逝时间**，不是墙钟。跨夏令时它不会跟着走 ——
  「每 86400 秒」在换表那天不等价于「每天」。这是两种不同的语义，不能混。
- ``cron`` 的五字段表达式要处理步长、范围、``*`` 的星号语义，
  是另一个规模的实现；V1 用不上。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = [
    "RecurrenceKind",
    "RecurrenceRule",
    "RecurrenceError",
    "parse_rule",
]

#: ``HH:MM`` 或 ``HH:MM:SS``。秒可选是因为「八点」和「八点整」是同一件事。
_TIME_RE = re.compile(r"^(?P<h>[01]\d|2[0-3]):(?P<m>[0-5]\d)(?::(?P<s>[0-5]\d))?$")

#: 向前找多少天。daily 最多看 2 天，weekly 最多看 8 天（覆盖满一周 + 缓冲）。
#: 上界存在是因为 DST 空洞可能连续两天不可达（罕见但可能），需要终止条件。
_MAX_SCAN_DAYS = 8

_WEEKDAY_CN = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "日"}


class RecurrenceError(ValueError):
    """规则不合法。构造期就抛，**不**返回半合法的对象。"""


class RecurrenceKind:
    DAILY = "daily"
    WEEKLY = "weekly"

    ALL = (DAILY, WEEKLY)


@dataclass(frozen=True, slots=True)
class RecurrenceRule:
    """一条重复提醒规则。**解析即校验**（parse-don't-validate）。"""

    kind: str
    #: 本地墙钟时间，``HH:MM:SS``。**不带时区** —— 时区在 ``timezone``。
    time: str
    #: 显式 IANA 时区名，如 ``Asia/Shanghai``。**必填**：不猜。
    timezone: str
    #: ISO 星期几（1=周一 … 7=周日），只对 ``weekly`` 有意义。
    weekdays: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in RecurrenceKind.ALL:
            raise RecurrenceError(
                f"重复规则只支持 {RecurrenceKind.ALL}，收到 {self.kind!r}"
            )
        matched = _TIME_RE.match(self.time)
        if not matched:
            raise RecurrenceError(f"时间要 HH:MM 或 HH:MM:SS，收到 {self.time!r}")
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise RecurrenceError(f"时区必须是有效的 IANA 名，收到 {self.timezone!r}") from exc
        if self.kind == RecurrenceKind.WEEKLY:
            if not self.weekdays:
                raise RecurrenceError("weekly 必须指定至少一个星期几")
            bad = [d for d in self.weekdays if d not in _WEEKDAY_CN]
            if bad:
                raise RecurrenceError(
                    f"星期几必须是 1..7（1=周一），收到 {bad}"
                )
        elif self.weekdays:
            raise RecurrenceError("daily 不接受 weekdays")
        # 归一化成 HH:MM:SS。「八点」与「八点整」是同一件事，但下游拆分
        # 按三段走，所以**在这里**统一，别留到用的时候才炸 ——
        # 那是把校验和解析混在一起，parse-don't-validate 要求的正是这一下。
        canonical = f"{matched['h']}:{matched['m']}:{matched['s'] or '00'}"
        if canonical != self.time:
            object.__setattr__(self, "time", canonical)
        if self.weekdays and tuple(sorted(set(self.weekdays))) != self.weekdays:
            object.__setattr__(self, "weekdays", tuple(sorted(set(self.weekdays))))

    # -- 序列化 ------------------------------------------------------------- #
    def to_json(self) -> str:
        payload: dict[str, object] = {
            "kind": self.kind,
            "time": self.time,
            "timezone": self.timezone,
        }
        if self.kind == RecurrenceKind.WEEKLY:
            payload["weekdays"] = sorted(self.weekdays)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    # -- 推导 ---------------------------------------------------------------- #
    def next_after(self, moment: datetime) -> datetime:
        """严格晚于 ``moment`` 的下一个瞬时（带时区）。

        找不到就抛 :class:`RecurrenceError` 而不是返回 ``None`` ——
        「配了一个永远不触发的提醒」是**配置错误**，该在设置时就说出来，
        不该等到提醒发不出来才表现得像没触发。
        """
        tz = ZoneInfo(self.timezone)
        hour, minute, second = (int(p) for p in self.time.split(":"))
        # 先按当前时区算出「今天这张日历上的日期」。
        # 用 moment 自己的日期，而不是 UTC 日期 —— 否则东八区凌晨一点会算成前一天。
        local_day = moment.astimezone(tz).date()

        # 从**今天**开始扫，不是明天。凌晨两点问「下一个八点」得到的是今天的
        # 八点 —— 跳过今天会让「每天八点」在凌晨设规则时平白晚一天。
        # 严格晚于由下面那行 `hit <= moment` 保证：正好八点时今天会被跳过，
        # 于是「确认送达后推进」拿到的是明天，不会原地踏步。
        for offset in range(0, _MAX_SCAN_DAYS + 1):
            day = local_day + timedelta(days=offset)
            if self.kind == RecurrenceKind.WEEKLY and day.isoweekday() not in self.weekdays:
                continue
            naive = datetime(day.year, day.month, day.day, hour, minute, second)
            hit = _localize(naive, tz)
            if hit is not None and hit > moment:
                return hit
        raise RecurrenceError(
            f"{_describe(self)} 在 {local_day} 之后 {_MAX_SCAN_DAYS} 天内没有可触发时刻"
        )

    def describe(self) -> str:
        return _describe(self)


def _describe(rule: RecurrenceRule) -> str:
    """一句给用户看的话。不含 tz 数据库细节。"""
    hhmm = rule.time[:5]
    zone = rule.timezone.split("/")[-1].replace("_", " ")
    if rule.kind == RecurrenceKind.DAILY:
        return f"每天 {hhmm}（{zone}）"
    days = "、".join(f"周{_WEEKDAY_CN[d]}" for d in sorted(rule.weekdays))
    return f"{days} {hhmm}（{zone}）"


def _localize(naive: datetime, tz: ZoneInfo) -> datetime | None:
    """把墙钟时间落成带时区的瞬时。**空洞返回 ``None``。**

    做法：``fold=0`` / ``fold=1`` 各造一个，换算到 UTC 再换回本地 ——
    回不去就是空洞（那次墙上根本没这个时刻）；回得去但两个 UTC 偏移不同
    就是重叠（那天出现两次）。
    """
    early = naive.replace(tzinfo=tz, fold=0)
    late = naive.replace(tzinfo=tz, fold=1)

    # 空洞判定：任一 fold 都回不到原本的墙钟时间。
    for cand in (early, late):
        if cand.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None) != naive:
            return None

    # 重叠判定：两个 fold 的 UTC 偏移不同 = 那天这个时刻出现两次。
    if early.utcoffset() != late.utcoffset():
        return min(early, late)
    return early


def parse_rule(text: str | None) -> RecurrenceRule | None:
    """从 ``tasks.reminder_rule`` 的原始文本解出规则。

    ``None`` / 空串 = 无重复规则（一次性提醒）。其余情况**抛**而不是
    降级：规则坏了就当它不存在，等于把「每天八点」悄悄变成一次性，
    而用户不会知道。
    """
    if text is None or not text.strip():
        return None
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RecurrenceError(f"重复规则不是合法 JSON：{exc}") from exc
    if not isinstance(raw, dict):
        raise RecurrenceError("重复规则必须是 JSON 对象")
    missing = {"kind", "time", "timezone"} - set(raw)
    if missing:
        raise RecurrenceError(f"重复规则缺字段：{sorted(missing)}")
    days = raw.get("weekdays") or ()
    if not isinstance(days, (list, tuple)):
        raise RecurrenceError("weekdays 必须是数组")
    return RecurrenceRule(
        kind=str(raw["kind"]),
        time=str(raw["time"]),
        timezone=str(raw["timezone"]),
        weekdays=tuple(sorted({int(d) for d in days})),
    )
