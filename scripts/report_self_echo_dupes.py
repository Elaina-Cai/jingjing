# -*- coding: utf-8 -*-
"""自我回声重复报告：同一句话的 message（同步回声）与 reply（我们自己写的）。

写侧已在 record_message_events 里合并（is_self 回声命中近期 reply -> 不追新
事件、只回填 external_msg_id）；本脚本只读，统计历史存量。

用法：
  .venv_it/Scripts/python.exe scripts/report_self_echo_dupes.py
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WINDOW_SECONDS = 900


def _norm(text: object) -> str:
    return " ".join(str(text or "").split())


def _ts(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except Exception:
        return None


def scan(db: Path) -> list[tuple[dict, dict]]:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.execute("pragma query_only = on")
    conn.execute("pragma temp_store = memory")
    rows = conn.execute(
        "select event_id, occurred_at, chat_id, content, external_msg_id, event_type, metadata_json "
        "from terminal_events where event_type in ('reply', 'message') order by occurred_at"
    ).fetchall()
    replies: list[dict] = []
    dupes: list[tuple[dict, dict]] = []
    for raw in rows:
        row = dict(raw)
        if row["event_type"] == "reply":
            replies.append(row)
            continue
        try:
            meta = json.loads(row.get("metadata_json") or "{}")
        except Exception:
            meta = {}
        if not int(meta.get("is_self") or 0):
            continue
        body = _norm(row.get("content"))
        if len(body) < 8:
            continue
        stamp = _ts(row.get("occurred_at"))
        for reply in reversed(replies):
            if str(reply.get("chat_id")) != str(row.get("chat_id")):
                continue
            if _norm(reply.get("content")) != body:
                continue
            reply_stamp = _ts(reply.get("occurred_at"))
            if stamp is None or reply_stamp is None:
                continue
            if abs((stamp - reply_stamp).total_seconds()) <= WINDOW_SECONDS:
                dupes.append((row, reply))
                break
    return dupes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data/yinyue_meta.db"))
    args = ap.parse_args()
    dupes = scan(Path(args.db))
    print(f"self-echo duplicates: {len(dupes)}")
    for row, reply in dupes[:5]:
        print(
            "  message",
            row.get("external_msg_id"),
            "@", str(row.get("occurred_at"))[:19],
            "-> reply", str(reply.get("event_id"))[:12],
            "@", str(reply.get("occurred_at"))[:19],
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
