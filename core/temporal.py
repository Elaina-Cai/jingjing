"""统一时间感知（time awareness）基础设施。

设计目标
========
1. **一个时钟**：全项目内部统一使用「系统本地 naive datetime」作为应用时钟，
   与 ``Message`` / ``Conversation`` 等表写入 ``datetime.now()`` 的方式保持一致。
2. **一处格式化**：prompt、工具输出、群聊缓冲、记忆上下文都通过本模块输出
   人类可读的时间说法（今天/昨天/3分钟前/09-19 08:00），避免各写各的。
3. **可选展示时区**：通过 ``TEMPORAL_TIMEZONE``（IANA 名称，如 Asia/Shanghai）
   控制对用户/模型展示的时区；不配置则使用系统本地时区。
4. **可解析**：把模型/配置里传来的 ISO、``YYYY-MM-DD``、``MM-DD HH:MM``、
   ``HH:MM``、unix 时间戳等统一解析成应用时钟。

这里刻意不引入重量级依赖，任何模块都可以安全 import。
"""
from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any

from core import festivals
from safelog import warn

try:  # pragma: no cover - zoneinfo 在 Python 3.9+ 可用，Windows 需要 tzdata
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    warn("core/temporal.py:27 Exception", err=repr(__import__("sys").exc_info()[1]))
    ZoneInfo = None  # type: ignore[assignment]


WEEKDAYS_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
_NUMERIC = (int, float)

# 日期的中文写法需要先做一次轻量归一化，才能交给 strptime。
_CN_TIME_RE = re.compile(r"\s*(?:[（(]?\s*周[一二三四五六日天]\s*[）)]?)\s*")


def _temporal_settings() -> Any:
    try:
        from core.config.settings import config

        return getattr(config, "temporal", None)
    except Exception:
        warn("core/temporal.py:43 Exception", err=repr(__import__("sys").exc_info()[1]))
        return None


def configured_timezone_name() -> str:
    """返回配置的展示时区名；空字符串表示使用系统本地时区。"""
    settings = _temporal_settings()
    name = str(getattr(settings, "timezone", "") or "").strip()
    if not name:
        name = (os.environ.get("TEMPORAL_TIMEZONE") or "").strip()
    if not name:
        name = (os.environ.get("TZ") or "").strip()
    return name


def get_timezone() -> tzinfo:
    """返回展示用 tzinfo；无法解析时退回系统本地时区。"""
    name = configured_timezone_name()
    if name and ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception:
            warn("core/temporal.py:64 Exception", err=repr(__import__("sys").exc_info()[1]))
    local = datetime.now().astimezone().tzinfo
    return local or timezone.utc


def now() -> datetime:
    """应用时钟：系统本地 naive 时间。

    所有落库的 ``created_at`` 都是通过 ``datetime.now()`` 写入的，因此内部
    比较必须使用同一个墙钟，否则相对时间会整体偏移。
    """
    return datetime.now()


# 语义别名，方便读代码时区分「应用时钟」和「带时区的展示时钟」。
now_naive = now


def now_aware() -> datetime:
    """当前时刻，带配置好的展示时区。"""
    return datetime.now().astimezone(get_timezone())


def to_naive_local(value: datetime | None) -> datetime | None:
    """把 datetime 统一成应用时钟（naive 本地墙钟）——**全仓唯一的换算入口**。

    - ``None``：原样返回 ``None``（可空列/可空参数是常态，**不许**让调用方各自加守卫）；
    - naive：视为系统本地时间，原样返回；
    - aware：先转到系统本地时区，再去掉 tzinfo。

    为什么必须走这里（而不是各文件自己 ``astimezone().replace(tzinfo=None)``）：
    「落库一律 naive 本地墙钟」是本项目的时钟约定，散落的复制件一旦有一处写成
    ``astimezone(timezone.utc)`` 或漏掉 ``replace``，读出来的相对时间就整体偏 8
    小时，而且只在真实流量上显形（详见 ``docs/架构所有权地图.md`` G3）。
    ``tests/test_time_convention.py`` 有 AST 守卫禁止生产代码再写第二份。
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone().replace(tzinfo=None)


def parse_naive_local(value: Any, default: datetime | None = None) -> datetime | None:
    """datetime / ISO 文本 → 应用时钟（naive 本地墙钟）；解析不出来给 ``default``。

    旧实现把这段「datetime 直转、字符串先 ``fromisoformat`` 再转」抄了三份
    （``core/terminal/shadow.py``、``core/terminal/command_shadow.py``、
    ``core/tasks/mapping.py``），各自的兜底与失败口径还不一样（有的回
    ``datetime.now()``、有的回 ``None``）。这里只保留一处解析：
    ``default`` 由调用方按自己的语义传（要 ``now()`` 就传 ``now()``）。

    接受 ``YYYY-MM-DD HH:MM:SS``（空格分隔）与 ISO（``T`` / ``+08:00`` / ``Z``）。
    """
    if value is None:
        return default
    if isinstance(value, datetime):
        return to_naive_local(value)
    text = str(value).strip()
    if not text:
        return default
    try:
        parsed = datetime.fromisoformat(text.replace(" ", "T").replace("Z", "+00:00"))
    except ValueError:
        warn("core/temporal.py:128 ValueError", err=repr(__import__("sys").exc_info()[1]))
        return default
    return to_naive_local(parsed)


_RELATIVE_UNITS = {
    "分": "minutes", "分钟": "minutes",
    "时": "hours", "小时": "hours",
    "天": "days", "日": "days",
    "周": "weeks", "星期": "weeks",
}
_CN_WEEKDAYS = {
    "一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6,
}


_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}


def _cn_number(text: str) -> int | None:
    text = str(text or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if "十" in text:
        left, _, right = text.partition("十")
        tens = _CN_DIGITS.get(left, 1) if left else 1
        ones = _CN_DIGITS.get(right, 0) if right else 0
        return tens * 10 + ones
    if len(text) == 1:
        return _CN_DIGITS.get(text)
    return None


def _parse_clock(text: str) -> tuple[int, int] | None:
    text = str(text or "").strip()
    m = re.match(
        r"^(?:凌晨|早上|上午|中午|下午|傍晚|晚上|夜里|夜间)?\s*"
        r"(\d{1,2})\s*[:：点时]\s*(\d{1,2})?\s*分?$",
        text,
    )
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
    else:
        m = re.match(
            r"^(?:凌晨|早上|上午|中午|下午|傍晚|晚上|夜里|夜间)?\s*"
            r"([零〇一二三四五六七八九十]{1,3})\s*[点时]\s*"
            r"(半|([零〇一二三四五六七八九十]{1,3})\s*分?)?$",
            text,
        )
        if not m:
            return None
        hour = _cn_number(m.group(1)) or 0
        if m.group(2) == "半":
            minute = 30
        else:
            minute = _cn_number(m.group(3)) or 0
    if hour > 23 or minute > 59:
        return None
    if any(p in text for p in ("下午", "傍晚", "晚上", "夜里", "夜间")) and hour < 12:
        hour += 12
    elif "中午" in text and hour < 12:
        hour = 12
    return hour, minute


def _parse_relative(text: str) -> datetime | None:
    """解析「昨天 08:00」「3天前」「上周三」「下午三点」等中文相对时间。"""
    text = str(text or "").strip()
    if not text:
        return None
    base = now()

    m = re.match(r"^(\d+)\s*(分钟|分|小时|时|天|日|周|个月|月|年)\s*(前|之前|以前|后|之后|以后)$", text)
    if m:
        amount = int(m.group(1))
        unit_key = m.group(2)
        if unit_key in ("月", "个月"):
            delta = timedelta(days=30 * amount)
        elif unit_key == "年":
            delta = timedelta(days=365 * amount)
        else:
            delta = timedelta(**{_RELATIVE_UNITS.get(unit_key, "days"): amount})
        forward = any(key in m.group(3) for key in ("后",))
        return base + delta if forward else base - delta

    day_map = {
        "今天": 0, "今日": 0, "昨晚": -1, "昨天": -1, "昨日": -1, "前天": -2,
        "明晚": 1, "明天": 1, "明日": 1, "后天": 2, "大后天": 3, "今晚": 0,
    }
    m = re.match(
        r"^(今天|今日|昨天|昨日|昨晚|前天|明天|明日|明晚|后天|大后天|今晚)\s*(.*)$",
        text,
    )
    if m:
        dt = base + timedelta(days=day_map[m.group(1)])
        rest = m.group(2).strip()
        if rest:
            clock = _parse_clock(rest)
            if clock:
                hour, minute = clock
                if m.group(1) in ("今晚", "明晚", "昨晚") and hour < 12:
                    hour += 12
                dt = dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return dt

    m = re.match(r"^(本周|这周|上周|下周|周|礼拜|星期)\s*([一二三四五六日天])$", text)
    if m:
        prefix, weekday_text = m.group(1), m.group(2)
        target = _CN_WEEKDAYS.get(weekday_text)
        if target is None:
            return None
        monday = base - timedelta(days=base.weekday())
        if prefix == "上周":
            weeks = -1
        elif prefix == "下周":
            weeks = 1
        else:
            weeks = 0
        return (monday + timedelta(days=weeks * 7 + target)).replace(
            hour=0, minute=0, second=0, microsecond=0,
        )

    clock = _parse_clock(text)
    if clock:
        return base.replace(hour=clock[0], minute=clock[1], second=0, microsecond=0)

    return None


def parse_datetime(value: Any, default: datetime | None = None) -> datetime | None:
    """尽量把任意常见时间表示解析成应用时钟（naive local）。

    支持：datetime / date / 数字时间戳 / ISO 字符串 / 常见中英文格式，
    以及 ``MM-DD HH:MM``、``MM-DD``、``HH:MM`` 这类省略年份的写法。
    """
    if value is None or value == "":
        return default
    if isinstance(value, datetime):
        return to_naive_local(value)
    if isinstance(value, date) and not isinstance(value, datetime):
        return datetime(value.year, value.month, value.day)
    if isinstance(value, _NUMERIC):
        try:
            return datetime.fromtimestamp(float(value))
        except (OverflowError, OSError, ValueError):
            warn("core/temporal.py:278 (OverflowError, OSError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
            return default

    text = str(value).strip()
    if not text:
        return default

    # ISO 8601（含 Z、时区偏移）。
    iso_text = text.replace("Z", "+00:00").replace("z", "+00:00")
    try:
        return to_naive_local(datetime.fromisoformat(iso_text))
    except ValueError:
        warn("core/temporal.py:289 ValueError", err=repr(__import__("sys").exc_info()[1]))

    # 中文写法："2026年09月21日 周一 14:32" -> "2026-09-21 14:32"
    chinese = text.replace("年", "-").replace("月", "-").replace("日", " ")
    chinese = chinese.replace("号", " ").replace("时", ":").replace("分", ":")
    chinese = _CN_TIME_RE.sub(" ", chinese).strip()
    if chinese != text:
        try:
            return to_naive_local(datetime.fromisoformat(chinese))
        except ValueError:
            warn("core/temporal.py:299 ValueError", err=repr(__import__("sys").exc_info()[1]))

    # 英文/斜杠日期。
    for fmt in (
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y/%m/%d",
        "%Y.%m.%d %H:%M:%S",
        "%Y.%m.%d %H:%M",
        "%Y.%m.%d",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            warn("core/temporal.py:316 ValueError", err=repr(__import__("sys").exc_info()[1]))
            continue

    relative = _parse_relative(text)
    if relative is not None:
        return relative

    # 省略年份 / 只有时分。
    reference = now()
    partial_formats = {
        "%m-%d %H:%M": lambda p: p.replace(year=reference.year),
        "%m-%d": lambda p: p.replace(year=reference.year),
        "%H:%M": lambda p: p.replace(
            year=reference.year, month=reference.month, day=reference.day,
        ),
    }
    for fmt, fixup in partial_formats.items():
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            warn("core/temporal.py:335 ValueError", err=repr(__import__("sys").exc_info()[1]))
            continue
        try:
            return fixup(parsed)
        except ValueError:
            warn("core/temporal.py:339 ValueError", err=repr(__import__("sys").exc_info()[1]))
            continue

    return default


def weekday_cn(value: datetime | None = None) -> str:
    dt = parse_datetime(value) or now()
    return WEEKDAYS_CN[dt.weekday()]


def to_display(value: Any) -> datetime:
    """把应用时钟转成展示时区（aware）后的 datetime。"""
    dt = parse_datetime(value) or now()
    return dt.astimezone(get_timezone())


def utc_offset_label(value: Any = None) -> str:
    """返回 UTC+08:00 形式的时区标签。"""
    dt = to_display(value)
    offset = dt.utcoffset() or timedelta(0)
    total = int(offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    hours, remainder = divmod(total, 3600)
    minutes = remainder // 60
    return f"UTC{sign}{hours:02d}:{minutes:02d}"


def _days_between(later: date, earlier: date) -> int:
    return (later - earlier).days


def describe_age_seconds(seconds: float) -> str:
    """秒数 -> 相对年龄描述。"""
    seconds = float(seconds)
    if seconds < 0:
        seconds = abs(seconds)
        if seconds < 60:
            return "马上"
        if seconds < 3600:
            return f"{max(1, int(seconds // 60))}分钟后"
        if seconds < 86400:
            return f"{max(1, int(seconds // 3600))}小时后"
        return f"{max(1, int(seconds // 86400))}天后"
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{max(1, int(seconds // 60))}分钟前"
    if seconds < 86400:
        return f"{max(1, int(seconds // 3600))}小时前"
    days = int(seconds // 86400)
    if days == 1:
        return "昨天"
    if days == 2:
        return "前天"
    if days < 7:
        return f"{days}天前"
    if days < 30:
        return f"{max(1, days // 7)}周前"
    if days < 365:
        return f"{max(1, days // 30)}个月前"
    return f"{max(1, days // 365)}年前"


def describe_age(value: Any, now_value: Any = None) -> str:
    """把一个时间点描述成相对年龄：刚刚/N分钟前/昨天/3周前..."""
    dt = parse_datetime(value)
    if dt is None:
        return ""
    reference = parse_datetime(now_value) or now()
    return describe_age_seconds((reference - dt).total_seconds())


def format_stamp(
    value: Any,
    now_value: Any = None,
    *,
    include_relative: bool = True,
) -> str:
    """历史/上下文里每条消息使用的时间戳。

    规则（对模型最友好）::

        刚刚
        3分钟前
        今天 09:30（5小时前）
        昨天 09:10
        前天 08:00
        3天前 12:00
        09-19 08:00
        2025-12-31 23:59
    """
    dt = parse_datetime(value)
    if dt is None:
        return ""
    reference = parse_datetime(now_value) or now()
    seconds = (reference - dt).total_seconds()
    time_part = dt.strftime("%H:%M")
    same_year = dt.year == reference.year
    day_diff = _days_between(reference.date(), dt.date())

    if -60 <= seconds < 60:
        return "刚刚"
    if 0 <= seconds < 3600:
        return f"{max(1, int(seconds // 60))}分钟前"
    if 0 <= seconds < 86400 and day_diff == 0:
        hours = int(seconds // 3600)
        if include_relative and hours >= 1:
            return f"今天 {time_part}（{hours}小时前）"
        return f"今天 {time_part}"

    if day_diff == 0:
        # 未来 60 秒内已在上面返回；同一天未来时刻。
        return f"今天 {time_part}"
    if day_diff == 1:
        return f"昨天 {time_part}"
    if day_diff == 2:
        return f"前天 {time_part}"
    relative_days = _history_relative_days()
    if 2 < day_diff < relative_days:
        return f"{day_diff}天前 {time_part}"
    if -relative_days < day_diff < 0:
        return f"{-day_diff}天后 {time_part}" if day_diff <= -2 else f"明天 {time_part}"
    if same_year:
        return f"{dt:%m-%d %H:%M}"
    return f"{dt:%Y-%m-%d %H:%M}"


def format_absolute(value: Any, now_value: Any = None) -> str:
    """完整日期时间（YYYY-MM-DD HH:MM），用于摘要等需要跨天保存的场景。"""
    dt = parse_datetime(value)
    if dt is None:
        return ""
    return dt.strftime("%Y-%m-%d %H:%M")


def end_of_day(value: Any = None) -> datetime:
    dt = parse_datetime(value) or now()
    return dt.replace(hour=23, minute=59, second=59, microsecond=0)


def start_of_day(value: Any = None) -> datetime:
    dt = parse_datetime(value) or now()
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def is_same_day(left: Any, right: Any = None) -> bool:
    l_dt = parse_datetime(left)
    r_dt = parse_datetime(right) or now()
    if l_dt is None:
        return False
    return l_dt.date() == r_dt.date()


def day_period(value: Any = None) -> str:
    """一天中的时段：凌晨/清晨/上午/中午/下午/傍晚/晚上/深夜。"""
    hour = to_display(value).hour
    if hour < 5:
        return "凌晨"
    if hour < 8:
        return "清晨"
    if hour < 11:
        return "上午"
    if hour < 13:
        return "中午"
    if hour < 17:
        return "下午"
    if hour < 19:
        return "傍晚"
    if hour < 23:
        return "晚上"
    return "深夜"


def is_weekend(value: Any = None) -> bool:
    return to_display(value).weekday() >= 5


def _quiet_hours() -> tuple[int, int]:
    settings = _temporal_settings()

    def _hour(name: str, default: int) -> int:
        try:
            return int(getattr(settings, name, default) or 0)
        except (TypeError, ValueError):
            warn("core/temporal.py:524 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
            return default

    start = _hour("quiet_hours_start", 23) % 24
    end = _hour("quiet_hours_end", 7) % 24
    return start, end


def is_quiet_hours(value: Any = None) -> bool:
    """是否处于静默时段（默认 23:00 - 07:00，支持跨天）。"""
    start, end = _quiet_hours()
    if start == end:
        return False
    hour = to_display(value).hour
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def _display_clock(value: Any = None) -> tuple[str, str, str, str, str, str]:
    """返回展示时钟的（日期, 星期, 时分秒, 时区标签, 昨天, 明天）。"""
    display = to_display(value)
    today = display.strftime("%Y年%m月%d日")
    weekday = WEEKDAYS_CN[display.weekday()]
    clock = display.strftime("%H:%M:%S")
    tz_label = utc_offset_label(display)
    yesterday = (display - timedelta(days=1)).strftime("%Y年%m月%d日")
    tomorrow = (display + timedelta(days=1)).strftime("%Y年%m月%d日")
    return today, weekday, clock, tz_label, yesterday, tomorrow


def festival_today_suffix(now_value: Any = None) -> str:
    """「今天」行尾的节日后缀：'' 或 '（中秋节，农历八月十五）'。"""
    return festivals.today_suffix(to_display(now_value).date())


def festival_upcoming_line(now_value: Any = None) -> str:
    """「临近节日」提示行：'' 或 '临近节日: 国庆节（10月01日，6天后）'。"""
    return festivals.upcoming_line(to_display(now_value).date())


def _festival_short_suffix(now_value: Any = None) -> str:
    """now_line 用的短后缀：'' 或 '·中秋节、国庆节'。"""
    names = festivals.festivals_on(to_display(now_value).date())
    return ("·" + "、".join(names)) if names else ""


def now_line(now_value: Any = None) -> str:
    """一行当前时间，适合塞在 user prompt 末尾。"""
    today, weekday, clock, tz_label, _, _ = _display_clock(now_value)
    period = day_period(now_value)
    suffix = f"（{period}{'·周末' if is_weekend(now_value) else ''}{_festival_short_suffix(now_value)}）"
    return f"当前时间: {today} {weekday} {clock}{suffix} {tz_label}"


def _history_relative_days() -> int:
    settings = _temporal_settings()
    try:
        return int(getattr(settings, "history_relative_days", 7) or 7)
    except (TypeError, ValueError):
        warn("core/temporal.py:583 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
        return 7


def _stale_hours() -> int:
    settings = _temporal_settings()
    try:
        return int(getattr(settings, "stale_message_hours", 48) or 48)
    except (TypeError, ValueError):
        warn("core/temporal.py:591 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
        return 48


def prompt_block(now_value: Any = None, *, include_rules: bool = True) -> str:
    """注入 system prompt 的完整时间感知块。"""
    today, weekday, clock, tz_label, yesterday, tomorrow = _display_clock(now_value)
    display = to_display(now_value)
    lines = [
        "[时间感知]",
        f"当前时间: {today} {weekday} {clock}（{day_period(now_value)}"
        f"{'·周末' if is_weekend(now_value) else ''}）{tz_label}",
        f"当前时段: {day_period(now_value)}"
        f"{'（静默时段，非紧急不要打扰）' if is_quiet_hours(now_value) else ''}",
        f"今天: {today} {weekday}{festival_today_suffix(now_value)}",
        f"昨天: {yesterday} {WEEKDAYS_CN[(display - timedelta(days=1)).weekday()]}",
        f"明天: {tomorrow} {WEEKDAYS_CN[(display + timedelta(days=1)).weekday()]}",
    ]
    upcoming = festival_upcoming_line(now_value)
    if upcoming:
        lines.append(upcoming)
    if include_rules:
        lines.extend([
            "- 「今天 14:32 / 昨天 09:10 / 3分钟前」都是消息发送时间；"
            "对照当前时间判断「多久没聊了」这类时效问题、也据此调整语气；"
            "旧消息别当成刚发生，必要时说明时间差；深夜/静默时段别过度活跃或主动打扰。",
            "- 「明天/后天/上周三」以当前时间为基准推算；要精确时间就调 get_current_time；"
            "节日看「今天」行，放假/调休以官方通知为准。",
            "- 查「上周/上个月/历史记录」时，搜索必须带时间范围"
            "（默认只覆盖最近 24 小时，别查不到就说没有）。",
        ])
    if include_rules:
        stale_hours = _stale_hours()
        if stale_hours:
            lines.append(
                f"- 超过 {stale_hours} 小时的消息按旧消息对待，回答时说明时间差。"
            )
    return "\n".join(lines)


def message_time_line(message_time: Any = None, now_value: Any = None) -> str:
    """本轮用户消息的发送时间提示。"""
    if message_time is None:
        return f"本条消息发送时间: 刚刚（{now_line(now_value)}）"
    stamp = format_stamp(message_time, now_value)
    if not stamp:
        return f"本条消息发送时间: 刚刚（{now_line(now_value)}）"
    age = describe_age(message_time, now_value)
    if age and age not in stamp:
        return f"本条消息发送时间: {stamp}（{age}）"
    return f"本条消息发送时间: {stamp}"


def elapsed_phrase(since: Any, now_value: Any = None) -> str:
    """事件距今多久，用于触发原因、路由解释等。"""
    return describe_age(since, now_value) or "刚刚"


def day_window_text(value: Any = None) -> str:
    """本周的起止日期，用于工具补全「这周」的语义。"""
    dt = parse_datetime(value) or now()
    monday = dt - timedelta(days=dt.weekday())
    sunday = monday + timedelta(days=6)
    return f"{monday:%Y-%m-%d}（周一）~ {sunday:%Y-%m-%d}（周日）"


def compact_stamp(value: Any, now_value: Any = None) -> str:
    """不包含「今天/昨天」相对词、但保留完整日期的时间。"""
    return format_absolute(value)
