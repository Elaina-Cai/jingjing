"""无账号/操作绑定的旧回读不能从同文记录生成发送凭据。"""
import sqlite3

from core.execution import wx_sender
from core.execution.gui import wechat


def test_legacy_lookup_never_reads_world_or_default_snapshot(tmp_path, monkeypatch):
    path = tmp_path / "world.db"
    with sqlite3.connect(path) as con:
        con.execute("create table inbound_event(msg_id text,chat_id text,text text,is_self integer)")
        con.execute("insert into inbound_event values('999','room','same',1)")
    monkeypatch.setattr(wx_sender, "_WORLD_DB", str(path), raising=False)
    def forbidden(*args, **kwargs):
        raise AssertionError("unbound lookup must not read any store")
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(wechat, "_decrypted_db", forbidden)
    monkeypatch.setattr(wechat, "rows_after", forbidden)
    assert wx_sender._lookup_ack("room", "same", 1) is None
    assert wx_sender.ack_after_send("room", "same", 1) == {"ack":"unconfirmed"}


def test_legacy_baseline_cannot_start_observation_thread(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("no default snapshot or background thread")
    monkeypatch.setattr(wechat, "_decrypted_db", forbidden)
    monkeypatch.setattr(wx_sender, "Thread", forbidden)
    baseline = wx_sender.ack_baseline("room")
    assert baseline is None
    assert wx_sender.ack_after_send("room", "same", baseline) == {"ack":"unconfirmed"}
    assert wx_sender.spawn_ack_backfill("room", "same", baseline) is False
