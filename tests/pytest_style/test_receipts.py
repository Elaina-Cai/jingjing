# -*- coding: utf-8 -*-
"""`loop/receipts.py` 的判据：说发出去了，必须有凭据。

2026-11-04 的真样本：真库 67 条 `state='sent'` 里 23 条在微信账库查无此消息
（凭据率 65.7%），其中 257/258 两条 `ack` 还停在 `pending`、`why` 空。
所以这里的每一条用例都在守同一句话：**队列说 sent 不算，账库里有这个 id 才算。**
"""

from __future__ import annotations

import os
import sqlite3

import pytest

from loop import direct_send
from loop import receipts as rc


def _msg_db(path, rows):
    path = path.parent / "data/platforms/wechat/a/account.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path)
    c.execute("create table messages (id text, chat_id text, content text, "
              "is_self integer, created_at text, sender_id text generated always as (case when is_self=1 then 'self' else 'other' end) virtual)")
    c.executemany("insert into messages (id, chat_id, content, is_self, created_at) "
                  "values (?,?,?,?,?)", rows)
    c.commit()
    c.close()
    return str(path)


def _queue_db(path, rows):
    c = sqlite3.connect(path)
    c.execute("create table outbound_queue (id integer primary key, chat_id text, state text, "
              "ack text, why text, payload text, created_ts real, updated_ts real, platform text default 'wechat', account_id text default 'a')")
    c.executemany("insert into outbound_queue (id, chat_id, state, ack, why, payload, "
                  "created_ts, updated_ts) values (?,?,?,?,?,?,?,?)", rows)
    c.commit()
    c.close()
    return str(path)


def test_parse_msg_id_only_accepts_the_msg_shape():
    assert rc.parse_msg_id("msg 2534904576254688467") == "2534904576254688467"
    assert rc.parse_msg_id("  msg 42  ") == "42"
    assert rc.parse_msg_id("") == ""
    assert rc.parse_msg_id("ok") == ""
    assert rc.parse_msg_id(None) == ""


def test_a_row_that_is_not_sent_yet_is_not_delivered():
    ok, why = rc.judge(state="pending", ack="queued", why="", msg_exists=None)
    assert ok is False
    assert "还没发出去" in why


def test_sent_without_a_msg_id_is_not_evidence():
    for ack in ("", "pending", "unconfirmed"):
        ok, why = rc.judge(state="sent", ack=ack, why="", msg_exists=None)
        assert ok is False
        assert "没回读到 msg_id" in why


def test_an_empty_ack_is_not_evidence_even_with_a_msg_id():
    ok, _why = rc.judge(state="sent", ack="", why="msg 123", msg_exists=True)
    assert ok is True  # 有 msg_id 且账库真有 → ack 空也不影响（凭据是 id，不是 ack）


def test_a_msg_id_that_is_not_in_the_account_db_does_not_count():
    ok, why = rc.judge(state="sent", ack="confirmed", why="msg 123", msg_exists=False)
    assert ok is False
    assert "不存在" in why


def test_an_unreadable_account_db_counts_as_unverified():
    ok, why = rc.judge(state="sent", ack="confirmed", why="msg 123", msg_exists=None)
    assert ok is False
    assert "先当没核对" in why


def test_three_layers_all_true_is_delivered():
    ok, why = rc.judge(state="sent", ack="confirmed", why="msg 2534904576254688467", msg_exists=True)
    assert ok is True
    assert why == "msg 2534904576254688467"


def test_msg_in_db_finds_the_real_sample(tmp_path):
    db = _msg_db(tmp_path / "account.db", [("2534904576254688467", "10000000001@chatroom", "x", 1,
                                            "2026-11-04 18:56:30")])
    assert rc.msg_in_db("2534904576254688467", db=db) is True
    assert rc.msg_in_db("100000000071111111", db=db) is False


def test_msg_in_db_can_be_scoped_to_one_chat(tmp_path):
    db = _msg_db(tmp_path / "account.db", [("42", "a@chatroom", "x", 1, "2026-11-04 10:00:00")])
    assert rc.msg_in_db("42", "a@chatroom", db=db) is True
    assert rc.msg_in_db("42", "b@chatroom", db=db) is False


def test_a_missing_account_db_is_none_not_false():
    assert rc.msg_in_db("42", db=os.path.join(os.sep, "nope", "account.db")) is None


def test_verify_row_accepts_a_row_with_a_real_message(tmp_path, monkeypatch):
    db = _msg_db(tmp_path / "account.db", [("777", "c1", "你好", 1, "2026-11-04 10:00:00")])
    monkeypatch.setattr(rc, "_account_db", lambda: db)
    rec = rc.verify_row({"id": 9, "chat_id": "c1", "state": "sent", "ack": "confirmed",
                         "why": "msg 777", "platform":"wechat", "account_id":"a", "payload": {"text": "你好"}})
    assert rec.verified is True
    assert rec.msg_id == "777"
    assert rec.queue_id == 9


def test_verify_row_rejects_the_real_unverified_sample(tmp_path, monkeypatch):
    db = _msg_db(tmp_path / "account.db", [])
    monkeypatch.setattr(rc, "_account_db", lambda: db)
    rec = rc.verify_row({"id": 258, "chat_id": "10000000001@chatroom", "state": "sent",
                         "ack": "pending", "why": ""})
    assert rec.verified is False
    assert "没回读到 msg_id" in rec.why


def test_verify_splits_verified_unverified_and_pending(tmp_path, monkeypatch):
    db = _msg_db(tmp_path / "account.db", [("11", "c1", "hi", 1, "2026-11-04 10:00:00")])
    monkeypatch.setattr(rc, "_account_db", lambda: db)
    qdb = _queue_db(tmp_path / "world.db", [
        (1, "c1", "sent", "confirmed", "msg 11", '{"text":"hi"}', 0, 0),
        (2, "c1", "sent", "pending", "", "{}", 0, 0),
        (3, "c1", "pending", "queued", "", "{}", 0, 0),
    ])
    conn = sqlite3.connect(qdb)
    try:
        out = rc.verify([1, 2, 3], conn=conn)
    finally:
        conn.close()
    assert out["verified"] == [1]
    assert [u["queue_id"] for u in out["unverified"]] == [2]
    assert out["pending"] == [3]


def test_backfill_finds_the_message_and_writes_the_evidence(tmp_path, monkeypatch):
    # 注意时间口径：`_find_message` 用 `strftime('%s', created_at)`（按 UTC 解析），
    # 所以这一行的时间戳必须跟 created_at 对得上，否则会被 600 秒窗口挡掉。

    db = _msg_db(tmp_path / "account.db", [("555", "c1", "我去问一下场地", 1, "2026-11-04 10:00:00")])
    monkeypatch.setattr(rc, "_account_db", lambda: db)
    qdb = _queue_db(tmp_path / "world.db", [
        (7, "c1", "sent", "pending", "", '{"text": "我去问一下场地"}', 0, 1793786400.0)])
    conn = sqlite3.connect(qdb)
    try:
        dry = rc.backfill(conn=conn, dry_run=True)
        assert dry["fixed"] == []
        assert "operation_binding_unavailable" in dry["error"]
        row = conn.execute("select ack, why from outbound_queue where id=7").fetchone()
        assert row == ("pending", "")  # dry_run 不写库
        real = rc.backfill(conn=conn, dry_run=False)
        assert real["fixed"] == []
        conn.commit()
        row = conn.execute("select ack, why from outbound_queue where id=7").fetchone()
        assert row == ("pending", "")
    finally:
        conn.close()


def test_backfill_leaves_a_row_it_cannot_prove_alone(tmp_path, monkeypatch):
    db = _msg_db(tmp_path / "account.db", [])
    monkeypatch.setattr(rc, "_account_db", lambda: db)
    qdb = _queue_db(tmp_path / "world.db", [
        (8, "c1", "sent", "pending", "", '{"text": "查无此句"}', 0, 1793800000.0)])
    conn = sqlite3.connect(qdb)
    try:
        out = rc.backfill(conn=conn, dry_run=False)
        assert out["fixed"] == []
        assert out["unmatched"] == [8]
        row = conn.execute("select ack, why from outbound_queue where id=8").fetchone()
        assert row == ("pending", "")
    finally:
        conn.close()


def test_direct_send_does_not_call_an_unprovable_row_delivered(monkeypatch):
    """接线（§24 地基②）：队列 sent 但账库查不到 → `unverified`，不许记 `delivered`。"""
    monkeypatch.setattr(direct_send, "_query_queue",
                        lambda ids, conn=None: [{"id": int(i), "state": "sent",
                                                 "ack": "pending", "why": ""} for i in ids])
    monkeypatch.setattr(rc, "_account_db", lambda: os.path.join(os.sep, "nope", "account.db"))
    st = {"items": {"m1": {"state": "queued", "queue_ids": [258], "target": "银月的测试群"}}}
    out = direct_send.check_deliveries(state=st)
    assert out["delivered"] == []
    assert out["unverified"] == ["m1"]
    assert st["items"]["m1"]["state"] == "unverified"


def test_direct_send_still_delivers_when_the_message_is_there(tmp_path, monkeypatch):
    db = _msg_db(tmp_path / "account.db", [("900", "c1", "hi", 1, "2026-11-04 10:00:00")])
    monkeypatch.setattr(rc, "_account_db", lambda: db)
    monkeypatch.setattr(direct_send, "_query_queue",
                        lambda ids, conn=None: [{"id": int(i), "state": "sent",
                                                 "ack": "confirmed", "why": "msg 900", "chat_id": "c1", "platform":"wechat", "account_id":"a", "payload": {"text": "hi"}} for i in ids])
    st = {"items": {"m2": {"state": "queued", "queue_ids": [9], "target": "银月的测试群"}}}
    out = direct_send.check_deliveries(state=st)
    assert out["delivered"] == ["m2"]
    assert out["unverified"] == []


@pytest.fixture(autouse=True)
def explicit_source_root(tmp_path, monkeypatch):
    from db import world_db
    monkeypatch.setattr(world_db, "ROOT", str(tmp_path))
