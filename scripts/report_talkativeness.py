"""主动度读数：她在一段时间里说了多少、多密、有没有人接。

只读（``mode=ro``），不改数据、不调 LLM。三个口径：
1. 量：每日 inbound / 她的 reply / 占比。
2. 密：自连发间隔（<=60s 占比、中位间隔）、最长连发。
3. 接：她的回复后 5 分钟内，是否有别人再说话 / 有人 @ 她 / 有人回她那条。

用法::

    python scripts/report_talkativeness.py --chat 10000000003@chatroom --days 7
    python scripts/report_talkativeness.py --chat 10000000003@chatroom --json out.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "yinyue_meta.db"


def _connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def _rows(conn: sqlite3.Connection, sql: str, args: tuple) -> list[tuple]:
    return list(conn.execute(sql, args))


def _parse(stamp: str) -> dt.datetime:
    return dt.datetime.strptime(str(stamp)[:19], "%Y-%m-%d %H:%M:%S")


def _daily(conn: sqlite3.Connection, chat: str, days: int) -> list[dict]:
    rows = _rows(
        conn,
        """
        select date(occurred_at) d,
               sum(case when event_type='reply' then 1 else 0 end) hers,
               sum(case when event_type in ('message','question') then 1 else 0 end) inbound
        from terminal_events
        where chat_id=? and occurred_at >= datetime('now', ?)
        group by d order by d
        """,
        (chat, f"-{days} day"),
    )
    out = []
    for day, hers, inbound in rows:
        out.append(
            {
                "date": day,
                "replies": int(hers or 0),
                "inbound": int(inbound or 0),
                "reply_rate": round((hers or 0) / inbound, 4) if inbound else None,
            }
        )
    return out


def _bursts(stamps: list[str]) -> dict:
    if len(stamps) < 2:
        return {"replies": len(stamps), "within_60s": 0, "share_60s": None, "median_gap_s": None, "longest_run_60s": len(stamps)}
    gaps = [(_parse(b) - _parse(a)).total_seconds() for a, b in zip(stamps, stamps[1:])]
    within = [g for g in gaps if g <= 60]
    run = best = 1
    for g in gaps:
        run = run + 1 if g <= 60 else 1
        best = max(best, run)
    return {
        "replies": len(stamps),
        "within_60s": len(within),
        "share_60s": round(len(within) / len(gaps), 4),
        "median_gap_s": round(sorted(gaps)[len(gaps) // 2], 1),
        "longest_run_60s": best,
    }


def _engagement(conn: sqlite3.Connection, chat: str, days: int, window_s: int = 300) -> dict:
    replies = _rows(
        conn,
        "select occurred_at, external_msg_id, content from terminal_events "
        "where chat_id=? and event_type='reply' and occurred_at >= datetime('now', ?) order by occurred_at",
        (chat, f"-{days} day"),
    )
    follow = 0
    at_her = 0
    reply_to_her = 0
    for stamp, ext_id, _content in replies:
        start = _parse(stamp)
        end = start + dt.timedelta(seconds=window_s)
        nxt = _rows(
            conn,
            "select content, parent_event_id, external_msg_id from terminal_events "
            "where chat_id=? and event_type in ('message','question') and occurred_at > ? and occurred_at <= ?",
            (chat, stamp, end.strftime("%Y-%m-%d %H:%M:%S")),
        )
        if nxt:
            follow += 1
        for content, parent, _ext in nxt:
            text = content or ""
            if "@鲸鲸" in text or "@P6" in text:
                at_her += 1
                break
        if ext_id:
            hit = _rows(
                conn,
                "select 1 from terminal_events where chat_id=? and parent_event_id=? limit 1",
                (chat, ext_id),
            )
            if hit:
                reply_to_her += 1
    total = len(replies)
    return {
        "replies": total,
        "followed_within_window": follow,
        "follow_rate": round(follow / total, 4) if total else None,
        "got_at": at_her,
        "at_rate": round(at_her / total, 4) if total else None,
        "got_reply": reply_to_her,
        "reply_rate": round(reply_to_her / total, 4) if total else None,
        "window_s": window_s,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--chat", required=True)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    conn = _connect(Path(args.db))
    stamps = [
        r[0]
        for r in _rows(
            conn,
            "select occurred_at from terminal_events where chat_id=? and event_type='reply' "
            "and occurred_at >= datetime('now', ?) order by occurred_at",
            (args.chat, f"-{args.days} day"),
        )
    ]
    report = {
        "chat": args.chat,
        "days": args.days,
        "daily": _daily(conn, args.chat, args.days),
        "burst": _bursts(stamps),
        "engagement": _engagement(conn, args.chat, args.days),
    }
    print(f"chat={report['chat']} days={report['days']}")
    print(f"{'date':<12}{'replies':>8}{'inbound':>9}{'rate':>8}")
    for d in report["daily"]:
        print(f"{d['date']:<12}{d['replies']:>8}{d['inbound']:>9}{(d['reply_rate'] if d['reply_rate'] is not None else 0):>8.3f}")
    b = report["burst"]
    print(f"\nburst: replies={b['replies']} <=60s={b['within_60s']} share={b['share_60s']} median_gap={b['median_gap_s']}s longest_run={b['longest_run_60s']}")
    e = report["engagement"]
    print(f"engagement: followed={e['followed_within_window']}/{e['replies']} ({e['follow_rate']}) got_at={e['got_at']} ({e['at_rate']}) got_reply={e['got_reply']} ({e['reply_rate']}) window={e['window_s']}s")
    if args.json:
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"json={args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())