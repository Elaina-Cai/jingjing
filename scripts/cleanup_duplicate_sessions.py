"""Clean duplicate empty ``chat_sessions`` rows and optionally reset windows.

Background
----------
Before the exact-JSON metadata lookup fix in ``MessageRouter._get_or_create_session``
every incoming WeChat message created a brand-new ``chat_sessions`` row.  The
rows created by that bug are empty: no ``conversations`` row references them,
and the fixed router now reuses the newest row.  They only waste space and make
Count/log diagnostics look broken (e.g. one group with 503 sessions).

This script is deliberately conservative:

* it groups sessions by ``(source, metadata)`` where metadata is not NULL;
* inside each group it keeps the *newest* row (the one the router reuses);
* it archives and deletes only duplicate rows that have **zero** conversations;
* any duplicate that actually owns conversations is reported and left alone;
* it can optionally archive + delete ``conversation_windows`` rows for the chat
  ids passed with ``--chat-id`` (used to reset a canary group's polluted state).

Dry-run is the default; pass ``--apply`` to write.  Archive is a JSON file under
``data/archive/`` so every deleted row can be restored manually.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
from pathlib import Path
from typing import Any


def _connect(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    return con


def _rows_as_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def _find_duplicate_sessions(con: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return duplicate session rows that are safe to delete (no conversations)."""
    return con.execute(
        """
        WITH groups AS (
            SELECT source, metadata, MAX(id) AS keep_id, COUNT(*) AS n
            FROM chat_sessions
            WHERE metadata IS NOT NULL
            GROUP BY source, metadata
            HAVING n > 1
        )
        SELECT s.*
        FROM chat_sessions AS s
        JOIN groups AS g
          ON s.source = g.source AND s.metadata = g.metadata
        WHERE s.id != g.keep_id
          AND NOT EXISTS (
              SELECT 1 FROM conversations AS c WHERE c.session_id = s.id
          )
        ORDER BY s.id
        """
    ).fetchall()


def _find_kept_with_conversations(con: sqlite3.Connection) -> list[dict[str, Any]]:
    """Report duplicates that hold conversations and therefore are kept."""
    rows = con.execute(
        """
        WITH groups AS (
            SELECT source, metadata, MAX(id) AS keep_id, COUNT(*) AS n
            FROM chat_sessions
            WHERE metadata IS NOT NULL
            GROUP BY source, metadata
            HAVING n > 1
        )
        SELECT s.id, s.source, s.metadata, COUNT(c.id) AS conversations
        FROM chat_sessions AS s
        JOIN groups AS g
          ON s.source = g.source AND s.metadata = g.metadata
        LEFT JOIN conversations AS c ON c.session_id = s.id
        WHERE s.id != g.keep_id
        GROUP BY s.id
        HAVING conversations > 0
        ORDER BY conversations DESC, s.id
        """
    ).fetchall()
    return _rows_as_dicts(rows)


def _find_windows(
    con: sqlite3.Connection, chat_ids: list[str]
) -> list[sqlite3.Row]:
    if not chat_ids:
        return []
    placeholders = ",".join("?" for _ in chat_ids)
    return con.execute(
        f"SELECT * FROM conversation_windows WHERE chat_id IN ({placeholders})",
        chat_ids,
    ).fetchall()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/yinyue_meta.db")
    parser.add_argument(
        "--archive-dir", default="data/archive",
        help="where to write the JSON archive of deleted rows",
    )
    parser.add_argument(
        "--chat-id", action="append", default=[],
        help="also archive + delete conversation_windows with this chat_id "
             "(repeatable)",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="actually delete; without it only a dry-run is printed",
    )
    parser.add_argument(
        "--vacuum", action="store_true",
        help="VACUUM the database after applying (requires free disk space)",
    )
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"[!] db not found: {db_path}")
        return 2

    con = _connect(str(db_path))
    try:
        dupes = _find_duplicate_sessions(con)
        kept = _find_kept_with_conversations(con)
        windows = _find_windows(con, args.chat_id)

        print(f"db: {db_path}")
        print(f"empty duplicate sessions to delete : {len(dupes)}")
        for row in dupes[:5]:
            print(f"  - id={row['id']} source={row['source']} "
                  f"metadata={str(row['metadata'])[:70]}")
        if len(dupes) > 5:
            print(f"  ... and {len(dupes) - 5} more")
        print(f"duplicates with conversations (kept): {len(kept)}")
        for row in kept[:5]:
            print(f"  - id={row['id']} source={row['source']} "
                  f"conversations={row['conversations']}")
        print(f"conversation_windows to reset      : {len(windows)}")
        for row in windows:
            print(f"  - id={row['id']} chat_id={row['chat_id']} "
                  f"person_id={row['person_id']} state={row['state']}")

        if not args.apply:
            print("\ndry-run only; re-run with --apply to delete")
            return 0

        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        archive_dir = Path(args.archive_dir)
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_path = archive_dir / f"session_cleanup_{stamp}.json"
        payload = {
            "created_at": dt.datetime.now().isoformat(timespec="seconds"),
            "db": str(db_path),
            "deleted_sessions": _rows_as_dicts(dupes),
            "kept_duplicates_with_conversations": kept,
            "deleted_conversation_windows": _rows_as_dicts(windows),
        }
        archive_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

        with con:
            if dupes:
                ids = [int(row["id"]) for row in dupes]
                placeholders = ",".join("?" for _ in ids)
                con.execute(
                    f"DELETE FROM chat_sessions WHERE id IN ({placeholders})",
                    ids,
                )
            if windows:
                ids = [int(row["id"]) for row in windows]
                placeholders = ",".join("?" for _ in ids)
                con.execute(
                    f"DELETE FROM conversation_windows WHERE id IN ({placeholders})",
                    ids,
                )
        con.commit()

        if args.vacuum:
            con.execute("VACUUM")

        print(f"\napplied. archive: {archive_path}")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
