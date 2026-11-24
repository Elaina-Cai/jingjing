"""情绪时间戳解析不许每试一种格式就写一条告警。

2026-11-22 实测：`2026-10-23T12:00:00` 明明能被第三种格式解析，前两次失败照样各写一条
`ValueError` 进 silent_warnings.log——正常路径上的噪声把真正的故障埋了。
"""
from __future__ import annotations

from datetime import datetime

from core.context import emotion


def test_iso_stamp_parses_without_a_warning(monkeypatch):
    calls = []
    monkeypatch.setattr(emotion, "warn", lambda *a, **kw: calls.append((a, kw)))
    got = emotion._parse_stamp("2026-10-23T12:00:00")
    assert got == datetime(2026, 10, 23, 12, 0, 0)
    assert calls == []


def test_unparseable_stamp_warns_once(monkeypatch):
    calls = []
    monkeypatch.setattr(emotion, "warn", lambda *a, **kw: calls.append((a, kw)))
    assert emotion._parse_stamp("不是时间") is None
    assert len(calls) == 1
    assert calls[0][0][0] == "emotion_stamp_unparsed"
