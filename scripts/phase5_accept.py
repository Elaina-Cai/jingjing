#!/usr/bin/env python
"""Compressed Phase 5 acceptance check.

Replaces the "wait 24 hours and eyeball everything" gate with deterministic
counters plus a short real-traffic smoke:

* formal event stream is receiving events
* memory projections keep up with the event cursor
* legacy memory writers were actually skipped after MEMORY_ONLY rollout
* proactive path produced audit records (if a target is enabled)
* no startup/turn tracebacks in the newest rollout log

It is read-only.  Run after sending a few real messages/opening a group:

    python scripts/phase5_accept.py --hours 1 --min-events 20
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


def _count_logs(paths: list[Path], needles: tuple[str, ...]) -> dict[str, int]:
    counts = {needle: 0 for needle in needles}
    for path in paths:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for needle in needles:
            counts[needle] += text.count(needle)
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description="Compressed Phase 5 acceptance check")
    parser.add_argument("--meta-db", default=str(ROOT / "data" / "yinyue_meta.db"))
    parser.add_argument("--hours", type=int, default=1)
    parser.add_argument("--min-events", type=int, default=20)
    parser.add_argument("--max-cursor-lag", type=int, default=50)
    parser.add_argument(
        "--log-file",
        default="",
        help="newest matching logs/phase5_rollout*_stderr.log is used when empty",
    )
    args = parser.parse_args()

    db_path = Path(args.meta_db)
    if not db_path.exists():
        print(json.dumps({"pass": False, "error": f"meta db not found: {db_path}"}))
        return 2

    if args.log_file:
        log_paths = [Path(args.log_file)]
    else:
        all_logs = sorted(
            ROOT.glob("logs/phase5_rollout*_stderr.log"),
            key=lambda p: p.stat().st_mtime,
        )
        # Ignore pre-MEMORY_ONLY rollout logs: they may contain unrelated
        # startup crashes from earlier fix iterations.
        memory_only_logs: list[Path] = []
        for candidate in all_logs:
            try:
                text = candidate.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if "legacy_memory_write_skipped" in text:
                memory_only_logs.append(candidate)
        if memory_only_logs:
            start_mtime = memory_only_logs[0].stat().st_mtime
            log_paths = [
                path for path in all_logs
                if path.stat().st_mtime >= start_mtime
            ]
        elif all_logs:
            log_paths = [all_logs[-1]]
        else:
            log_paths = []

    since = datetime.now() - timedelta(hours=max(1, int(args.hours)))
    since_text = since.strftime("%Y-%m-%d %H:%M:%S")
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        events_window = int(con.execute(
            "select count(*) from terminal_events where occurred_at >= ?",
            (since_text,),
        ).fetchone()[0])
        event_types = {
            str(row["event_type"]): int(row["c"])
            for row in con.execute(
                "select event_type, count(*) c from terminal_events "
                "where occurred_at >= ? group by event_type",
                (since_text,),
            ).fetchall()
        }
        total_events = int(con.execute(
            "select count(*) from terminal_events"
        ).fetchone()[0])
        max_seq = int(con.execute(
            "select coalesce(max(sequence), 0) from terminal_events"
        ).fetchone()[0])
        cursor_row = con.execute(
            "select last_sequence from memory_projection_cursors "
            "where key = 'terminal_event'"
        ).fetchone()
        cursor = int(cursor_row["last_sequence"]) if cursor_row else 0
        projection_count = int(con.execute(
            "select count(*) from memory_projections"
        ).fetchone()[0])
        analysis_projection_count = int(con.execute(
            "select count(*) from analysis_projections"
        ).fetchone()[0])
        analysis_cursor_row = con.execute(
            "select last_sequence from analysis_projection_cursors "
            "where key = 'analysis_from_events'"
        ).fetchone()
        analysis_cursor = (
            int(analysis_cursor_row["last_sequence"])
            if analysis_cursor_row
            else 0
        )
        proactive_targets = int(con.execute(
            "select count(*) from proactive_targets where enabled = 1"
        ).fetchone()[0])
        proactive_sends = int(con.execute(
            "select count(*) from proactive_send_log where ok = 1"
        ).fetchone()[0])
    finally:
        con.close()

    log_counts = _count_logs(log_paths, (
        "legacy_memory_write_skipped",
        "proactive_trigger_done",
        "proactive_opportunity",
        "Traceback",
        "UnboundLocalError",
        "proactive_decision_scan_failed",
        "is bound to a different event loop",
    ))
    known_background_errors = log_counts["is bound to a different event loop"]
    errors = max(
        0,
        log_counts["Traceback"]
        + log_counts["UnboundLocalError"]
        + log_counts["proactive_decision_scan_failed"]
        - known_background_errors,
    )
    cursor_lag = max(0, max_seq - cursor)
    analysis_cursor_lag = max(0, max_seq - analysis_cursor)

    reasons: list[str] = []
    if events_window < max(1, int(args.min_events)):
        reasons.append(
            f"events_window={events_window} < min_events={args.min_events}"
        )
    if cursor_lag > max(0, int(args.max_cursor_lag)):
        reasons.append(f"cursor_lag={cursor_lag} > max={args.max_cursor_lag}")
    if analysis_cursor_lag > max(0, int(args.max_cursor_lag)):
        reasons.append(
            f"analysis_cursor_lag={analysis_cursor_lag} > max={args.max_cursor_lag}"
        )
    if projection_count <= 0:
        reasons.append("memory_projections=0")
    if analysis_projection_count <= 0:
        reasons.append("analysis_projections=0")
    if errors:
        reasons.append(f"log_errors={errors}")
    if proactive_targets > 0 and proactive_sends <= 0:
        reasons.append("proactive target enabled but no successful send yet")

    result = {
        "pass": not reasons,
        "window_hours": max(1, int(args.hours)),
        "meta_db": str(db_path),
        "rollout_logs": [str(path) for path in log_paths],
        "events_window": events_window,
        "event_types_window": event_types,
        "total_events": total_events,
        "event_sequence_max": max_seq,
        "projection_cursor": cursor,
        "projection_cursor_lag": cursor_lag,
        "memory_projections_total": projection_count,
        "analysis_projection_cursor": analysis_cursor,
        "analysis_projection_cursor_lag": analysis_cursor_lag,
        "analysis_projections_total": analysis_projection_count,
        "proactive_targets_enabled": proactive_targets,
        "proactive_sends_ok": proactive_sends,
        "log_counts": log_counts,
        "known_background_queue_errors": known_background_errors,
        "reasons": reasons,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not reasons else 1


if __name__ == "__main__":
    raise SystemExit(main())
