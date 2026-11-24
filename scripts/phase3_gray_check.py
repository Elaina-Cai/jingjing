"""Phase 3 real-run observation report.

Reads the formal metadata tables and prints a small JSON snapshot:

* terminal event distribution (message / reply / thought / action / command ...)
* silent decisions (thought events containing ``[SILENT]``)
* tool action calls
* memory projection types
* command ledger status counts

Run:
    python scripts/phase3_gray_check.py
    python scripts/phase3_gray_check.py --hours 24
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _table_or_none(engine, model):
    try:
        from sqlalchemy import select

        with engine.connect() as conn:
            conn.execute(select(model).limit(1))
        return True
    except Exception:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 3 gray-run observation")
    parser.add_argument("--meta-db", default=str(ROOT / "data" / "yinyue_meta.db"))
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--limit", type=int, default=5000)
    args = parser.parse_args()

    from sqlalchemy import func, select
    from sqlalchemy.orm import Session
    from sqlalchemy import create_engine

    from db.meta_models import (
        CommandRecordRow,
        MemoryProjectionRow,
        TerminalEvent,
    )

    engine = create_engine(f"sqlite:///{args.meta_db}")
    since = datetime.now() - timedelta(hours=max(1, int(args.hours)))
    report: dict = {
        "meta_db": args.meta_db,
        "window_hours": max(1, int(args.hours)),
        "events": {},
        "thought_silent": 0,
        "tool_actions": {},
        "memory_projection_types": {},
        "commands": {},
    }
    try:
        with Session(engine) as session:
            if _table_or_none(engine, TerminalEvent):
                rows = session.execute(
                    select(TerminalEvent.event_type, func.count(TerminalEvent.id))
                    .where(TerminalEvent.occurred_at >= since)
                    .group_by(TerminalEvent.event_type)
                ).all()
                report["events"] = {
                    str(row[0] or "unknown"): int(row[1] or 0) for row in rows
                }
                report["thought_silent"] = int(
                    session.execute(
                        select(func.count(TerminalEvent.id)).where(
                            TerminalEvent.occurred_at >= since,
                            TerminalEvent.event_type == "thought",
                            TerminalEvent.content.contains("[SILENT]"),
                        )
                    ).scalar() or 0
                )
                action_rows = session.execute(
                    select(
                        TerminalEvent.action_payload_json,
                        func.count(TerminalEvent.id),
                    )
                    .where(
                        TerminalEvent.occurred_at >= since,
                        TerminalEvent.event_type == "action",
                    )
                    .group_by(TerminalEvent.action_payload_json)
                ).all()
                for payload_json, count in action_rows:
                    try:
                        payload = json.loads(payload_json or "{}")
                        name = str(payload.get("action") or "")
                    except Exception:
                        name = ""
                    key = name or "unknown"
                    report["tool_actions"][key] = (
                        report["tool_actions"].get(key, 0) + int(count or 0)
                    )
            if _table_or_none(engine, MemoryProjectionRow):
                rows = session.execute(
                    select(MemoryProjectionRow.memory_type, func.count(MemoryProjectionRow.id))
                    .where(MemoryProjectionRow.created_at >= since)
                    .group_by(MemoryProjectionRow.memory_type)
                ).all()
                report["memory_projection_types"] = {
                    str(row[0] or "unknown"): int(row[1] or 0) for row in rows
                }
            if _table_or_none(engine, CommandRecordRow):
                rows = session.execute(
                    select(CommandRecordRow.status, func.count(CommandRecordRow.id))
                    .where(CommandRecordRow.created_at >= since)
                    .group_by(CommandRecordRow.status)
                ).all()
                report["commands"] = {
                    str(row[0] or "unknown"): int(row[1] or 0) for row in rows
                }
    finally:
        engine.dispose()

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
