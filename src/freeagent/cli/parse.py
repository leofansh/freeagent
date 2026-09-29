"""输入解析：把中文口语里的时间表达解析成日期/时刻。

只做**输入解析**，属于 CLI 层职责，不含任何业务判断。
刻意保持小而可预测：解析不出来就返回 ``None``，绝不猜。

三处易错点在此处理：
* ``大后天`` 必须先于 ``后天`` 匹配，否则会少一天；
* ``下周三`` 指「下一自然周的周三」，不是「三天后的周三」—— 以周一为周首；
* 中文数字时刻（「下午三点」「8点半」）与阿拉伯数字混用。
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

__all__ = ["parse_date_expr", "parse_time_expr"]

_WEEKDAY_MAP = {
    "一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6,
    "1": 0, "2": 1, "3": 2, "4": 3, "5": 4, "6": 5, "7": 6,
}

_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}

_ISO_DATE_RE = re.compile(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})")
_N_DAYS_RE = re.compile(r"(\d+)\s*天[后之]?后")
_NEXT_WEEKDAY_RE = re.compile(r"下+周\s*([一二三四五六日天1-7])")
_THIS_WEEKDAY_RE = re.compile(r"(?<!下)周\s*([一二三四五六日天1-7])")
_MERIDIEM_RE = re.compile(r"(上午|早上|早晨|中午|下午|傍晚|晚上|夜里)")

_NUM = r"[0-9〇零一二两三四五六七八九十]"
_CLOCK_RE = re.compile(
    rf"(?<![\d])({_NUM}{{1,3}})\s*[:：点時时]\s*({_NUM}{{1,3}}|半)?\s*分?"
)

# 长表达必须排在前面：「大后天」不能被「后天」抢先匹配。
_DATE_TOKENS: tuple[tuple[str, int], ...] = (
    ("大后天", 3),
    ("后天", 2),
    ("明天", 1),
    ("明日", 1),
    ("今天", 0),
    ("今日", 0),
    ("昨天", -1),
    ("昨日", -1),
)


def _cn_number(raw: str) -> int | None:
    """中文数字转 int。

    正确处理：十=10、十一=11、二十=20、二十三=23、三十五=35。
    注意「二十」是 20 而不是 30 —— 尾字为「十」时不做 +10。
    """
    if raw.isdigit():
        return int(raw)
    if raw in _CN_DIGITS:
        return _CN_DIGITS[raw]
    if len(raw) == 2:
        head, tail = raw[0], raw[1]
        if head == "十":  # 十一..十九
            d = _CN_DIGITS.get(tail)
            return 10 + d if d is not None else None
        if tail == "十":  # 二十..九十
            d = _CN_DIGITS.get(head)
            return d * 10 if d is not None else None
    if len(raw) == 3 and raw[1] == "十":  # 二十三..九十五
        a, b = _CN_DIGITS.get(raw[0]), _CN_DIGITS.get(raw[2])
        if a is not None and b is not None:
            return a * 10 + b
    return None


def _week_start(today: date) -> date:
    """本周一。"""
    return today - timedelta(days=today.weekday())


def parse_date_expr(text: str, today: date) -> date | None:
    """从文本里抽出一个日期。抽不出返回 ``None``。"""
    iso = _ISO_DATE_RE.search(text)
    if iso:
        try:
            return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        except ValueError:
            return None

    for token, offset in _DATE_TOKENS:
        if token in text:
            return today + timedelta(days=offset)

    n_days = _N_DAYS_RE.search(text)
    if n_days:
        return today + timedelta(days=int(n_days.group(1)))

    # 「下周X」= 下一个自然周里的第 X 天，不是「最近的 X」
    next_weekday = _NEXT_WEEKDAY_RE.search(text)
    if next_weekday:
        target = _WEEKDAY_MAP[next_weekday.group(1)]
        return _week_start(today) + timedelta(days=7 + target)

    # 裸「周X」：本周内就用本周，已过则顺延一周（今天不算「已过」）
    this_weekday = _THIS_WEEKDAY_RE.search(text)
    if this_weekday:
        target = _WEEKDAY_MAP[this_weekday.group(1)]
        candidate = _week_start(today) + timedelta(days=target)
        if candidate < today:
            candidate += timedelta(days=7)
        return candidate

    return None


def parse_time_expr(text: str, day: date) -> datetime | None:
    """从文本里抽出一个时刻（日期部分由 ``day`` 给出）。"""
    meridiem = _MERIDIEM_RE.search(text)
    clock = _CLOCK_RE.search(text)
    if clock is None:
        return None
    hour = _cn_number(clock.group(1))
    if hour is None:
        return None
    raw_minute = clock.group(2)
    if raw_minute is None:
        minute = 0
    elif raw_minute == "半":
        minute = 30
    else:
        parsed = _cn_number(raw_minute)
        if parsed is None:
            return None
        minute = parsed

    if meridiem is not None:
        marker = meridiem.group(1)
        if marker in ("下午", "傍晚", "晚上", "夜里") and hour < 12:
            hour += 12
        elif marker == "中午" and hour < 12:
            hour = 12
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return None
    return datetime.combine(day, time(hour=hour, minute=minute))
