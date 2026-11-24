# -*- coding: utf-8 -*-
r"""只读复算：事件落库失败/重连的分布 + ``terminal_events`` 增长基线。

用法（只读，不改库、不改 .env）：

    .\.venv_it\Scripts\python.exe -X utf8 -u scripts\report_terminal_drops.py
    .\.venv_it\Scripts\python.exe -X utf8 -u scripts\report_terminal_drops.py --since 2026-10-18T00:00
    .\.venv_it\Scripts\python.exe -X utf8 -u scripts\report_terminal_drops.py --json

打印四段：

1. ``logs/yinyue.log*`` 里四条事件的逐文件计数与首末时间：
   ``terminal_event_record_failed``（事件**丢了**）、
   ``terminal_event_store_reconnect``（CANTOPEN 重试）、
   ``outbox_drain_failed``（出站队列写不进去）、``temp_dir_pinned``（本修复是否生效）；
2. 按小时直方图（定位突发窗口，便于和外部扫描/备份对时间）；
3. ``terminal_events`` 增长基线：``max(id)`` / 行数 / 最近一条 ``occurred_at`` /
   最近 15 分钟新增行数（重启后 15 分钟对照用）；
4. 同一窗口内 ``utterance_edges_recorded`` 的条数（判断是「整条写路径挂了」还是
   「只有 events 挂了」）。
"""
from __future__ import annotations

import argparse
import glob
import io
import json
import pathlib
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta

ROOT = pathlib.Path(__file__).resolve().parent.parent
META_DB = ROOT / "data" / "yinyue_meta.db"

EVENTS = (
    "terminal_event_record_failed",
    "terminal_event_store_reconnect",
    "outbox_drain_failed",
    "temp_dir_pinned",
)


def _read_logs() -> list[dict]:
    records: list[dict] = []
    for path in sorted(glob.glob(str(ROOT / "logs" / "yinyue.log*"))):
        with io.open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    records.append(json.loads(line))
                except ValueError:
                    continue
    return records


def _parse(ts: object) -> datetime | None:
    text = str(ts or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def section_logs(records: list[dict], since: datetime | None) -> dict:
    print("\n== 1) 日志事件计数（logs/yinyue.log*）")
    print("   %-32s %8s  %-27s %-27s" % ("event", "count", "first", "last"))
    summary: dict[str, dict] = {}
    for event in EVENTS:
        rows = [r for r in records if r.get("event") == event]
        if since is not None:
            rows = [r for r in rows if (_parse(r.get("timestamp")) or datetime.min.replace(tzinfo=since.tzinfo)) >= since]
        stamps = sorted(str(r.get("timestamp") or "") for r in rows)
        summary[event] = {"count": len(rows), "first": stamps[0] if stamps else "", "last": stamps[-1] if stamps else ""}
        print("   %-32s %8d  %-27s %-27s" % (event, len(rows), (stamps[0] if stamps else "-")[:27], (stamps[-1] if stamps else "-")[:27]))
    dropped = summary["terminal_event_record_failed"]["count"]
    print("   ⇒ 事件丢失候选条数 = %d（每条 = 一条事件没能落库）" % dropped)
    return summary


def section_hours(records: list[dict]) -> None:
    print("\n== 2) terminal_event_record_failed 按小时")
    buckets = Counter(str(r.get("timestamp") or "")[:13] for r in records if r.get("event") == "terminal_event_record_failed")
    if not buckets:
        print("   （无）")
        return
    for hour, count in sorted(buckets.items()):
        print("   %s  %4d  %s" % (hour, count, "#" * min(60, count)))


def section_db() -> None:
    print("\n== 3) terminal_events 增长基线（只读 data/yinyue_meta.db）")
    if not META_DB.exists():
        print("   （没有 %s）" % META_DB)
        return
    con = sqlite3.connect("file:%s?mode=ro" % META_DB.as_posix(), uri=True)
    try:
        max_id, rows, newest = con.execute(
            "select coalesce(max(id), 0), count(*), coalesce(max(occurred_at), '') from terminal_events"
        ).fetchone()
        print("   max(id)=%s  rows=%s  newest occurred_at=%s" % (max_id, rows, newest))
        for minutes in (15, 60):
            since = (datetime.now() - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
            fresh = con.execute("select count(*) from terminal_events where occurred_at >= ?", (since,)).fetchone()[0]
            print("   最近 %2d 分钟新增 = %s（occurred_at 是本地 naive 时间）" % (minutes, fresh))
        edges = con.execute("select count(*) from utterance_edges").fetchone()[0] if _has_table(con, "utterance_edges") else "（无表）"
        print("   utterance_edges 行数 = %s" % edges)
    finally:
        con.close()


def _has_table(con: sqlite3.Connection, name: str) -> bool:
    row = con.execute("select 1 from sqlite_master where type='table' and name = ?", (name,)).fetchone()
    return row is not None


def section_cross(records: list[dict], summary: dict) -> None:
    print("\n== 4) 同一窗口的 utterance_edges_recorded")
    edges = [r for r in records if r.get("event") == "utterance_edges_recorded"]
    fails = [r for r in records if r.get("event") == "terminal_event_record_failed"]
    print("   utterance_edges_recorded = %d 条（%d 个文件里）" % (len(edges), len({r.get("timestamp") for r in edges})))
    fail_minutes = {str(r.get("timestamp") or "")[:15] for r in fails}
    edge_minutes = {str(r.get("timestamp") or "")[:15] for r in edges}
    print("   与失败同分钟出现的 edges 记录 = %d 分钟" % len(fail_minutes & edge_minutes))
    if len(fail_minutes & edge_minutes) == 0:
        print("   ⇒ 失败窗口里 edges 一条都没写成功：不是「只有 events 挂」，是整条写路径不可用")
    print("   （对照口径：两条日志都出自同一次 ingest，edges 走同一个 engine）")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="只读统计事件落库失败与增长基线")
    parser.add_argument("--since", default="", help="只看不早于该时间（ISO，含时区）")
    parser.add_argument("--json", action="store_true", help="末尾输出一段 JSON")
    args = parser.parse_args(argv)

    since = _parse(args.since) if args.since else None
    records = _read_logs()
    summary = section_logs(records, since)
    section_hours(records)
    section_db()
    section_cross(records, summary)
    if args.json:
        print("\n" + json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
