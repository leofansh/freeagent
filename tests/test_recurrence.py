"""重复提醒规则：显式时区、墙钟时间、夏令时空洞与重叠。

这些断言的**时间基准全部是真实 tzdata**，不是构造出来的假偏移 ——
用真实规则才验得出「换表那天八点还是不是八点」。
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from freeagent.domain import TaskKind
from freeagent.services.recurrence import (
    RecurrenceError,
    RecurrenceKind,
    RecurrenceRule,
    parse_rule,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
NEW_YORK = ZoneInfo("America/New_York")


def daily(hhmm: str, tz: str) -> RecurrenceRule:
    return RecurrenceRule(kind=RecurrenceKind.DAILY, time=hhmm, timezone=tz)


def weekly(hhmm: str, tz: str, days: tuple[int, ...]) -> RecurrenceRule:
    return RecurrenceRule(
        kind=RecurrenceKind.WEEKLY, time=hhmm, timezone=tz, weekdays=days
    )


class TestParseDontValidate:
    """构造时就不该产出半合法的对象。"""

    @pytest.mark.parametrize(
        "kwargs, frag",
        [
            (dict(kind="hourly", time="08:00", timezone="Asia/Shanghai"), "只支持"),
            (dict(kind="daily", time="8:00", timezone="Asia/Shanghai"), "HH:MM"),
            (dict(kind="daily", time="25:00", timezone="Asia/Shanghai"), "HH:MM"),
            (dict(kind="daily", time="08:61", timezone="Asia/Shanghai"), "HH:MM"),
            (dict(kind="daily", time="08:00", timezone="Not/AZone"), "IANA"),
            (dict(kind="daily", time="08:00", timezone=""), "IANA"),
            (dict(kind="weekly", time="08:00", timezone="Asia/Shanghai"), "至少一个"),
            (
                dict(
                    kind="weekly",
                    time="08:00",
                    timezone="Asia/Shanghai",
                    weekdays=(0,),
                ),
                "1..7",
            ),
            (
                dict(
                    kind="weekly",
                    time="08:00",
                    timezone="Asia/Shanghai",
                    weekdays=(8,),
                ),
                "1..7",
            ),
            (
                dict(
                    kind="daily",
                    time="08:00",
                    timezone="Asia/Shanghai",
                    weekdays=(1,),
                ),
                "daily 不接受",
            ),
        ],
    )
    def test_rejects(self, kwargs, frag):
        with pytest.raises(RecurrenceError) as exc:
            RecurrenceRule(**kwargs)
        assert frag in str(exc.value)

    def test_time_is_canonicalised_to_hhmmss(self):
        """「八点」与「八点整」是同一件事，但下游按三段拆 —— 构造时就统一。"""
        assert daily("08:00", "Asia/Shanghai").time == "08:00:00"
        assert daily("08:00:00", "Asia/Shanghai").time == "08:00:00"
        assert weekly("09:30", "Asia/Shanghai", (1,)).time == "09:30:00"

    def test_single_digit_hour_is_rejected_not_padded(self):
        """``8:00`` 不补成 ``08:00`` —— 补了就得接受「用户少打一个 0」。

        那会让 ``8:00`` 和 ``08:00`` 混为一谈，而格式校验的意义就是
        「只有一种写法」。要宽严就在解析层决定，不在值对象里悄悄修。
        """
        with pytest.raises(RecurrenceError, match="HH:MM"):
            daily("8:00", "Asia/Shanghai")

    def test_weekdays_are_sorted_and_deduped(self):
        r = weekly("09:00", "Asia/Shanghai", (5, 1, 5, 3))
        assert r.weekdays == (1, 3, 5)


class TestNoDstZone:
    """Asia/Shanghai 没有夏令时 —— 用它隔离出「规则本身」的行为。"""

    def test_daily_advances_one_day(self):
        r = daily("08:00", "Asia/Shanghai")
        nxt = r.next_after(datetime(2026, 9, 30, 12, 0, tzinfo=SHANGHAI))
        assert nxt.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M") == "2026-10-01 08:00"

    def test_before_the_time_advances_to_today(self):
        """凌晨两点问「下一个八点」= **今天**的八点，不是明天的。"""
        r = daily("08:00", "Asia/Shanghai")
        nxt = r.next_after(datetime(2026, 9, 30, 2, 0, tzinfo=SHANGHAI))
        assert nxt.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M") == "2026-09-30 08:00"

    def test_exactly_at_the_time_advances_to_tomorrow(self):
        """「严格晚于」：正好八点时下一个是**明天**八点。"""
        r = daily("08:00", "Asia/Shanghai")
        nxt = r.next_after(datetime(2026, 9, 30, 8, 0, tzinfo=SHANGHAI))
        assert nxt.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M") == "2026-10-01 08:00"

    def test_local_calendar_not_utc_calendar(self):
        """东八区凌晨一点问「下一个八点」= 当天八点。

        踩过的坑：按 UTC 日期算会退到前一天 —— 东八区 00:30 时 UTC 还是前一天，
        于是「今天」被判成「明天」。这正是不能把 ``reminder_time`` 存成
        「本地墙钟 + 猜的时区」的原因。
        """
        r = daily("08:00", "Asia/Shanghai")
        nxt = r.next_after(datetime(2026, 9, 30, 0, 30, tzinfo=SHANGHAI))
        assert nxt.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M") == "2026-09-30 08:00"


class TestWeekly:
    # 刻意**不用** strftime 的 %a / %A —— 那是 locale 依赖的。
    # 踩过的坑：tests/test_feishu_log_encoding.py 会 setlocale(LC_ALL, ...)
    # 且不保证恢复，于是这两个断言只在完整跑时红、单跑时绿 ——
    # 那是最难查的一种失败。星期几用 isoweekday() 断言，locale 无关。

    def test_picks_the_next_matching_weekday(self):
        r = weekly("09:00", "Asia/Shanghai", (1, 3, 5))   # 一三五
        # 2026-09-30 是周三
        nxt = r.next_after(datetime(2026, 9, 30, 12, 0, tzinfo=SHANGHAI))
        local = nxt.astimezone(SHANGHAI)
        assert (local.year, local.month, local.day) == (2026, 10, 2)
        assert local.isoweekday() == 5, "应落在周五"
        assert (local.hour, local.minute) == (9, 0)

    def test_wraps_to_next_week(self):
        r = weekly("09:00", "Asia/Shanghai", (1,))
        nxt = r.next_after(datetime(2026, 9, 30, 12, 0, tzinfo=SHANGHAI))  # 周三
        local = nxt.astimezone(SHANGHAI)
        assert (local.month, local.day) == (10, 5)
        assert local.isoweekday() == 1, "应落在下周一"

    def test_every_day_is_equivalent_to_daily(self):
        daily_r = daily("09:00", "Asia/Shanghai")
        weekly_r = weekly("09:00", "Asia/Shanghai", (1, 2, 3, 4, 5, 6, 7))
        probe = datetime(2026, 9, 30, 12, 0, tzinfo=SHANGHAI)
        assert daily_r.next_after(probe) == weekly_r.next_after(probe)


class TestDaylightSavingGap:
    """春季跳表：那天某个墙钟时间**不存在**。语义 = 跳过那天。"""

    def test_nonexistent_local_time_is_skipped(self):
        # America/New_York 2026-03-08 是 DST 开始日，02:00→03:00，02:30 不存在
        r = daily("02:30", "America/New_York")
        nxt = r.next_after(datetime(2026, 3, 7, 12, 0, tzinfo=NEW_YORK))
        # 应跳过 03-08，落到 03-09
        assert nxt.astimezone(NEW_YORK).strftime("%Y-%m-%d %H:%M %Z") == (
            "2026-03-09 02:30 EDT"
        )

    def test_offset_actually_changed_across_the_boundary(self):
        """确认前面几条不是因为纽约「恰好没变」而通过的。

        2026-03-08 是换表日：03-07 的 08:00 是 EST(-5)，03-09 的 08:00 是 EDT(-4)。
        """
        before = daily("08:00", "America/New_York").next_after(
            datetime(2026, 3, 6, 12, 0, tzinfo=NEW_YORK)
        )   # -> 03-07 08:00
        after = daily("08:00", "America/New_York").next_after(
            datetime(2026, 3, 8, 12, 0, tzinfo=NEW_YORK)
        )   # -> 03-09 08:00
        assert before.utcoffset().total_seconds() == -5 * 3600, "03-07 应是 EST"
        assert after.utcoffset().total_seconds() == -4 * 3600, "03-09 应是 EDT"

    def test_wall_clock_is_preserved_across_the_change(self):
        """换表前后墙钟都必须是 08:00 —— 这正是不存时区会漂掉的东西。"""
        r = daily("08:00", "America/New_York")
        before = r.next_after(datetime(2026, 3, 6, 12, 0, tzinfo=NEW_YORK))   # 03-07
        after = r.next_after(datetime(2026, 3, 8, 12, 0, tzinfo=NEW_YORK))    # 03-09
        assert before.astimezone(NEW_YORK).strftime("%Y-%m-%d %H:%M") == "2026-03-07 08:00"
        assert after.astimezone(NEW_YORK).strftime("%Y-%m-%d %H:%M") == "2026-03-09 08:00"
        # 墙钟相同、UTC 偏移不同 —— 这就是「存时区」与「不存时区」的分水岭
        assert before.astimezone(NEW_YORK).strftime("%H:%M") == (
            after.astimezone(NEW_YORK).strftime("%H:%M")
        )
        assert before.utcoffset() != after.utcoffset()


class TestDaylightSavingOverlap:
    """秋季回拨：那天某个墙钟时间**出现两次**。语义 = 取更早。"""

    def test_takes_the_earlier_instant(self):
        # America/New_York 2026-11-01 是 DST 结束日，02:00→01:00，01:30 出现两次
        r = daily("01:30", "America/New_York")
        nxt = r.next_after(datetime(2026, 10, 31, 12, 0, tzinfo=NEW_YORK))
        assert nxt.astimezone(NEW_YORK).strftime("%Y-%m-%d %H:%M %Z") == (
            "2026-11-01 01:30 EDT"
        )

    def test_earlier_means_the_heavier_offset(self):
        """EDT(-4) 比 EST(-5) 更早 —— 夏令时那个是「前一次」。"""
        r = daily("01:30", "America/New_York")
        nxt = r.next_after(datetime(2026, 10, 31, 12, 0, tzinfo=NEW_YORK))
        assert nxt.utcoffset().total_seconds() == -4 * 3600
        assert nxt.fold == 0


class TestNoImplicitTimezone:
    """时区必填，不猜。"""

    def test_timezone_is_required_and_validated(self):
        with pytest.raises(RecurrenceError, match="IANA"):
            RecurrenceRule(kind="daily", time="08:00", timezone="")

    def test_a_bogus_zone_is_rejected_at_construction(self):
        with pytest.raises(RecurrenceError, match="IANA"):
            RecurrenceRule(kind="daily", time="08:00", timezone="Mars/Olympus")


class TestSerialisation:
    @pytest.mark.parametrize(
        "rule",
        [
            daily("08:00", "Asia/Shanghai"),
            weekly("09:30", "Europe/London", (1, 5)),
        ],
    )
    def test_round_trip(self, rule):
        assert parse_rule(rule.to_json()) == rule

    def test_no_rule_for_none_and_blank(self):
        assert parse_rule(None) is None
        assert parse_rule("") is None
        assert parse_rule("   ") is None

    @pytest.mark.parametrize(
        "bad",
        [
            "{not json",
            "[1,2]",
            '"a string"',
            '{"kind":"daily"}',                        # 缺字段
            '{"kind":"daily","time":"08:00"}',         # 缺时区
            '{"kind":"daily","time":"8:00","timezone":"UTC"}',      # 时间不合法
            '{"kind":"daily","time":"08:00","timezone":"Bad/Zone"}',  # 时区不合法
            '{"kind":"daily","time":"08:00","timezone":"UTC","weekdays":[1]}',  # daily 带 weekdays
        ],
    )
    def test_broken_rule_raises_instead_of_degrading(self, bad):
        """规则坏了要抛 —— 降级等于把「每天八点」悄悄变成一次性。"""
        with pytest.raises(RecurrenceError):
            parse_rule(bad)

    def test_describe_is_human_readable(self):
        assert "每天 08:00" in daily("08:00", "Asia/Shanghai").describe()
        w = weekly("09:30", "Asia/Shanghai", (1, 3, 5))
        d = w.describe()
        assert "周一" in d and "周三" in d and "周五" in d and "09:30" in d
