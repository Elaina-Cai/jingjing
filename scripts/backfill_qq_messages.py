# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
import sys
import urllib.request
from datetime import datetime

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

QQ_DB = ROOT / "data/platforms/qq/3580199493/account.db"
ACCOUNT_ID = "3580199493"


def _api(action, payload, port=3000, timeout=10):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/{action}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _text_of(msg):
    parts = []
    for seg in (msg.get("message") or []):
        kind = seg.get("type")
        data = seg.get("data") or {}
        if kind == "text":
            parts.append(str(data.get("text") or ""))
        elif kind == "image":
            parts.append("[image]")
        elif kind == "face":
            parts.append("[face]")
        elif kind:
            parts.append(f"[{kind}]")
    return " ".join(" ".join(parts).split())


def collect(group_id, since_local, *, port=3000, count=200):
    hist = _api("get_group_msg_history", {"group_id": int(group_id), "count": count}, port)
    msgs = (hist.get("data") or {}).get("messages") or []
    conn = sqlite3.connect(str(QQ_DB))
    existing = {str(r[0]) for r in conn.execute(
        "select msg_id from messages where chat_id like ?", (f"%{group_id}%",))}
    chat_id = f"group:{group_id}"
    rows = []
    for m in msgs:
        ts = datetime.fromtimestamp(int(m.get("time") or 0))
        stamp = ts.strftime("%Y-%m-%d %H:%M:%S")
        mid = str(m.get("message_id") or "")
        if not mid or mid in existing or stamp < since_local:
            continue
        sender = m.get("sender") or {}
        rows.append({
            "msg_id": mid,
            "sender_id": str(sender.get("user_id") or ""),
            "sender_name": str(sender.get("card") or sender.get("nickname") or ""),
            "chat_id": chat_id,
            "chat_type": "group",
            "content": _text_of(m),
            "msg_type": "text",
            "created_at": stamp,
            "is_self": 0,
            "source": "napcat",
            "msg_source": "napcat_backfill",
            "account_id": ACCOUNT_ID,
            "synced_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "msg_type_name": "",
        })
    return rows


def apply(rows):
    conn = sqlite3.connect(str(QQ_DB))
    cols = [r[1] for r in conn.execute("pragma table_info(messages)")]
    use = [c for c in ("msg_id", "sender_id", "sender_name", "chat_id", "chat_type", "content",
                       "msg_type", "created_at", "is_self", "source", "msg_source", "account_id",
                       "synced_at") if c in cols]
    sql = f"insert into messages ({', '.join(use)}) values ({', '.join('?' for _ in use)})"
    for row in rows:
        conn.execute(sql, [row.get(c) for c in use])
    conn.commit()
    from core.terminal.event_runtime import get_event_identity_service, get_terminal_event_store
    from core.terminal.shadow import record_message_events
    store = get_terminal_event_store(force=True)
    ids = get_event_identity_service()
    n = record_message_events(list(rows), platform="qq", account_id=ACCOUNT_ID, identity_service=ids, store=store)
    return len(rows), n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", default="1124196924")
    ap.add_argument("--since", default="1970-01-01 00:00:00")
    ap.add_argument("--port", type=int, default=3000)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    rows = collect(args.group, args.since, port=args.port)
    print(f"missing messages: {len(rows)}")
    for row in rows[-40:]:
        print(f"  {row['created_at'][11:]} <{row['sender_name']}> {row['content'][:50]}")
    if args.apply and rows:
        stored, events = apply(rows)
        print(f"applied: account.db={stored} terminal_events={events}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
