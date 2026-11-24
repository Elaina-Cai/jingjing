"""Meta-database startup migration checks (P2).

The meta database predates the conversation identity columns and the
conversation-window / evidence tables.  ``create_all`` adds tables but never
columns, so ``init_meta_db`` must run the targeted meta migrations before any
repository reads the ORM model.

Run directly: python tests/test_meta_migration.py
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.meta_repo import MetaRepositoryManager

_ROOT = Path(__file__).resolve().parent.parent
_DB = _ROOT / "data" / "_test_meta_migration.db"


def _cleanup() -> None:
    for suffix in ("", "-wal", "-shm"):
        Path(str(_DB) + suffix).unlink(missing_ok=True)


def _table_columns(path: Path, table: str) -> set[str]:
    conn = sqlite3.connect(str(path))
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def _table_names(path: Path) -> set[str]:
    conn = sqlite3.connect(str(path))
    try:
        return {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        conn.close()


def test_startup_migration() -> bool:
    print("\n[meta database startup migration]")
    _cleanup()
    meta = None
    try:
        conn = sqlite3.connect(str(_DB))
        conn.execute(
            "CREATE TABLE conversations ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, persona_id INTEGER, "
            "session_id INTEGER, role TEXT, content TEXT, emotion TEXT, "
            "metadata TEXT, created_at DATETIME)"
        )
        conn.execute(
            "CREATE TABLE conversation_windows ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT, account_id TEXT, "
            "chat_id TEXT, person_id TEXT, state TEXT)"
        )
        conn.commit()
        conn.close()

        meta = MetaRepositoryManager(str(_DB))
        conv_cols = _table_columns(_DB, "conversations")
        win_cols = _table_columns(_DB, "conversation_windows")
        cursor_cols = _table_columns(_DB, "evidence_index_cursors")

        ok = {"platform", "account_id", "chat_id", "sender_id"} <= conv_cols
        ok &= "last_send_log_id" in win_cols
        ok &= {
            "platform", "account_id", "last_synced_at", "last_message_id",
        } <= cursor_cols
        # Repositories must be able to query the migrated columns.
        missing = meta.conversations.list_missing_identity(limit=10)
        ok &= isinstance(missing, list)
        ok &= meta.evidence_cursors.get("wechat", "bot") is None

        # A second startup is idempotent.
        meta.engine.dispose()
        meta = MetaRepositoryManager(str(_DB))
        ok &= "sender_id" in _table_columns(_DB, "conversations")

        print(f"  conversation columns: {sorted(conv_cols)}")
        print(f"  cursor table: {'evidence_index_cursors' in _table_names(_DB)}")
        print("  ok" if ok else "  FAIL")
        return bool(ok)
    finally:
        try:
            if meta is not None:
                meta.engine.dispose()
        except Exception:
            pass
        _cleanup()


def main() -> bool:
    print("=" * 50)
    print("meta migration checks")
    print("=" * 50)
    ok = test_startup_migration()
    print("\n" + ("all passed" if ok else "FAILED"))
    return bool(ok)


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
