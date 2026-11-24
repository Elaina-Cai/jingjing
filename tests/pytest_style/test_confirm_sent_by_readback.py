"""confirm_sent_by_readback 的核心核对：只认自发、正文逐字、时间窗内唯一。"""
import sqlite3
from datetime import datetime

from scripts import confirm_sent_by_readback as cs


def _db(tmp_path, rows):
    path = tmp_path / "data" / "platforms" / "wechat" / "wxid_a" / "account.db"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as con:
        con.execute("create table messages(id integer,chat_id text,is_self integer,content text,sender_id text,msg_id integer,created_at text)")
        con.executemany("insert into messages values(?,?,?,?,?,?,?)", rows)
    return path


def test_self_row_in_window_is_confirmed(tmp_path):
    path = _db(tmp_path, [(1, "room", 1, "在吗", "wxid_a", 99, "2026-11-24 11:15:15")])
    status, mid = cs.check({"chat_id": "room", "text": "在吗", "sent_at_utc": "2026-11-24T11:15:08"}, path)
    assert (status, mid) == ("UNIQUE", "99")


def test_other_person_same_text_is_not_confirmed(tmp_path):
    path = _db(tmp_path, [(1, "room", 0, "在吗", "wxid_b", 99, "2026-11-24 11:15:15")])
    assert cs.check({"chat_id": "room", "text": "在吗", "sent_at_utc": "2026-11-24T11:15:08"}, path) == ("NONE", "")


def test_outside_window_is_not_confirmed(tmp_path):
    path = _db(tmp_path, [(1, "room", 1, "在吗", "wxid_a", 99, "2026-11-24 12:30:00")])
    assert cs.check({"chat_id": "room", "text": "在吗", "sent_at_utc": "2026-11-24T11:15:08"}, path) == ("NONE", "")


def test_missing_text_is_reported(tmp_path):
    path = _db(tmp_path, [])
    assert cs.check({"chat_id": "room", "text": "", "sent_at_utc": "2026-11-24T11:15:08"}, path) == ("NO_TEXT", "")
