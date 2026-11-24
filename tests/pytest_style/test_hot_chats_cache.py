"""自发闸的「谁在说话」查询不许每 400ms 跑一次。

2026-11-22 实测：引擎空转时仍占约 8% CPU，主因是自发闸每轮都查一次近 30 分钟
有人说话的会话——一天二十多万次。判据窗口是 1800 秒，20 秒缓存不影响结论。
"""
from __future__ import annotations

from loop import run


def _reset():
    run._HOT_CACHE.update({"at": 0.0, "value": frozenset()})


def test_repeated_polls_reuse_the_same_query(monkeypatch):
    _reset()
    calls = []

    def fake_since(since, limit=50):
        calls.append((since, limit))
        return [{"chat_id": "room@chatroom"}]

    monkeypatch.setattr(run.ports, "peer_events_since", fake_since)
    first = run._hot_chats(1000.0, 1800)
    second = run._hot_chats(1010.0, 1800)
    assert first == second == {"room@chatroom"}
    assert len(calls) == 1


def test_cache_expires_and_refreshes(monkeypatch):
    _reset()
    calls = []
    monkeypatch.setattr(run.ports, "peer_events_since",
                        lambda since, limit=50: calls.append(limit) or [])
    run._hot_chats(1000.0, 1800)
    run._hot_chats(1030.0, 1800)
    assert len(calls) == 2
