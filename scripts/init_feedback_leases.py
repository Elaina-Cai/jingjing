"""Explicitly create the opt-in feedback lease table.

The runtime never creates this table implicitly.  Keep this script's DDL in
sync with ``core.quality.feedback_lease.CREATE_SQL``; the test suite compares
them so a schema edit cannot silently diverge.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SANDBOX_ROOTS = (ROOT / ".test_tmp", Path(tempfile.gettempdir()) / "yinyue-legacy-import")
TABLE = "feedback_leases"
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS feedback_leases (
    sample_id INTEGER PRIMARY KEY,
    owner TEXT NOT NULL,
    token TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
)
"""


def _inside_sandbox(path: Path) -> bool:
    resolved = path.resolve()
    for base in SANDBOX_ROOTS:
        if resolved.is_relative_to(base.resolve()):
            return True
    return False


def _has_table(conn: sqlite3.Connection) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)
        ).fetchone()
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--allow-production", action="store_true")
    args = parser.parse_args(argv)
    path = Path(args.db).expanduser()
    if not path.exists():
        print(json.dumps({"ok": False, "error": "database_not_found", "db": str(path)}, ensure_ascii=False))
        return 2
    if not _inside_sandbox(path) and not args.allow_production:
        print(json.dumps({
            "ok": False,
            "error": "outside_sandbox_requires_allow_production",
            "db": str(path),
        }, ensure_ascii=False))
        return 2
    conn = sqlite3.connect(str(path))
    try:
        exists = _has_table(conn)
        if not args.apply:
            print(json.dumps({
                "ok": True,
                "dry_run": True,
                "db": str(path),
                "table": TABLE,
                "table_exists": exists,
            }, ensure_ascii=False))
            return 0
        with conn:
            conn.execute(CREATE_SQL)
        print(json.dumps({
            "ok": True,
            "dry_run": False,
            "db": str(path),
            "table": TABLE,
            "table_exists": True,
            "created": not exists,
        }, ensure_ascii=False))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
