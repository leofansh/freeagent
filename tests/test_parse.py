"""中文时间表达解析测试。"""

from __future__ import annotations

from datetime import date, datetime, time

import pytest

from freeagent.cli.parse import parse_date_expr, parse_time_expr

TODAY = date(2026, 9, 26)  # 周六


@pytest.mark.parametrize(
    "text,expected",
    [
        ("今天要交", date(2026, 9, 26)),
        ("今日要交", date(2026, 9, 26)),
        ("昨天催过了", date(2026, 9, 25)),
        ("昨日催过了", date(2026, 9, 25)),
        ("明天要交", date(2026, 9, 27)),
        ("后天要交", date(2026, 9, 28)),
        ("大后天要交", date(2026, 9, 29)),
        ("3天后交", date(2026, 9, 29)),
        ("2026-10-05 交", date(2026, 10, 5)),
        ("2026/10/05 交", date(2026, 10, 5)),
        # 2026-09-26 是周六，本周 = 09-21..09-27，下周 = 09-28..10-04
        ("下周一交", date(2026, 9, 28)),
        ("下周二交", date(2026, 9, 29)),
        ("下周日交", date(2026, 10, 4)),
        # 裸「周X」：本周内优先；「周六」就是今天；「周日」是明天（仍属本周）
        ("周六交", date(2026, 9, 26)),
        ("周日交", date(2026, 9, 27)),
        # 裸「周五」已过（昨天），顺延一周
        ("周五交", date(2026, 10, 2)),
    ],
)
def test_parse_date(text, expected):
    assert parse_date_expr(text, TODAY) == expected


def test_no_date_returns_none():
    assert parse_date_expr("写个周报", TODAY) is None
    assert parse_date_expr("", TODAY) is None


def test_invalid_iso_date_returns_none():
    assert parse_date_expr("2026-13-45 交", TODAY) is None


@pytest.mark.parametrize(
    "text,expected",
    [
        ("下午三点提醒", time(15, 0)),
        ("上午9点提醒", time(9, 0)),
        ("晚上8点半", time(20, 30)),
        ("15:00 提醒", time(15, 0)),
        ("15：30 提醒", time(15, 30)),
        ("中午12点", time(12, 0)),
        ("夜里11点", time(23, 0)),
    ],
)
def test_parse_time(text, expected):
    got = parse_time_expr(text, TODAY)
    assert got is not None
    assert got.time() == expected
    assert got.date() == TODAY


def test_no_time_returns_none():
    assert parse_time_expr("下周再说", TODAY) is None


def test_out_of_range_hour_returns_none():
    assert parse_time_expr("99:00", TODAY) is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("十", 10), ("一", 1), ("九", 9),
        ("十一", 11), ("十九", 19),
        ("二十", 20), ("三十", 30), ("九十", 90),
        ("二十三", 23), ("三十五", 35),
        ("9", 9), ("15", 15),
    ],
)
def test_chinese_numerals(raw, expected):
    from freeagent.cli.parse import _cn_number

    assert _cn_number(raw) == expected


def test_cn_number_rejects_nonsense():
    from freeagent.cli.parse import _cn_number

    assert _cn_number("零一") is None
    assert _cn_number("猫") is None
