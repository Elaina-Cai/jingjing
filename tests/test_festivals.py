"""节日感知（festivals）回归测试。

覆盖：
- 公历↔农历换算（对 .NET 中国农历日历生成的已知点 + 全量回环）
- 节日判定（今天/临近）、闰月不算节、tier 窗口
- [时间感知] 块 / now_line / get_current_time 工具
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import festivals, temporal  # noqa: E402


def test_lunar_conversion_known_dates() -> None:
    # 期望值来自本机 .NET ChineseLunisolarCalendar（生成数据时逐点对照）
    cases = [
        (date(1901, 2, 19), (1901, 1, 1, False)),
        (date(2000, 2, 5), (2000, 1, 1, False)),
        (date(2023, 1, 22), (2023, 1, 1, False)),
        (date(2023, 3, 22), (2023, 2, 1, True)),  # 2023 闰二月初一
        (date(2023, 4, 20), (2023, 3, 1, False)),
        (date(2024, 2, 10), (2024, 1, 1, False)),
        (date(2024, 9, 17), (2024, 8, 15, False)),
        (date(2025, 1, 29), (2025, 1, 1, False)),
        (date(2025, 7, 25), (2025, 6, 1, True)),  # 2025 闰六月初一
        (date(2026, 2, 17), (2026, 1, 1, False)),
        (date(2026, 9, 25), (2026, 8, 15, False)),
        (date(2033, 12, 22), (2033, 11, 1, True)),  # 2033 闰十一月初一
        (date(2034, 1, 20), (2033, 12, 1, False)),
        (date(2101, 1, 28), (2100, 12, 29, False)),  # 表内最后一天
    ]
    for solar, expected in cases:
        got = festivals.solar_to_lunar(solar)
        assert got is not None, solar
        actual = (got.year, got.month, got.day, got.is_leap)
        assert actual == expected, (solar, actual, expected)

    assert festivals.lunar_to_solar(2026, 8, 15) == date(2026, 9, 25)
    assert festivals.lunar_to_solar(2023, 2, 1, leap=True) == date(2023, 3, 22)
    assert festivals.lunar_to_solar(2023, 3, 1) == date(2023, 4, 20)
    assert festivals.lunar_to_solar(2026, 12, 30) is None  # 该年腊月只有 29 天
    assert festivals.chinese_new_year(2026) == date(2026, 2, 17)
    assert festivals.chinese_new_year(1901) == date(1901, 2, 19)


def test_lunar_round_trip_all_years() -> None:
    checked = 0
    for year in range(1901, 2101):
        for month in range(1, 13):
            for day in (1, 15):
                solar = festivals.lunar_to_solar(year, month, day)
                assert solar is not None, (year, month, day)
                back = festivals.solar_to_lunar(solar)
                assert back is not None, solar
                assert (back.year, back.month, back.day, back.is_leap) == (
                    year,
                    month,
                    day,
                    False,
                ), (year, month, day, back)
                checked += 1
    assert checked == 200 * 12 * 2


def test_lunar_leap_month_round_trip() -> None:
    for year, natural_month in ((2023, 2), (2025, 6), (2033, 11)):
        solar = festivals.lunar_to_solar(year, natural_month, 1, leap=True)
        assert solar is not None, (year, natural_month)
        back = festivals.solar_to_lunar(solar)
        assert back is not None and back.is_leap and back.month == natural_month, back
        assert back.text.startswith("闰"), back.text


def test_lunar_out_of_range() -> None:
    assert festivals.solar_to_lunar(date(1901, 2, 18)) is None
    assert festivals.solar_to_lunar(date(2101, 1, 29)) is None
    assert festivals.lunar_to_solar(1900, 1, 1) is None
    assert festivals.lunar_to_solar(2101, 1, 1) is None
    assert festivals.chinese_new_year(2101) == date(2101, 1, 29)


def test_festivals_on() -> None:
    assert festivals.festivals_on(date(2026, 9, 25)) == ["中秋节"]
    assert festivals.festivals_on(date(2026, 10, 1)) == ["国庆节"]
    assert festivals.festivals_on(date(2020, 10, 1)) == ["中秋节", "国庆节"]
    assert festivals.festivals_on(date(2026, 2, 16)) == ["除夕"]
    assert festivals.festivals_on(date(2026, 5, 20)) == ["网络情人节"]
    assert festivals.festivals_on(date(2023, 3, 22)) == []  # 闰二月初一不算龙抬头
    assert festivals.festivals_on(date(2023, 2, 21)) == ["龙抬头"]
    assert festivals.festivals_on(date(2026, 9, 21)) == []
    assert festivals.today_suffix(date(2026, 9, 25)) == "（中秋节，农历八月十五）"
    assert festivals.today_suffix(date(2020, 10, 1)) == "（中秋节、国庆节，农历八月十五）"
    assert festivals.today_suffix(date(2026, 9, 21)) == ""


def test_known_festival_dates() -> None:
    assert festivals.lunar_to_solar(2026, 5, 5) == date(2026, 6, 19)  # 端午
    assert festivals.lunar_to_solar(2026, 1, 15) == date(2026, 3, 3)  # 元宵
    assert festivals.lunar_to_solar(2026, 9, 9) == date(2026, 10, 18)  # 重阳
    assert festivals.lunar_to_solar(2024, 8, 15) == date(2024, 9, 17)
    assert festivals.lunar_to_solar(2025, 8, 15) == date(2025, 10, 6)
    qingming = [festivals.festivals_on(date(y, 4, d)) for y, d in (
        (2020, 4), (2021, 4), (2022, 5), (2023, 5), (2024, 4), (2025, 4), (2026, 5),
    )]
    assert all("清明节" in names for names in qingming), qingming
    assert festivals.festivals_on(date(2026, 5, 10)) == ["母亲节"]
    assert festivals.festivals_on(date(2026, 6, 21)) == ["父亲节"]


def test_upcoming_line() -> None:
    assert festivals.upcoming_line(date(2026, 9, 25)) == (
        "临近节日: 国庆节（10月01日，6天后）"
    )
    assert festivals.upcoming_line(date(2026, 9, 26)) == (
        "临近节日: 国庆节（10月01日，5天后）"
    )
    middle = festivals.upcoming_line(date(2026, 9, 21))
    assert "中秋节（9月25日，4天后）" in middle and "国庆节" in middle
    # 除夕/春节相邻时两个都提示
    assert festivals.upcoming_line(date(2027, 1, 28)) == (
        "临近节日: 除夕（2月05日，8天后）；春节（2月06日，9天后）"
    )
    # C 档（只在当天）不进「临近」
    assert festivals.upcoming_line(date(2026, 5, 20)) == ""
    # 无临近节日
    assert festivals.upcoming_line(date(2026, 5, 13)) == ""


def test_prompt_block_and_now_line() -> None:
    now = datetime(2026, 9, 25, 12, 0)
    block = temporal.prompt_block(now)
    assert "今天: 2026年09月25日 周五（中秋节，农历八月十五）" in block
    assert "临近节日: 国庆节（10月01日，6天后）" in block
    assert "放假/调休以官方通知为准" in block
    assert "（中午·中秋节）" in temporal.now_line(now)

    # 普通日子不产生节日噪声
    plain = temporal.prompt_block(datetime(2026, 5, 13, 12, 0))
    assert "临近节日" not in plain
    assert "今天: 2026年05月13日 周三\n" in plain
    assert "·" not in temporal.now_line(datetime(2026, 5, 13, 12, 0)).split("（")[1].split("）")[0]


def test_get_current_time_tool() -> None:
    from agents.tools.system import GetCurrentTimeTool

    original = temporal.now
    temporal.now = lambda: datetime(2026, 9, 25, 12, 0)  # type: ignore[assignment]
    try:
        result = asyncio.run(GetCurrentTimeTool().execute())
    finally:
        temporal.now = original  # type: ignore[assignment]
    assert "（中秋节，农历八月十五）" in result.output
    assert "临近节日: 国庆节（10月01日，6天后）" in result.output
    assert result.data["festivals_today"] == ["中秋节"]
    assert result.data["festivals_upcoming"] == [
        {"name": "国庆节", "date": "2026-10-01", "days_until": 6}
    ]


def main() -> bool:
    tests = [
        test_lunar_conversion_known_dates,
        test_lunar_round_trip_all_years,
        test_lunar_leap_month_round_trip,
        test_lunar_out_of_range,
        test_festivals_on,
        test_known_festival_dates,
        test_upcoming_line,
        test_prompt_block_and_now_line,
        test_get_current_time_tool,
    ]
    failed: list[str] = []
    for test in tests:
        try:
            test()
            print(f"  ok   {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed.append(test.__name__)
            print(f"  FAIL {test.__name__}: {exc!r}")
    print(f"festival checks: {len(tests) - len(failed)}/{len(tests)} passed")
    return not failed


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
