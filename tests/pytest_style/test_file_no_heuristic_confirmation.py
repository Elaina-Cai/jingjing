import sqlite3

from core.execution import wx_sender


def test_equal_names_in_multiple_accounts_cannot_confirm_delivery(tmp_path, monkeypatch):
    from db import world_db
    import threading
    for account in ("a", "b"):
        path = tmp_path / account / "account.db"
        path.parent.mkdir()
        with sqlite3.connect(path) as con:
            con.execute("create table messages(id integer,chat_id text,content text,msg_type_name text,created_at text)")
            con.execute("insert into messages values(7,'other','same.pdf','file',datetime('now','localtime'))")
    calls = []
    monkeypatch.setattr(world_db, "confirm_outbound", lambda *a, **kw: calls.append("confirm"))
    monkeypatch.setattr(threading.Thread, "start", lambda *a: calls.append("thread"))
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **kw: calls.append("scan"))
    result = wx_sender._confirm_file_later("room", "same.pdf", delays=(0,))
    assert result == {"confirmed": False, "why": "missing_file_delivery_evidence"}
    assert calls == []


def test_real_send_attempt_keeps_unconfirmed_receipt_without_name_probe(monkeypatch):
    from db import world_db
    calls = []
    monkeypatch.setattr(wx_sender, "_orig_send_file", lambda *a: {"ok": True, "ack": "unconfirmed"})
    monkeypatch.setattr(wx_sender, "_confirm_file_later", lambda *a: calls.append("confirm"))
    def record(*args, **kw):
        calls.append(kw)
    monkeypatch.setattr(world_db, "record_outbound", record)
    result = wx_sender.WxSender().send_file("room", "same.pdf")
    assert result == {"ok": True, "ack": "unconfirmed"}
    assert len(calls) == 1
    assert calls[0]["ack"] == "unconfirmed"
