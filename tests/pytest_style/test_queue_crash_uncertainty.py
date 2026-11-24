import contextlib
import sqlite3
import pytest

from db import world_db as world


def test_crash_recovery_cannot_replay_and_blocks_only_its_group(monkeypatch, tmp_path):
    path = str(tmp_path / "queue.db")
    @contextlib.contextmanager
    def connection():
        con = sqlite3.connect(path)
        con.row_factory = sqlite3.Row
        try:
            yield con
        finally:
            con.close()
    with connection() as con:
        for ddl in world._SCHEMA:
            con.execute(ddl)
        for i, group, seq, state in [(1, "a", 0, "sending"), (2, "a", 1, "pending"), (3, "b", 0, "pending")]:
            con.execute("insert into outbound_queue(id,idem_key,group_id,seq,chat_id,state,attempts,heartbeat_at,not_after) values(?,?,?,?,?,?,1,1,9999)",
                        (i, str(i), group, seq, "room", state))
        con.execute("insert into outbound_attempt(queue_id,attempt_no,result) values(1,1,'running')")
        con.commit()
    monkeypatch.setattr(world, "_open", connection)
    monkeypatch.setattr(world, "_read_conn", connection)
    assert world.reap_stale(now=1000) == 1
    assert world.reap_stale(now=1001) == 0
    assert world.claim_next(group_id="a", now=1001) is None
    with pytest.raises(ValueError, match="settle_claim_mismatch"):
        world.settle(1, False, owner="worker", attempt_no=1, now=1002)
    assert world.claim_next(group_id="a", now=1003) is None
    assert world.claim_next(group_id="b", now=1003)["id"] == 3
    stats = world.queue_stats()
    assert stats["unverified"] == 1
    assert any(r["id"] == 1 for r in stats["rows"])
    with connection() as con:
        row = con.execute("select state,ack,attempts from outbound_queue where id=1").fetchone()
        assert tuple(row) == ("unverified", "unconfirmed", 1)
        assert con.execute("select result from outbound_attempt where queue_id=1").fetchone()[0] == "aborted"


def test_interrupted_delivery_is_visible_as_uncertain():
    from loop.delivery_note import _one
    assert "不确定" in _one({"state": "unverified", "created_ts": 1})
