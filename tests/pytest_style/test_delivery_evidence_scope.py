import asyncio
import contextlib
import sqlite3

import pytest

from loop import delivery_note as note, ports


@pytest.mark.parametrize("state,ack", [("sent", "confirmed"), ("sent", "unconfirmed"), ("failed", ""), ("sending", ""), ("expired", "")])
def test_queue_status_does_not_claim_recipient_received_or_encourage_retry(state, ack):
    line = note._one({"state": state, "ack": ack})
    assert "对方那边收到了" not in line
    assert "现在说一遍" not in line
    assert "对方回了" not in note._with_reply(line, "room", 1, 10000)
    assert "对方没接" not in note._with_reply(line, "room", 1, 10000)
    if ack == "confirmed":
        assert "本地发件记录" in line


def test_note_and_cache_are_bound_to_turn_tenant(monkeypatch):
    from db import world_db
    note._CACHE.clear()
    calls = []
    def last(chat, **kw):
        calls.append((chat, kw))
        return [{"state": "sent", "ack": "confirmed" if kw["account_id"] == "a" else "unconfirmed", "created_ts": 999}]
    monkeypatch.setattr(world_db, "last_outbound", last)
    @ports.scoped_turn
    async def turn(db, *, platform, chat_id):
        assert note.note("other", now=1000) == ""
        return note.note(chat_id, now=1000)
    def finish(coro):
        try:
            coro.send(None)
        except StopIteration as done:
            return done.value
        raise AssertionError("scope-only coroutine unexpectedly suspended")
    a = finish(turn("data/platforms/wechat/a/account.db", platform="wechat", chat_id="room"))
    b = finish(turn("data/platforms/wechat/b/account.db", platform="wechat", chat_id="room"))
    assert "同文本地发件记录" in a and "不能证明属于本次发送" in a
    assert "尚无本地回读确认" in b
    assert len(calls) == 2
    assert calls[0][1] == {"platform": "wechat", "account_id": "a", "limit": 1}
    assert note.note("room", now=1000) == ""
    note._CACHE.clear()


def test_last_outbound_filters_before_limit(monkeypatch):
    from db import world_db
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("create table outbound_queue(id integer,platform text,account_id text,chat_id text)")
    conn.executemany("insert into outbound_queue values(?,?,?,?)", [(1,"wechat","a","room"), (2,"qq","a","room"), (3,"wechat","b","room")])
    @contextlib.contextmanager
    def connection():
        yield conn
    monkeypatch.setattr(world_db, "conn", connection)
    monkeypatch.setattr(world_db, "_qrow", dict)
    rows = world_db.last_outbound("room", platform="wechat", account_id="a", limit=1)
    assert [r["id"] for r in rows] == [1]
    assert world_db.last_outbound("room", platform="wechat") == []
    conn.close()
