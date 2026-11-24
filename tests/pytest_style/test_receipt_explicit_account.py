import sqlite3

from loop import receipts
from db import world_db


def test_known_account_never_uses_default_same_id_message(tmp_path, monkeypatch):
    for account, body in [("a", "different"), ("b", "expected")]:
        path = tmp_path / "data/platforms/wechat" / account / "account.db"
        path.parent.mkdir(parents=True)
        with sqlite3.connect(path) as con:
            con.execute("create table messages(id integer,chat_id text,content text,is_self integer, sender_id text generated always as (case when is_self=1 then 'self' else 'other' end) virtual)")
            con.execute("insert into messages values(7,'room',?,1)", (body,))
    monkeypatch.setattr(world_db, "ROOT", str(tmp_path))
    def forbidden():
        raise AssertionError("known source cannot select default")
    monkeypatch.setattr(receipts, "_account_db", forbidden)
    row = dict(id=1, state="sent", ack="confirmed", why="msg 7", chat_id="room", payload={"text":"expected"}, platform="wechat", account_id="a")
    assert not receipts.verify_row(row).verified
    assert receipts.verify_row(dict(row, account_id="b")).verified
    foreign = tmp_path / "data/platforms/wechat/b/account.db"
    assert not receipts.verify_row(row, db=foreign).verified


def test_missing_source_never_uses_default_or_explicit_database(tmp_path, monkeypatch):
    def forbidden():
        raise AssertionError("unknown source cannot select default")
    monkeypatch.setattr(receipts, "_account_db", forbidden)
    row = dict(id=1, state="sent", ack="confirmed", why="msg 7", chat_id="room", payload={"text":"expected"})
    for source in ({}, {"platform":"wechat"}, {"account_id":"a"}):
        for db in (None, tmp_path / "account.db"):
            result = receipts.verify_row(dict(row, **source), db=db)
            assert not result.verified
            assert result.verification_error == "observation_source_missing"


def test_bulk_readers_keep_source_and_unknown_history_unchanged(tmp_path, monkeypatch):
    path = tmp_path / "data/platforms/wechat/a/account.db"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as c:
        c.execute("create table messages(id integer,chat_id text,content text,is_self integer, sender_id text generated always as (case when is_self=1 then 'self' else 'other' end) virtual)")
        c.execute("insert into messages values(7,'room','expected',1)")
    monkeypatch.setattr(world_db, "ROOT", str(tmp_path))
    with sqlite3.connect(":memory:") as c:
        c.execute("create table outbound_queue(id integer,chat_id text,state text,ack text,why text,payload text,platform text,account_id text,created_ts real,updated_ts real)")
        for qid, account in [(1,"a"),(2,"")]:
            c.execute("insert into outbound_queue values(?, 'room','sent','confirmed','msg 7',?, 'wechat',?,100,100)", (qid,'{"text":"expected"}',account))
        c.commit()
        before = c.execute("select * from outbound_queue where id=2").fetchone()
        result = receipts.mark_unverified(conn=c, dry_run=False)
        assert result["kept"] == [1]
        assert result["marked"] == []
        assert result["unresolved"] == [{"queue_id":2,"why":"observation_source_missing"}]
        assert c.execute("select * from outbound_queue where id=2").fetchone() == before
        report = receipts.summary(conn=c, now=100)
        assert report["verified"] == 1
        assert report["unverified"] == 1
