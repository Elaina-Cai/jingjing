#!/usr/bin/env python
"""Remove test-fixture events from the live terminal event stream (Phase 5).

Some scheduler-level tests used to drive the real reply path without installing
a temporary event store, so their fake replies (``bot_account_id="wxid_bot"``,
content ``ok``) landed in ``terminal_events`` -- and were then projected into
``memory_projections`` and the derived vector index.

This script removes those rows from all three layers, in the right order:

1. back up the meta DB (plus WAL/SHM) and dump the ids to be deleted;
2. delete derived vector entries (``memory_embeddings``);
3. delete derived projections (``memory_projections``);
4. delete the fact rows from ``terminal_events``.

Dry-run by default:

    python scripts/cleanup_test_memory_events.py
    python scripts/cleanup_test_memory_events.py --apply
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

META_DB = ROOT / "data" / "yinyue_meta.db"
BACKUP_ROOT = ROOT / "backups"
DEFAULT_BOT_ACCOUNT = "wxid_bot"


def _counts(con: sqlite3.Connection, bot_account: str) -> dict:
    events = con.execute(
        "select count(*) from terminal_events where bot_account_id = ?",
        (bot_account,),
    ).fetchone()[0]
    projections = con.execute(
        "select count(*) from memory_projections where bot_account_id = ?",
        (bot_account,),
    ).fetchone()[0]
    types = con.execute(
        "select event_type, count(*) from terminal_events "
        "where bot_account_id = ? group by event_type",
        (bot_account,),
    ).fetchall()
    contents = con.execute(
        "select distinct substr(content, 1, 40) from terminal_events "
        "where bot_account_id = ? limit 10",
        (bot_account,),
    ).fetchall()
    return {
        "events": events,
        "projections": projections,
        "types": types,
        "contents": [row[0] for row in contents],
    }


def _projection_ids(con: sqlite3.Connection, bot_account: str) -> list[str]:
    return [
        row[0]
        for row in con.execute(
            "select memory_id from memory_projections where bot_account_id = ?",
            (bot_account,),
        )
    ]


def _event_ids(con: sqlite3.Connection, bot_account: str) -> list[str]:
    return [
        row[0]
        for row in con.execute(
            "select event_id from terminal_events where bot_account_id = ?",
            (bot_account,),
        )
    ]


def _analysis_refs(con: sqlite3.Connection, event_ids: set[str]) -> list[str]:
    refs: list[str] = []
    try:
        rows = con.execute(
            "select memory_id, source_event_ids_json from analysis_projections"
        ).fetchall()
    except sqlite3.OperationalError:
        return refs
    for memory_id, raw in rows:
        try:
            ids = json.loads(raw or "[]")
        except (TypeError, ValueError):
            continue
        if isinstance(ids, list) and any(str(i) in event_ids for i in ids):
            refs.append(str(memory_id))
    return refs


def _delete_vectors(projection_ids: list[str]) -> dict:
    if not projection_ids:
        return {"deleted": 0, "error": ""}
    try:
        from core.bootstrap import get_vector_store

        collection = get_vector_store().client.get_collection("memory_embeddings")
        existing = set(collection.get(include=[]).get("ids", []))
        wanted = [mid for mid in projection_ids if mid in existing]
        if wanted:
            collection.delete(ids=wanted)
        return {"deleted": len(wanted), "error": ""}
    except Exception as exc:  # noqa: BLE001 - operator script
        return {"deleted": 0, "error": str(exc)[:200]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bot-account", default=DEFAULT_BOT_ACCOUNT)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", default="")
    args = parser.parse_args()

    if not META_DB.exists():
        print(f"[err] meta db not found: {META_DB}")
        return 2

    con = sqlite3.connect(str(META_DB))
    before = _counts(con, args.bot_account)
    projection_ids = _projection_ids(con, args.bot_account)
    event_ids = _event_ids(con, args.bot_account)
    analysis_refs = _analysis_refs(con, set(event_ids))

    print(f"target bot_account_id = {args.bot_account!r}")
    print(f"  terminal_events      : {before['events']}")
    print(f"  memory_projections   : {before['projections']}")
    print(f"  event types          : {before['types']}")
    print(f"  distinct contents    : {before['contents'][:6]}")
    print(f"  analysis_projections referencing them: {analysis_refs or 'none'}")

    if not args.apply:
        print("\n[dry-run] nothing deleted. Re-run with --apply to clean.")
        con.close()
        return 0

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = Path(args.backup_dir) if args.backup_dir else (
        BACKUP_ROOT / f"phase5_test_event_cleanup_{stamp}"
    )
    backup_dir.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        src = Path(str(META_DB) + suffix)
        if src.exists():
            shutil.copy2(src, backup_dir / src.name)
    (backup_dir / "deleted_ids.json").write_text(
        json.dumps(
            {
                "bot_account_id": args.bot_account,
                "event_ids": event_ids,
                "projection_ids": projection_ids,
                "analysis_projection_refs": analysis_refs,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nbackup -> {backup_dir}")

    vectors = _delete_vectors(projection_ids)
    print(f"vector entries deleted : {vectors['deleted']} {vectors['error']}")

    if analysis_refs:
        con.executemany(
            "delete from analysis_projections where memory_id = ?",
            [(mid,) for mid in analysis_refs],
        )

    con.executemany(
        "delete from memory_projections where memory_id = ?",
        [(mid,) for mid in projection_ids],
    )
    con.executemany(
        "delete from terminal_events where event_id = ?",
        [(eid,) for eid in event_ids],
    )
    con.commit()

    after = _counts(con, args.bot_account)
    remaining = sqlite3.connect(str(META_DB)).execute(
        "select count(*) from terminal_events"
    ).fetchone()[0]
    cursor = con.execute(
        "select last_sequence from memory_projection_cursors where key = 'terminal_event'"
    ).fetchone()
    print(f"events deleted         : {before['events'] - after['events']}")
    print(f"projections deleted    : {before['projections'] - after['projections']}")
    print(f"terminal_events remain : {remaining}")
    print(f"projection cursor      : {cursor[0] if cursor else 'n/a'}")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
