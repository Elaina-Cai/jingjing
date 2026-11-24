"""verify_remember_roundtrip：事件→提示词的读回核对。"""
import json
import sqlite3
from pathlib import Path

from scripts import verify_remember_roundtrip as vr


def _meta(tmp, rows):
    db = Path(tmp) / "meta.db"
    con = sqlite3.connect(db)
    con.execute("create table terminal_events(id integer primary key, event_id text, event_type text, platform text, "
                "bot_account_id text, chat_id text, content text, occurred_at text)")
    con.executemany("insert into terminal_events(event_id,event_type,platform,bot_account_id,chat_id,content,occurred_at) "
                    "values(?,?,?,?,?,?,?)", [r[:1] + ("memory_write",) + r[1:] for r in rows])
    con.commit(); con.close()
    return db


def _prompt(tmp, name, text):
    d = Path(tmp) / "prompt" / name
    d.mkdir(parents=True)
    (d / "request.json").write_text(json.dumps({"turn_id": name, "rounds": [{"messages": [{"role": "user", "content": text}]}]},
                                               ensure_ascii=False), encoding="utf-8")


def test_roundtrip_marks_read_back(tmp_path):
    db = _meta(tmp_path, [("evt-1", "wechat", "acct", "c1", "主人不吃香菜", "2026-11-24 12:00:00")])
    _prompt(tmp_path, "t1", "[记忆索引]\n主人不吃香菜")
    rows = vr.roundtrip(db, Path(tmp_path) / "prompt")
    assert rows[0]["seen_in_prompt"] == "t1"


def test_roundtrip_marks_not_read_back(tmp_path):
    db = _meta(tmp_path, [("evt-1", "wechat", "acct", "c1", "主人不吃香菜", "2026-11-24 12:00:00")])
    _prompt(tmp_path, "t1", "别的对话")
    assert vr.roundtrip(db, Path(tmp_path) / "prompt")[0]["seen_in_prompt"] == ""


def test_no_writes_is_empty(tmp_path):
    db = _meta(tmp_path, [])
    assert vr.roundtrip(db, Path(tmp_path) / "prompt") == []
