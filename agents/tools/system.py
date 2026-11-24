from __future__ import annotations

import json
from datetime import timedelta

from agents.tools.base import BaseTool, ToolResult
from safelog import warn


class EmitEventTool(BaseTool):
    name = "emit_event"
    description = "发布事件，通知其他Agent或触发后续动作。例如通知'有紧急消息'、'日报已生成'等"
    parameters = {
        "event_type": {"type": "string", "description": "事件类型，如 analysis_completed, proactive_trigger", "required": True},
        "payload": {"type": "string", "description": "事件数据，JSON格式"},
    }

    def __init__(self, event_bus=None):
        self.event_bus = event_bus

    async def execute(self, **kwargs) -> ToolResult:
        event_type_str = kwargs.get("event_type", "")
        payload_str = kwargs.get("payload", "{}")

        try:
            payload = json.loads(payload_str) if isinstance(payload_str, str) else payload_str
        except json.JSONDecodeError:
            warn("agents/tools/system.py:26 json.JSONDecodeError", err=repr(__import__("sys").exc_info()[1]))
            payload = {"raw": payload_str}

        if self.event_bus:
            from core.infra.event_bus import EventType, Event
            try:
                et = EventType(event_type_str)
            except ValueError:
                warn("agents/tools/system.py:33 ValueError", err=repr(__import__("sys").exc_info()[1]))
                et = EventType.PROACTIVE_TRIGGER

            self.event_bus.publish(Event(et, payload=payload))
            return ToolResult(f"已发布事件: {event_type_str}")
        else:
            return ToolResult(f"事件已记录(未发布): {event_type_str}")


class NotifyUserTool(BaseTool):
    name = "notify_user"
    description = "给用户发送系统通知弹窗，用于紧急消息提醒"
    parameters = {
        "title": {"type": "string", "description": "通知标题", "required": True},
        "message": {"type": "string", "description": "通知内容", "required": True},
    }

    async def execute(self, **kwargs) -> ToolResult:
        title = kwargs.get("title", "微信助理")
        message = kwargs.get("message", "")

        from notify.push import send_notification
        ok = send_notification(title, message)
        if ok:
            return ToolResult(f"已通知用户: {title} - {message[:50]}")
        return ToolResult("通知发送失败", error="notification_failed")


class GetCurrentTimeTool(BaseTool):
    name = "get_current_time"
    description = (
        "获取当前日期、时间、星期、时区、今天的节日和临近节日，以及昨天/明天/本周的日期。"
        "用于判断'明天'是哪天、计算时间差、确认相对日期等；"
        "可以用 offset_days/offset_hours 查询偏移后的时间。"
    )
    parameters = {
        "offset_days": {
            "type": "integer",
            "description": "相对今天的天数偏移：0=今天，1=明天，-1=昨天，7=下周此时",
        },
        "offset_hours": {
            "type": "integer",
            "description": "相对现在的小时偏移：2=两小时后，-3=三小时前",
        },
    }

    async def execute(self, **kwargs) -> ToolResult:
        from core import festivals, temporal

        try:
            offset_days = int(kwargs.get("offset_days") or 0)
        except (TypeError, ValueError):
            warn("agents/tools/system.py:84 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
            offset_days = 0
        try:
            offset_hours = int(kwargs.get("offset_hours") or 0)
        except (TypeError, ValueError):
            warn("agents/tools/system.py:88 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
            offset_hours = 0

        base = temporal.now()
        target = base + timedelta(days=offset_days, hours=offset_hours)
        display = temporal.to_display(target)
        weekday = temporal.weekday_cn(target)
        tz_label = temporal.utc_offset_label(target)
        base_date = temporal.to_display(base).date()
        festival_suffix = temporal.festival_today_suffix(base)
        upcoming_line = temporal.festival_upcoming_line(base)

        lines = [
            f"当前时间: {display.strftime('%Y年%m月%d日')} {weekday} "
            f"{display.strftime('%H:%M:%S')}（{tz_label}）",
            f"今天: {temporal.to_display(base).strftime('%Y年%m月%d日')} "
            f"{temporal.weekday_cn(base)}{festival_suffix}",
            f"昨天: {temporal.to_display(base - timedelta(days=1)).strftime('%Y年%m月%d日')} "
            f"{temporal.weekday_cn(base - timedelta(days=1))}",
            f"明天: {temporal.to_display(base + timedelta(days=1)).strftime('%Y年%m月%d日')} "
            f"{temporal.weekday_cn(base + timedelta(days=1))}",
            *([upcoming_line] if upcoming_line else []),
            f"本周: {temporal.day_window_text(base)}",
            f"当前时段: {temporal.day_period(base)}"
            f"{'（静默时段）' if temporal.is_quiet_hours(base) else ''}"
            f"{'（周末）' if temporal.is_weekend(base) else ''}",
        ]
        if offset_days or offset_hours:
            lines.append(
                f"目标时间: {display.strftime('%Y年%m月%d日')} {weekday} "
                f"{display.strftime('%H:%M:%S')}（{tz_label}）"
            )

        return ToolResult("\n".join(lines), data={
            "datetime": target.isoformat(),
            "date": target.strftime("%Y-%m-%d"),
            "time": target.strftime("%H:%M:%S"),
            "weekday": weekday,
            "timezone": temporal.configured_timezone_name() or str(temporal.get_timezone()),
            "utc_offset": tz_label,
            "today": base.strftime("%Y-%m-%d"),
            "yesterday": (base - timedelta(days=1)).strftime("%Y-%m-%d"),
            "tomorrow": (base + timedelta(days=1)).strftime("%Y-%m-%d"),
            "week_window": temporal.day_window_text(base),
            "day_period": temporal.day_period(base),
            "is_weekend": temporal.is_weekend(base),
            "is_quiet_hours": temporal.is_quiet_hours(base),
            "festivals_today": festivals.festivals_on(base_date),
            "festivals_upcoming": [
                {"name": o.name, "date": o.date.strftime("%Y-%m-%d"), "days_until": o.days_until}
                for o in festivals.upcoming(base_date)
            ],
            "offset_days": offset_days,
            "offset_hours": offset_hours,
        })
