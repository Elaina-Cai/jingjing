"""正常路径上的时间戳/缺文件不许写告警。

2026-11-22 实测 silent_warnings.log 里两类噪声占了 4115 条：
`snapshot_time_unparsed` 2498（分钟级时间戳先撞第一种格式就写一条）和
`actions_read_receipts_failed` 1617（新一天还没写过流水，FileNotFoundError）。
真正要看故障时，它们会把有用的行埋掉。
"""
from __future__ import annotations

import safelog

from core.infra import actions
from loop import snapshot


def test_minute_precision_snapshot_time_parses_without_warning(monkeypatch):
    calls = []
    monkeypatch.setattr(safelog, "warn", lambda *a, **kw: calls.append((a, kw)))
    assert snapshot._epoch_of("2026-11-09 16:00") is not None
    assert calls == []


def test_bad_snapshot_time_warns_once(monkeypatch):
    calls = []
    monkeypatch.setattr(safelog, "warn", lambda *a, **kw: calls.append((a, kw)))
    assert snapshot._epoch_of("不是时间") is None
    assert len(calls) == 1


def test_missing_ledger_file_is_empty_not_an_error(monkeypatch, tmp_path):
    monkeypatch.setattr(actions, "DIR", str(tmp_path))
    calls = []
    monkeypatch.setattr(actions, "warn", lambda *a, **kw: calls.append((a, kw)))
    assert actions.read_receipts(day="2000-01-01") == []
    assert calls == []
