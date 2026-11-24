"""中国节日日历与公历↔农历换算（节日感知的确定性数据层）。

为什么要有这个模块
==================
2026-09-25 是中秋节，但鲸鲸回了「不是什么大节，就普通一天」：``[时间感知]``
块只给了公历日期，节日只能靠模型自己"记"——而模型的公历↔农历换算不可靠。
本模块把节日变成**本地、确定性**的事实：拿到一个日期就能算出今天是什么节、
接下来有什么节，不依赖网络、不依赖模型训练数据。

数据来源与范围
==============
``_LUNAR_INFO``（1901..2100 共 200 项）由 .NET
``System.Globalization.ChineseLunisolarCalendar`` 生成（该日历支持
1901-02-19 ~ 2101-01-28）。每项编码一个农历年：

* 低 13 位：第 1..13 个月槽的月长，1=30 天、0=29 天（月槽=微软历口径，
  闰月占一个槽位，其后的月槽整体后移）；
* 第 13..16 位：闰月的月槽号（0=无闰月；如 2023 年闰二月=3）。

换算结果已与本机 .NET 日历的 4946 个采样点（每个农历月的初一与十五）
逐一交叉验证；测试见 ``tests/test_festivals.py``。

约定
====
* 节日按「自然月」定义（正月=1、二月=2……腊月=12），闰月里的同名日子不算节；
* 只做日期级判断，不区分节日当天几点；
* 超出 1901-02-19 ~ 2101-01-28 一律返回 None / 不匹配，**绝不猜**；
* 清明用寿星公式计算（1901-2100），已用 2000-2026 的官方日期钉住；
  个别偏远年份可能差 1 天，这是已知边界。

节日的展示分三档（tier），决定「临近」提示的窗口：
A=长假/大节（提前 15 天）、B=民间/社交节日（提前 7 天）、C=纪念日（只在当天）。
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from datetime import date, timedelta
from safelog import warn

__all__ = [
    "LunarDate",
    "FestivalOccurrence",
    "solar_to_lunar",
    "lunar_to_solar",
    "chinese_new_year",
    "festivals_on",
    "today_suffix",
    "upcoming",
    "upcoming_line",
]

# ---------------------------------------------------------------------------
# 农历数据（生成物，勿手改；改数据必须重新跑生成器并复验采样点）
# ---------------------------------------------------------------------------

# 1901..2100，每年一项，编码见模块 docstring。
_LUNAR_INFO: tuple[int, ...] = (
    0x00752, 0x00ea5, 0x0d64a, 0x0064b, 0x00a9b, 0x0b556, 0x0056a, 0x00b59, 0x07752, 0x00752,
    0x0fb25, 0x00b25, 0x00a4b, 0x0d4ab, 0x002ad, 0x0056b, 0x06b69, 0x00da9, 0x11d92, 0x00e92,
    0x00d25, 0x0da4d, 0x00a56, 0x002b6, 0x0b5b5, 0x006d4, 0x00ea9, 0x07e92, 0x00e92, 0x0ed26,
    0x0052b, 0x00a57, 0x0d2b6, 0x00b5a, 0x006d4, 0x08ec9, 0x00749, 0x11693, 0x00a93, 0x0052b,
    0x0ea5b, 0x00aad, 0x0056a, 0x0bb55, 0x00ba4, 0x00b49, 0x07a93, 0x00a95, 0x1152d, 0x00536,
    0x00aad, 0x0d5aa, 0x005b2, 0x00da5, 0x09d4a, 0x00d4a, 0x12a95, 0x00a97, 0x00556, 0x0eab5,
    0x00ad5, 0x006d2, 0x0aea5, 0x00ea5, 0x0064a, 0x08c97, 0x00a9b, 0x1155a, 0x0056a, 0x00b69,
    0x0d752, 0x00b52, 0x00b25, 0x0b64b, 0x00a4b, 0x134ab, 0x002ad, 0x0056d, 0x0eb69, 0x00da9,
    0x00d92, 0x0bd25, 0x00d25, 0x17a4d, 0x00a56, 0x002b6, 0x0e5b5, 0x006d5, 0x00ea9, 0x0de92,
    0x00e92, 0x00d26, 0x08a56, 0x00a57, 0x134d6, 0x0035a, 0x006d5, 0x0d6c9, 0x00749, 0x00693,
    0x0b52b, 0x0052b, 0x00a5b, 0x0755a, 0x0056a, 0x11b55, 0x00ba4, 0x00b49, 0x0da93, 0x00a95,
    0x0052d, 0x0aaad, 0x00ab5, 0x155aa, 0x005d2, 0x00da5, 0x0fd4a, 0x00d4a, 0x00c95, 0x0b52e,
    0x00556, 0x00ab5, 0x075b2, 0x006d2, 0x0eea5, 0x00725, 0x0064b, 0x0cc97, 0x00cab, 0x0055a,
    0x08ad6, 0x00b69, 0x19752, 0x00b52, 0x00b25, 0x0fa4b, 0x00a4b, 0x004ab, 0x0c55b, 0x005ad,
    0x00b6a, 0x07b52, 0x00d92, 0x11d25, 0x00d25, 0x00a55, 0x0d4ad, 0x004b6, 0x005b5, 0x08daa,
    0x00ec9, 0x13e92, 0x00e92, 0x00d26, 0x0ea56, 0x00a57, 0x004d6, 0x0a6d5, 0x00755, 0x00749,
    0x08e93, 0x00693, 0x1152b, 0x0052b, 0x00a5b, 0x0d55a, 0x0056a, 0x00b65, 0x0b74a, 0x00b4a,
    0x13a95, 0x00a95, 0x0052d, 0x0eaad, 0x00ab5, 0x005aa, 0x0aba5, 0x00da5, 0x00d4a, 0x09c95,
    0x00c96, 0x1194e, 0x00556, 0x00ab5, 0x0d5b2, 0x006d2, 0x00ea5, 0x0ae4a, 0x0064b, 0x12c97,
    0x004ab, 0x0055b, 0x0ead6, 0x00b6a, 0x00752, 0x0b725, 0x00b25, 0x00a8b, 0x0749b, 0x004ab,
)

_FIRST_LUNAR_YEAR = 1901
_LAST_LUNAR_YEAR = 2100
_LUNAR_BASE = date(1901, 2, 19)  # 1901 年正月初一
_LUNAR_END = date(2101, 1, 29)  # 2101 年正月初一（= 2100 年最后一天 + 1，表外终点）

_LUNAR_MONTH_NAMES = ("", "正", "二", "三", "四", "五", "六", "七", "八", "九", "十", "冬", "腊")
_CN_DIGITS = ("", "一", "二", "三", "四", "五", "六", "七", "八", "九")


# ---------------------------------------------------------------------------
# 农历换算
# ---------------------------------------------------------------------------


def _lunar_info(year: int) -> int:
    return _LUNAR_INFO[year - _FIRST_LUNAR_YEAR]


def _leap_slot(year: int) -> int:
    """闰月所在月槽（0=无闰月）。"""
    return (_lunar_info(year) >> 13) & 0xF


def _slot_month_len(year: int, slot: int) -> int:
    """第 slot 个月槽的天数（29/30）。"""
    return 30 if (_lunar_info(year) >> (slot - 1)) & 1 else 29


def _year_days(year: int) -> int:
    leap = _leap_slot(year)
    slots = 13 if leap else 12
    return sum(_slot_month_len(year, slot) for slot in range(1, slots + 1))


@functools.lru_cache(maxsize=8)
def _new_year_dates() -> dict[int, date]:
    """农历年 -> 该年正月初一的公历日期（1901..2101，2101 为表外终点）。"""
    out = {_FIRST_LUNAR_YEAR: _LUNAR_BASE}
    cursor = _LUNAR_BASE
    for year in range(_FIRST_LUNAR_YEAR, _LAST_LUNAR_YEAR + 1):
        cursor = cursor + timedelta(days=_year_days(year))
        out[year + 1] = cursor
    return out


def chinese_new_year(year: int) -> date | None:
    """该农历年正月初一对应的公历日期；范围外返回 None。"""
    if not (_FIRST_LUNAR_YEAR <= year <= _LAST_LUNAR_YEAR + 1):
        return None
    return _new_year_dates()[year]


@dataclass(frozen=True)
class LunarDate:
    """一个农历日期；``month`` 是自然月（1..12），``is_leap`` 标记闰月。"""

    year: int
    month: int
    day: int
    is_leap: bool = False

    @property
    def text(self) -> str:
        """人类可读写法，如「腊月廿九」「闰二月初一」。"""
        month = _LUNAR_MONTH_NAMES[self.month] if 1 <= self.month <= 12 else str(self.month)
        prefix = "闰" if self.is_leap else ""
        return f"{prefix}{month}月{_day_text(self.day)}"


def _day_text(day: int) -> str:
    if day <= 0:
        return str(day)
    if day <= 10:
        return "初" + _CN_DIGITS[day]
    if day < 20:
        return "十" + (_CN_DIGITS[day - 10] if day > 10 else "")
    if day == 20:
        return "二十"
    if day < 30:
        return "廿" + _CN_DIGITS[day - 20]
    return "三十"


def solar_to_lunar(value: date) -> LunarDate | None:
    """公历 -> 农历；超出数据范围返回 None。"""
    if value < _LUNAR_BASE or value >= _LUNAR_END:
        return None
    years = _new_year_dates()
    low, high = _FIRST_LUNAR_YEAR, _LAST_LUNAR_YEAR
    while low < high:  # 找最后一个正月初一 <= value 的农历年
        mid = (low + high + 1) // 2
        if years[mid] <= value:
            low = mid
        else:
            high = mid - 1
    year = low
    offset = (value - years[year]).days
    slot = 1
    while slot <= 13 and offset >= _slot_month_len(year, slot):
        offset -= _slot_month_len(year, slot)
        slot += 1
    if slot > 13:  # 理论不可达；数据损坏时宁可返回 None 也不猜
        return None
    leap = _leap_slot(year)
    is_leap = bool(leap) and slot == leap
    # 自然月：闰月（slot == leap）记为其所闰的自然月；其后的月槽整体 -1。
    month = slot - 1 if (leap and slot >= leap) else slot
    return LunarDate(year=year, month=month, day=offset + 1, is_leap=is_leap)


def lunar_to_solar(year: int, month: int, day: int, *, leap: bool = False) -> date | None:
    """农历（自然月口径）-> 公历；非法日期/范围外返回 None。"""
    if not (_FIRST_LUNAR_YEAR <= year <= _LAST_LUNAR_YEAR):
        return None
    leap_slot = _leap_slot(year)
    if leap:
        if not leap_slot or month != leap_slot - 1:
            return None
        slot = leap_slot
    else:
        if not 1 <= month <= 12:
            return None
        slot = month if (not leap_slot or month < leap_slot) else month + 1
    if not 1 <= day <= _slot_month_len(year, slot):
        return None
    offset = sum(_slot_month_len(year, s) for s in range(1, slot)) + day - 1
    return chinese_new_year(year) + timedelta(days=offset)


# ---------------------------------------------------------------------------
# 节日表
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Festival:
    name: str
    kind: str  # solar | lunar | nth_weekday | qingming | lunar_eve
    month: int = 0
    day: int = 0
    weekday: int = 6  # 0=周一 ... 6=周日
    nth: int = 1
    tier: str = "B"  # A=长假大节(提前15天) B=民间节日(提前7天) C=只在当天


_FESTIVALS: tuple[_Festival, ...] = (
    _Festival("元旦", "solar", 1, 1, tier="A"),
    _Festival("春节", "lunar", 1, 1, tier="A"),
    _Festival("除夕", "lunar_eve", tier="A"),
    _Festival("清明节", "qingming", tier="A"),
    _Festival("劳动节", "solar", 5, 1, tier="A"),
    _Festival("端午节", "lunar", 5, 5, tier="A"),
    _Festival("中秋节", "lunar", 8, 15, tier="A"),
    _Festival("国庆节", "solar", 10, 1, tier="A"),
    _Festival("元宵节", "lunar", 1, 15, tier="B"),
    _Festival("情人节", "solar", 2, 14, tier="B"),
    _Festival("妇女节", "solar", 3, 8, tier="B"),
    _Festival("愚人节", "solar", 4, 1, tier="B"),
    _Festival("母亲节", "nth_weekday", 5, weekday=6, nth=2, tier="B"),
    _Festival("儿童节", "solar", 6, 1, tier="B"),
    _Festival("父亲节", "nth_weekday", 6, weekday=6, nth=3, tier="B"),
    _Festival("七夕节", "lunar", 7, 7, tier="B"),
    _Festival("教师节", "solar", 9, 10, tier="B"),
    _Festival("重阳节", "lunar", 9, 9, tier="B"),
    _Festival("平安夜", "solar", 12, 24, tier="B"),
    _Festival("圣诞节", "solar", 12, 25, tier="B"),
    _Festival("腊八节", "lunar", 12, 8, tier="B"),
    _Festival("植树节", "solar", 3, 12, tier="C"),
    _Festival("青年节", "solar", 5, 4, tier="C"),
    _Festival("龙抬头", "lunar", 2, 2, tier="C"),
    _Festival("建党节", "solar", 7, 1, tier="C"),
    _Festival("建军节", "solar", 8, 1, tier="C"),
    _Festival("网络情人节", "solar", 5, 20, tier="C"),
)
_FESTIVAL_ORDER = {f.name: index for index, f in enumerate(_FESTIVALS)}


def _nth_weekday(year: int, month: int, weekday: int, nth: int) -> date:
    first = date(year, month, 1)
    shift = (weekday - first.weekday()) % 7
    return first + timedelta(days=shift + (nth - 1) * 7)


def _qingming(year: int) -> date | None:
    """清明（寿星公式；1901-2099 使用对应世纪常数，2100 用 22 世纪常数）。"""
    if not 1901 <= year <= 2100:
        return None
    if year >= 2100:
        y, c = year - 2000, 5.15
    elif year >= 2000:
        y, c = year - 2000, 4.81
    else:
        y, c = year - 1900, 5.59
    return date(year, 4, int(y * 0.2422 + c) - y // 4)


def _resolve(festival: _Festival, year: int) -> date | None:
    """第 year 年（公历年；农历节日按农历年）的节日日期。"""
    if festival.kind == "solar":
        try:
            return date(year, festival.month, festival.day)
        except ValueError:
            warn("core/festivals.py:282 ValueError", err=repr(__import__("sys").exc_info()[1]))
            return None
    if festival.kind == "lunar":
        return lunar_to_solar(year, festival.month, festival.day)
    if festival.kind == "nth_weekday":
        return _nth_weekday(year, festival.month, festival.weekday, festival.nth)
    if festival.kind == "qingming":
        return _qingming(year)
    if festival.kind == "lunar_eve":
        new_year = chinese_new_year(year)
        return new_year - timedelta(days=1) if new_year else None
    return None


@dataclass(frozen=True)
class FestivalOccurrence:
    name: str
    date: date
    days_until: int
    note: str = ""  # 农历写法，如「农历八月十五」
    tier: str = "B"


def _occurrence(festival: _Festival, day: date, reference: date) -> FestivalOccurrence:
    note = ""
    if festival.kind in ("lunar", "lunar_eve"):
        lunar = solar_to_lunar(day)
        if lunar is not None:
            note = f"农历{lunar.text}"
    return FestivalOccurrence(
        name=festival.name,
        date=day,
        days_until=(day - reference).days,
        note=note,
        tier=festival.tier,
    )


def occurrences(start: date, end: date) -> list[FestivalOccurrence]:
    """[start, end] 区间内（含端点）的全部节日，按日期与登记顺序排序。"""
    found: dict[tuple[date, str], FestivalOccurrence] = {}
    for festival in _FESTIVALS:
        for year in range(start.year - 1, end.year + 2):
            day = _resolve(festival, year)
            if day is None or not start <= day <= end:
                continue
            key = (day, festival.name)
            if key not in found:
                found[key] = _occurrence(festival, day, start)
    return sorted(found.values(), key=lambda o: (o.date, _FESTIVAL_ORDER[o.name]))


def festivals_on(day: date) -> list[str]:
    """当天节日名（可为空列表；同一天多节按登记顺序）。"""
    return [o.name for o in occurrences(day, day)]


def today_suffix(day: date) -> str:
    """挂在「今天: ...」行尾的节日后缀：'' 或 '（中秋节，农历八月十五）'。"""
    items = occurrences(day, day)
    if not items:
        return ""
    names = "、".join(o.name for o in items)
    note = next((o.note for o in items if o.note), "")
    return f"（{names}，{note}）" if note else f"（{names}）"


def upcoming(day: date, *, limit: int = 2) -> list[FestivalOccurrence]:
    """「临近节日」：按档位窗口（A=15 天 / B=7 天）取最近的节日，最多 limit 个。"""
    best: list[FestivalOccurrence] = []
    for festival in _FESTIVALS:
        if festival.tier == "C":
            continue
        window = 15 if festival.tier == "A" else 7
        for year in (day.year - 1, day.year, day.year + 1):
            target = _resolve(festival, year)
            if target is None or target <= day:  # 当天走「今天」后缀，不进这里
                continue
            if (target - day).days <= window:
                best.append(_occurrence(festival, target, day))
                break
    best.sort(key=lambda o: (o.date, _FESTIVAL_ORDER[o.name]))
    return best[:limit]


def upcoming_line(day: date, *, limit: int = 2) -> str:
    """「临近节日」提示行：'' 或 '临近节日: 国庆节（10月01日，6天后）'。"""
    items = upcoming(day, limit=limit)
    if not items:
        return ""
    parts = [
        f"{o.name}（{o.date.month}月{o.date.day:02d}日，{o.days_until}天后）"
        for o in items
    ]
    return "临近节日: " + "；".join(parts)
