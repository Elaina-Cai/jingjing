"""直发订单的状态必须真的读回来。

2026-11-22 实测：`read_json_checked` 返回的第二个值是状态名（"ok"/"missing"/
"corrupt"），`load_state` 却把 "ok" 也当成读坏了——每次加载都返回空表，磁盘上那条
已处理订单永远读不回来，重启后会被当成新订单再发一遍。silent_warnings.log 里
`direct_send.state_unreadable reason='ok'` 有 2592 条。
"""
from __future__ import annotations

import json

from loop import direct_send


def test_saved_items_are_loaded(monkeypatch, tmp_path):
    p = tmp_path / "direct_sent.json"
    p.write_text(json.dumps({"items": {"m1": {"at": "2026-11-22 10:00", "target": "c1"}}}),
                 encoding="utf-8")
    monkeypatch.setattr(direct_send, "state_path", lambda: str(p))
    calls = []
    monkeypatch.setattr(direct_send, "warn", lambda *a, **kw: calls.append((a, kw)))
    state = direct_send.load_state()
    assert state["items"] == {"m1": {"at": "2026-11-22 10:00", "target": "c1"}}
    assert calls == []


def test_corrupt_state_is_empty_and_reported(monkeypatch, tmp_path):
    p = tmp_path / "direct_sent.json"
    p.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(direct_send, "state_path", lambda: str(p))
    calls = []
    monkeypatch.setattr(direct_send, "warn", lambda *a, **kw: calls.append((a, kw)))
    assert direct_send.load_state() == {"items": {}}
    assert len(calls) == 1 and calls[0][0][0] == "direct_send.state_unreadable"
