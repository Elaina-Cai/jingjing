"""TTL 与"作废要出声"（2026-10-30 事故的锁）。"""
from __future__ import annotations

import ast
from pathlib import Path

from db import world_db
from loop import outbox

ROOT = Path(__file__).resolve().parents[2]


def test_default_ttl_reads_the_knob(monkeypatch) -> None:
    assert world_db._ttl_default() == 600.0
    monkeypatch.setenv("YINYUE_QUEUE_TTL_S", "120")
    assert world_db._ttl_default() == 120.0
    monkeypatch.setenv("YINYUE_QUEUE_TTL_S", "not-a-number")
    assert world_db._ttl_default() == 600.0
    monkeypatch.setenv("YINYUE_QUEUE_TTL_S", "5")
    assert world_db._ttl_default() == 30.0


def test_expired_says_something(monkeypatch) -> None:
    seen: list[tuple] = []
    monkeypatch.setattr(outbox, "warn", lambda event, **kw: seen.append((event, kw)))
    outbox.note_expired(102, "wxid_x", "你说吧，我这会儿空着。", 644.0)
    assert seen and seen[0][0] == "outbound_expired"
    kw = seen[0][1]
    assert kw["qid"] == 102 and kw["waited_s"] == 644.0
    assert kw["preview"].startswith("你说吧")


def test_both_callers_pass_the_callback() -> None:
    """两个 expire() 调用点都必须接上回调，否则作废又变成无声的。"""
    for rel in ("loop/desk.py", "loop/run.py"):
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8").lstrip("\ufeff"))
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "expire"]
        assert calls, rel
        for call in calls:
            assert any(k.arg == "on_expired" for k in call.keywords), rel
