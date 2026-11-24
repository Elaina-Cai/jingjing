"""Durable memory ownership handback; only the explicit isolated rollback writes it."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3

from core.memory.legacy_import import import_domain

TABLE = "legacy_memory_rollback_owner"
GUARD = "legacy_memory_rollback_guard"
MEMORY_OPERATIONS = frozenset({"believe", "revise", "retract"})
GUARD_SQL = ("CREATE TRIGGER legacy_memory_rollback_guard BEFORE INSERT ON cognitive_events "
             "WHEN NEW.operation IN ('believe','revise','retract') AND EXISTS("
             "SELECT 1 FROM legacy_memory_rollback_owner WHERE scope_key="
             "json_array(json_extract(NEW.scope,'$.platform'),json_extract(NEW.scope,'$.account_id'),json_extract(NEW.scope,'$.chat_id'))) "
             "BEGIN SELECT RAISE(ABORT,'cognitive_memory_retired_use_legacy'); END")


def retired(conn, scope_key: str) -> bool:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone():
        return False
    return conn.execute("SELECT 1 FROM legacy_memory_rollback_owner WHERE scope_key=?", (scope_key,)).fetchone() is not None


def binding_scope(source, target, source_id: str) -> str:
    """Return the retired scope only for the exact file/source binding; otherwise fail closed."""
    conn = sqlite3.connect(Path(target).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone():
            return ""
        rows = conn.execute("SELECT scope_key,source_id,source_path,file_device,file_inode FROM legacy_memory_rollback_owner").fetchall()
        if not rows:
            return ""
        domain = import_domain(conn)
        if domain is None or domain[0] != source_id or len(rows) != 1:
            raise ValueError("legacy_rollback_domain_conflict")
        scope, owner, path, device, inode = rows[0]
        stat = Path(source).stat()
        if ((owner, scope) != domain or os.path.normcase(path) != os.path.normcase(str(Path(source).resolve()))
                or (device, inode) != (stat.st_dev, stat.st_ino)):
            raise ValueError("legacy_rollback_source_binding_conflict")
        coords = json.loads(scope)
        if not isinstance(coords, list) or len(coords) != 3 or not all(isinstance(v, str) and v for v in coords):
            raise ValueError("legacy_rollback_scope_invalid")
        guard = conn.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (GUARD,)).fetchone()
        if guard is None or guard[0].strip() != GUARD_SQL:
            raise ValueError("legacy_rollback_guard_missing_or_changed")
        return scope
    finally:
        conn.close()


def retire_attached_target(conn, binding, reason: str) -> None:
    """Caller owns one rollback-journal transaction covering main + rollback_target."""
    domain = import_domain(conn, schema="rollback_target")
    if domain != (binding.source_id, binding.scope_key):
        raise ValueError("legacy_rollback_domain_conflict")
    conn.execute("CREATE TABLE IF NOT EXISTS rollback_target.legacy_memory_rollback_owner("
                 "scope_key TEXT PRIMARY KEY,source_id TEXT NOT NULL,source_path TEXT NOT NULL,"
                 "file_device INTEGER NOT NULL,file_inode INTEGER NOT NULL,reason TEXT NOT NULL)")
    conn.execute("INSERT INTO rollback_target.legacy_memory_rollback_owner VALUES(?,?,?,?,?,?)",
                 (binding.scope_key, binding.source_id, str(binding.source), *binding.file_identity, reason))
    conn.execute(GUARD_SQL.replace("CREATE TRIGGER ", "CREATE TRIGGER rollback_target.", 1))
