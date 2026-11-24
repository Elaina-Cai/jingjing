#!/usr/bin/env python
"""Read-only B0 comparison: legacy account analysis vs Track B projections.

The Track B handoff asks for a concrete comparison artifact before switching
``_run_analysis_pipeline``.  This script never writes either store; it prints
how many rows each side produced so a real 24-hour sample can be checked
against the same cut-off.

Examples:
    python scripts/analysis_shadow_compare.py \
        --meta-db data/yinyue_meta.db \
        --account-db data/platforms/wechat/wxid_x/account.db
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


def _safe_count(conn: sqlite3.Connection, sql: str) -> list[tuple]:
    try:
        return list(conn.execute(sql).fetchall())
    except sqlite3.Error:
        return []


def _new_projection_counts(meta_db: Path) -> list[tuple]:
    with sqlite3.connect(str(meta_db)) as conn:
        return _safe_count(
            conn,
            """
            SELECT projection_type, COUNT(*) AS rows,
                   SUM(CASE WHEN source_event_ids_json NOT IN ('', '[]')
                            THEN 1 ELSE 0 END) AS with_sources
            FROM analysis_projections
            GROUP BY projection_type
            ORDER BY projection_type
            """,
        )


def _event_counts(meta_db: Path) -> list[tuple]:
    with sqlite3.connect(str(meta_db)) as conn:
        return _safe_count(
            conn,
            """
            SELECT event_type, COUNT(*)
            FROM terminal_events
            GROUP BY event_type
            ORDER BY event_type
            """,
        )


def _legacy_counts(account_db: Path) -> list[tuple]:
    with sqlite3.connect(str(account_db)) as conn:
        return _safe_count(
            conn,
            """
            SELECT analysis_type, COUNT(*)
            FROM analysis
            GROUP BY analysis_type
            ORDER BY analysis_type
            """,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--meta-db", required=True, help="meta DB path")
    parser.add_argument(
        "--account-db",
        action="append",
        default=[],
        help="legacy account DB path (repeatable)",
    )
    args = parser.parse_args()

    meta_db = Path(args.meta_db)
    if not meta_db.exists():
        print(f"meta DB not found: {meta_db}")
        return 1

    print(f"[new] analysis_projections @ {meta_db}")
    projection_counts = _new_projection_counts(meta_db)
    if projection_counts:
        for projection_type, rows, with_sources in projection_counts:
            print(
                f"  {projection_type or '(blank)':24} "
                f"rows={int(rows):6d} with_sources={int(with_sources or 0):6d}"
            )
    else:
        print("  no analysis_projections yet")

    print(f"[source] terminal_events @ {meta_db}")
    event_counts = _event_counts(meta_db)
    if event_counts:
        for event_type, count in event_counts:
            print(f"  {event_type or '(blank)':24} {int(count):6d}")
    else:
        print("  no terminal_events yet")

    for raw_path in args.account_db:
        account_db = Path(raw_path)
        print(f"[legacy] analysis @ {account_db}")
        if not account_db.exists():
            print("  missing")
            continue
        counts = _legacy_counts(account_db)
        if counts:
            for analysis_type, count in counts:
                print(f"  {analysis_type or '(blank)':24} {int(count):6d}")
        else:
            print("  no legacy analysis rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
