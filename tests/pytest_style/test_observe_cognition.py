"""观察报告脚本的纯函数：只读、可离线用临时文件测。"""
import json
import sqlite3
import time
from pathlib import Path

from scripts import observe_cognition as oc


def _ledger(tmp: Path, rows) -> Path:
    d = tmp / "loop"
    d.mkdir(parents=True)
    with open(d / "ledger-2026-11-24.jsonl", "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return d


def test_ledger_summary_counts_says_and_blocks(tmp_path):
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    d = _ledger(tmp_path, [
        {"at": now, "act": "say", "result": "sent"},
        {"at": now, "act": "say", "result": "failed", "blocked_reason": "not_in_allowlist"},
        {"at": now, "act": "rest", "result": "blocked", "blocked_reason": "required_reply_pending"},
        {"at": now, "act": "evaluate_experience", "result": "committed"},
    ])
    summary = oc.summarize_ledger(oc.ledger_rows(d, hours=24))
    assert summary["rows"] == 4
    assert summary["say_ok"] == 1 and summary["say_failed"] == 1
    assert summary["appraisals"] == 1
    assert summary["blocked"]["not_in_allowlist"] == 1


def test_appraisal_stats_counts_each_field(tmp_path):
    db = tmp_path / "world.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE cognitive_events(seq INTEGER PRIMARY KEY, operation TEXT, payload TEXT, "
                 "scope TEXT, committed_at TEXT)")
    scope = json.dumps({"chat_id": "c1"})
    now = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    rows = [
        {"meaning": "懂了", "feeling_text": "有点慌", "tendency": "", "unresolved_condition": "", "next_move": "reply"},
        {"meaning": "还在想", "feeling_text": "", "tendency": "先问清", "unresolved_condition": "他没回", "next_move": "clarify"},
    ]
    for payload in rows:
        conn.execute("INSERT INTO cognitive_events(operation,payload,scope,committed_at) VALUES(?,?,?,?)",
                     ("appraise", json.dumps(payload, ensure_ascii=False), scope, now))
    conn.commit()
    conn.close()
    stats = oc.appraisal_stats(db, hours=24)
    assert stats["total"] == 2
    assert stats["fields"] == {"meaning": 2, "feeling_text": 1, "tendency": 1,
                               "unresolved_condition": 1, "next_move": 2}
    assert stats["chats"] == {"c1": 2}


def test_mood_stats_reads_scoped_and_global(tmp_path):
    state = tmp_path / "state.json"
    key = json.dumps(["wechat", "acct", "g1"])
    state.write_text(json.dumps({
        "mood": {"text": "旧全局", "at": time.time() - 3600},
        "moods": {key: {"text": "这个会话的", "at": time.time() - 60}},
    }, ensure_ascii=False), encoding="utf-8")
    stats = oc.mood_stats(state)
    assert stats["global"] == "旧全局"
    assert stats["scoped"] == [{"chat": "g1", "text": "这个会话的", "age_h": stats["scoped"][0]["age_h"]}]
    assert stats["scoped"][0]["age_h"] is not None


def test_prompt_stats_counts_marks(tmp_path):
    d = tmp_path / "prompt"
    d.mkdir()
    (d / "t1.json").write_text('{"messages":[{"content":"以前的事\\n心境：有点慌"}]}', encoding="utf-8")
    (d / "t2.json").write_text('{"messages":[{"content":"普通回合"}]}', encoding="utf-8")
    stats = oc.prompt_stats(d, limit=10)
    assert stats["files"] == 2
    assert stats["marks"]["以前的事"] == 1
    assert stats["marks"]["心境："] == 1


def test_report_renders_without_crashing(tmp_path):
    text = oc.render_report(
        hours=24,
        ledger={"rows": 3, "acts": {}, "say_ok": 2, "say_failed": 0, "blocked": {}, "appraisals": 1},
        appraisals={"total": 1, "fields": {"meaning": 1}, "chats": {"c1": 1}},
        moods={"global": "", "global_age_h": None, "scoped": []},
        prompts={"files": 1, "marks": {"以前的事": 1}},
    )
    assert "观察报告" in text and "最近 24 小时" in text

def _trace(tmp_root: Path, rows) -> Path:
    d = tmp_root / "loop"
    d.mkdir(parents=True)
    with open(d / "trace-2026-11-24.jsonl", "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return d


def test_turn_stats_counts_must_answer_and_treats_sticker_as_a_reply(tmp_path):
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    d = _trace(tmp_path, [
        {"at": now, "wake": str({"addressed": {"kind": "named", "must_answer": True}}),
         "outcome": str({"said": []}), "steps": str([{"tool": "sticker"}])},
        {"at": now, "wake": str({"addressed": {"kind": "named", "must_answer": True}}),
         "outcome": str({"said": []}), "steps": str([{"tool": "rest"}])},
        {"at": now, "wake": str({"addressed": {"kind": "", "read_only": True}}),
         "outcome": str({"said": []}), "steps": str([{"tool": "say"}])},
        {"at": now, "scene": "group", "wake": str({"addressed": {"kind": ""}}),
         "outcome": str({"said": [{"text": "我也在"}]}), "steps": str([{"tool": "say"}])},
    ])
    stats = oc.turn_stats(d, hours=24)
    assert stats["turns"] == 4
    assert stats["must_answer"] == 2
    assert stats["must_answer_silent"] == 1
    assert stats["read_only"] == 1 and stats["read_only_say"] == 1
    assert stats["spoke"] == 2 and stats["rested"] == 1
    assert stats["group_turns"] == 1 and stats["group_smalltalk_speech"] == 1

def test_delivery_stats_counts_ack_and_failures(tmp_path):
    db = tmp_path / "world.db"
    con = sqlite3.connect(db)
    con.execute("create table outbound_queue(id integer, created_ts real)")
    con.execute("create table outbound_receipt(id integer, turn_id text, ack text)")
    con.execute("create table outbound_attempt(id integer, started_ts real, result text)")
    now = time.time()
    con.execute("insert into outbound_queue values(1, ?)", (now,))
    con.execute("insert into outbound_queue values(2, ?)", (now,))
    con.execute("insert into outbound_receipt values(11,'queue:1','confirmed')")
    con.execute("insert into outbound_receipt values(12,'queue:2','unconfirmed')")
    con.execute("insert into outbound_attempt values(1, ?, 'failed')", (now,))
    con.commit(); con.close()
    assert oc.delivery_stats(db, hours=1) == {"confirmed": 1, "unconfirmed": 1,
                                              "other": 0, "attempt_failed": 1}
