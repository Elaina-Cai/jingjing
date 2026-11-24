#!/usr/bin/env python
"""查看某会话最近的回合：消息 + 她的回复 + system 块 + 召回分数（只读）。

批1 观察工具：验证「记忆闸门 / [拿不准时] / 负知识」在真实链路里的表现。

用法：
  python scripts/inspect_turns.py --chat wxid_seed01 --n 8
  python scripts/inspect_turns.py --chat 10000000002@chatroom --n 8
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

META_DB = ROOT / "data" / "yinyue_meta.db"
CONTEXT_TRACE = ROOT / "logs" / "decision_trace.jsonl"
RECALL_TRACE = ROOT / "logs" / "memory_recall_trace.jsonl"


def recent_events(chat_id: str, limit: int) -> list[dict]:
    con = sqlite3.connect(f"file:{META_DB.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "select occurred_at, event_type, actor_person_id, content, metadata_json "
            "from terminal_events where chat_id = ? "
            "and event_type in ('message','question','reply','request','command') "
            "order by sequence desc limit ?",
            (chat_id, limit * 3),
        ).fetchall()
        names: dict[str, str] = {}
        for row in con.execute(
            "select pi.identity_value, pe.display_name "
            "from person_identities pi join person_entities pe "
            "on pe.person_id = pi.person_id where pe.display_name is not null"
        ):
            names.setdefault(str(row[0]), str(row[1]))
    finally:
        con.close()

    out = []
    for row in reversed(rows):
        try:
            meta = json.loads(row["metadata_json"] or "{}")
        except Exception:
            meta = {}
        is_bot = str(row["event_type"]) == "reply" or bool(meta.get("is_self"))
        if is_bot:
            speaker = "鲸鲸"
        else:
            speaker = (
                str(meta.get("sender_name") or "")
                or names.get(str(row["actor_person_id"] or ""), "")
                or str(row["actor_person_id"] or "?")
            )
        out.append(
            {
                "at": str(row["occurred_at"])[:19],
                "speaker": speaker,
                "content": str(row["content"] or "")[:120],
            }
        )
    # 她的同一句话会双写（reply + is_self message），相邻同内容去重
    deduped: list[dict] = []
    for event in out:
        if (
            deduped
            and event["speaker"] == deduped[-1]["speaker"]
            and event["content"] == deduped[-1]["content"]
        ):
            continue
        deduped.append(event)
    return deduped


def trace_tail(path: Path, chat_id: str, limit: int) -> list[dict]:
    if not path.exists():
        return []
    picked: list[dict] = []
    for line in reversed(path.read_text(encoding="utf-8").splitlines()):
        try:
            record = json.loads(line)
        except Exception:
            continue
        if str(record.get("kind") or "") != "context_detail":
            continue
        if str(record.get("chat_id") or "") != chat_id:
            continue
        picked.append(record)
        if len(picked) >= limit:
            break
    return list(reversed(picked))


def main() -> int:
    parser = argparse.ArgumentParser(description="查看会话最近回合（只读）")
    parser.add_argument("--chat", required=True)
    parser.add_argument("--n", type=int, default=6)
    args = parser.parse_args()

    print(f"==== 最近消息（{args.chat}）====")
    for event in recent_events(args.chat, args.n)[-(args.n * 2):]:
        print(f"  [{event['at']}] {event['speaker']}: {event['content']}")

    print()
    print("==== system 块（最近回合）====")
    for record in trace_tail(CONTEXT_TRACE, args.chat, args.n):
        names = [b.get("name") for b in record.get("blocks", [])]
        flag = "UNCERTAIN在" if "[不确定与沉默]" in names else "无不确定块"
        print(f"  {record.get('at', '')[:19]} [{flag}] {names}")

    print()
    print("==== 召回分数（最近回合）====")
    for record in trace_tail(RECALL_TRACE, args.chat, args.n):
        hits = record.get("hits") or []
        scores = [h.get("score") for h in hits]
        print(f"  {record.get('at', '')[:19]} q={str(record.get('query'))[:40]!r} scores={scores}")

    print()
    print("提示：开闸门后 scores 应全部 ≥ 0.45；q 为问句且无相关记忆时，她应说「没查到/记不清」而不是编。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
