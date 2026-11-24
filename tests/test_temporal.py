"""时间感知（time awareness）回归测试。

覆盖：
- 相对时间格式化 / 解析
- prompt 时间块 / 消息时间提示
- 静默时段与一天中的时段
- 群聊缓冲、对话历史的带时间戳输出
- 时间快路径与 get_current_time 工具
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import temporal


def test_format_stamp() -> None:
    now = datetime(2026, 9, 21, 14, 32, 7)
    assert temporal.format_stamp(now, now) == "刚刚"
    assert temporal.format_stamp(now - timedelta(minutes=3), now) == "3分钟前"
    assert temporal.format_stamp(now - timedelta(hours=5), now) == "今天 09:32（5小时前）"
    assert temporal.format_stamp(now - timedelta(days=1), now) == "昨天 14:32"
    assert temporal.format_stamp(now - timedelta(days=2), now) == "前天 14:32"
    assert temporal.format_stamp(now - timedelta(days=3), now) == "3天前 14:32"
    assert temporal.format_stamp(datetime(2026, 8, 19, 8, 0), now) == "08-19 08:00"
    assert temporal.format_stamp(datetime(2025, 12, 31, 23, 59), now) == "2025-12-31 23:59"
    assert temporal.format_absolute(datetime(2026, 9, 21, 14, 32), now) == "2026-09-21 14:32"


def test_describe_age() -> None:
    now = datetime(2026, 9, 21, 14, 32, 7)
    assert temporal.describe_age(now - timedelta(seconds=20), now) == "刚刚"
    assert temporal.describe_age(now - timedelta(minutes=8), now) == "8分钟前"
    assert temporal.describe_age(now - timedelta(days=1), now) == "昨天"
    assert temporal.describe_age(now - timedelta(days=10), now) == "1周前"
    assert temporal.describe_age(now - timedelta(days=70), now) == "2个月前"


def test_parse_relative() -> None:
    base = temporal.now().replace(second=0, microsecond=0)
    assert temporal.parse_datetime("昨天 08:00") == (base - timedelta(days=1)).replace(
        hour=8, minute=0,
    )
    assert temporal.parse_datetime("3天前").date() == (base - timedelta(days=3)).date()
    assert temporal.parse_datetime("今天下午三点") == base.replace(hour=15, minute=0)
    assert temporal.parse_datetime("今晚八点半") == base.replace(hour=20, minute=30)
    assert temporal.parse_datetime("上午十点二十") == base.replace(hour=10, minute=20)
    assert temporal.parse_datetime("上周三") is not None
    assert temporal.parse_datetime("2026-09-19") == datetime(2026, 9, 19, 0, 0)


def test_prompt_block_and_lines() -> None:
    now = datetime(2026, 9, 21, 14, 32, 7)
    block = temporal.prompt_block(now)
    assert "[时间感知]" in block
    assert "今天: 2026年09月21日 周一" in block
    assert "明天: 2026年09月22日 周二" in block
    assert "消息发送时间" in block
    line = temporal.message_time_line(now - timedelta(hours=2), now)
    assert "今天 12:32" in line
    assert "2小时前" in line
    assert "下午" in temporal.now_line(now)


def test_quiet_hours_and_day_period() -> None:
    assert temporal.is_quiet_hours(datetime(2026, 9, 21, 2, 0)) is True
    assert temporal.is_quiet_hours(datetime(2026, 9, 21, 12, 0)) is False
    assert temporal.day_period(datetime(2026, 9, 21, 2, 0)) == "凌晨"
    assert temporal.day_period(datetime(2026, 9, 21, 9, 0)) == "上午"
    assert temporal.day_period(datetime(2026, 9, 21, 15, 0)) == "下午"
    assert temporal.day_period(datetime(2026, 9, 21, 21, 0)) == "晚上"
    assert temporal.is_weekend(datetime(2026, 9, 20, 12, 0)) is True
    assert temporal.is_weekend(datetime(2026, 9, 21, 12, 0)) is False


def test_group_state_stamps() -> None:
    from legacy_brain.group_state import BufferedMessage, GroupState

    now = temporal.now()
    state = GroupState(chat_id="test")
    state.push_message(BufferedMessage(
        sender_id="u1", sender_name="张三", content="昨天说的话",
        created_at=now - timedelta(days=1),
    ))
    state.push_message(BufferedMessage(
        sender_id="u2", sender_name="李四", content="刚刚说的话",
        created_at=now - timedelta(minutes=2),
    ))
    text = state.get_recent_text(now=now)
    assert "昨天" in text and "张三" in text
    assert "2分钟前" in text and "李四" in text


def test_conversation_history_stamps() -> None:
    from agents.conversational import ConversationalAgent

    agent = object.__new__(ConversationalAgent)
    agent._persona = SimpleNamespace(name="鲸鲸")
    now = datetime(2026, 9, 21, 14, 32, 7)
    history = [
        SimpleNamespace(role="user", content="你好", created_at=now - timedelta(days=1)),
        SimpleNamespace(role="assistant", content="在的", created_at=now - timedelta(minutes=3)),
    ]
    text = agent._format_history(history, now=now)
    assert "[用户|昨天 14:32]" in text
    assert "[鲸鲸|3分钟前]" in text
    assert "时间感知" in agent._with_time_block("你是鲸鲸")


def test_get_current_time_tool() -> None:
    from agents.tools.system import GetCurrentTimeTool

    result = asyncio.run(GetCurrentTimeTool().execute())
    assert "今天:" in result.output
    assert "明天:" in result.output
    assert result.data["today"]
    assert result.data["tomorrow"]

    offset = asyncio.run(GetCurrentTimeTool().execute(offset_days=3))
    assert offset.data["offset_days"] == 3
    assert "目标时间:" in offset.output


def test_search_messages_date_range() -> None:
    from agents.tools.search import SearchMessagesTool

    class _FakeUQ:
        def __init__(self):
            self.kwargs = None

        def search_contacts(self, *args, **kwargs):
            return []

        def search_messages(self, **kwargs):
            self.kwargs = kwargs
            return []

        def get_contact_display_name(self, value):
            return value

    uq = _FakeUQ()
    tool = SearchMessagesTool(repo=None, unified_query=uq)
    tool.set_context(SimpleNamespace(extra={
        "chat_id": "chat", "is_group": False, "sender_id": "owner",
        "platform": "wechat", "account_id": "acct",
    }))
    tool._is_owner = lambda *args, **kwargs: True
    result = asyncio.run(tool.execute(
        chat_id="somebody", start_time="昨天 08:00", end_time="今天",
    ))
    assert not result.is_error
    assert uq.kwargs["since"] is not None
    assert uq.kwargs["until"] is not None
    assert uq.kwargs["since"] < uq.kwargs["until"]

    # 只传 end_time：窗口应当从 end_time 往前回推默认小时数，而不是从“现在”回推。
    window_hours = tool._default_hours_back()
    uq.kwargs = None
    asyncio.run(tool.execute(chat_id="somebody", end_time="2026-01-01"))
    assert uq.kwargs["until"] == datetime(2026, 1, 1, 23, 59, 59)
    assert uq.kwargs["since"] == datetime(2026, 1, 1, 23, 59, 59) - timedelta(
        hours=window_hours,
    )

    # start_time 优先于 hours_back。
    uq.kwargs = None
    asyncio.run(tool.execute(
        chat_id="somebody", start_time="2026-01-01", hours_back=1,
    ))
    assert uq.kwargs["since"] == datetime(2026, 1, 1, 0, 0, 0)
    assert uq.kwargs["until"] is None

    # hours_back=0 表示不限时间。
    uq.kwargs = None
    asyncio.run(tool.execute(chat_id="somebody", hours_back=0))
    assert uq.kwargs["since"] is None
    assert uq.kwargs["until"] is None


def main() -> bool:
    tests = [
        test_format_stamp,
        test_describe_age,
        test_parse_relative,
        test_prompt_block_and_lines,
        test_quiet_hours_and_day_period,
        test_group_state_stamps,
        test_conversation_history_stamps,
        test_get_current_time_tool,
        test_search_messages_date_range,
    ]
    failed: list[str] = []
    for test in tests:
        try:
            test()
            print(f"  ok   {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed.append(test.__name__)
            print(f"  FAIL {test.__name__}: {exc!r}")
    print(f"temporal checks: {len(tests) - len(failed)}/{len(tests)} passed")
    return not failed


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
