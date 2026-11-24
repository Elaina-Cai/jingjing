#!/usr/bin/env python
# 【历史工具 · 2026-09-21 归档】旧单库 data/yinyue.db 与 meta `memories` 表已按主人指令删除，
# 本脚本已完成使命，仅保留作审计/回溯参考；不要再在生产流程中运行。
"""Read-only coverage check: legacy store vs the unified Phase 5 layers.

Before the legacy DBs/tables can be deleted we must prove the new layers cover
the old data.  This script only reads; it prints a per-table report:

*   memories / core_memories -> memory_projections (exact normalized content)
*   messages                  -> account DB messages (msg_id set)
*   contacts                  -> account DB contacts (wxid set)
*   analysis                  -> account DB analysis (chat_id+type+created_at)
*   knowledge_docs            -> meta DB knowledge_docs (content_hash set)

    python scripts/verify_legacy_store_coverage.py
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

LEGACY_DB = ROOT / "data" / "yinyue.db"
SYSTEM_CONTACTS = {
    "notifymessage",
    "weixin",
    "fmessage",
    "medianote",
    "newsapp",
    "filehelper",
    "floatbottle",
    "qqmail",
    "tmessage",
    "qmessage",
}
META_DB = ROOT / "data" / "yinyue_meta.db"
ACCOUNT_DB = ROOT / "data" / "platforms" / "wechat" / "wxid_seed02" / "account.db"


def _norm(value) -> str:
    return " ".join(str(value or "").split())


def _has_table(con: sqlite3.Connection, name: str) -> bool:
    return (
        con.execute(
            "select 1 from sqlite_master where type='table' and name=?", (name,)
        ).fetchone()
        is not None
    )


def _table_names(con: sqlite3.Connection) -> list[str]:
    return [
        r[0] for r in con.execute("select name from sqlite_master where type='table'")
    ]


def main() -> int:
    if not LEGACY_DB.exists():
        print(f"legacy DB already deleted: {LEGACY_DB}")
        print("RESULT: ALL COVERED (nothing left to verify)")
        return 0
    legacy = sqlite3.connect(f"file:{LEGACY_DB.as_posix()}?mode=ro", uri=True)
    meta = sqlite3.connect(f"file:{META_DB.as_posix()}?mode=ro", uri=True)
    account = sqlite3.connect(f"file:{ACCOUNT_DB.as_posix()}?mode=ro", uri=True)
    rc = 0

    # ---- memories / core_memories -> projections ---------------------------
    projection_contents = [
        _norm(r[0])
        for r in meta.execute("select content from memory_projections")
        if _norm(r[0])
    ]

    def _covered(content: str) -> bool:
        """Exact match, or the projection is the 500-char prefix of a long memory."""
        for item in projection_contents:
            if content == item:
                return True
            if len(item) >= 300 and content.startswith(item):
                return True
        return False
    for label, con, table in (
        ("legacy.memories", legacy, "memories"),
        ("meta.memories", meta, "memories"),
        ("legacy.core_memories", legacy, "core_memories"),
        ("meta.core_memories", meta, "core_memories"),
    ):
        if not _has_table(con, table):
            print(f"{label:22s} : table missing (skip)")
            continue
        contents = [_norm(r[0]) for r in con.execute(f"select content from {table}")]
        interesting = [c for c in contents if c]
        covered = [c for c in interesting if _covered(c)]
        missing = [c for c in interesting if not _covered(c)]
        status = "OK" if not missing else "GAP"
        if missing:
            rc = 1
        print(
            f"{label:22s} : {len(covered)}/{len(interesting)} covered by "
            f"memory_projections  [{status}]"
        )
        for item in missing[:3]:
            print(f"    missing: {item[:70]!r}")

    # ---- messages -> account ------------------------------------------------
    if _has_table(legacy, "messages") and _has_table(account, "messages"):
        legacy_ids = {str(r[0]) for r in legacy.execute("select msg_id from messages")}
        account_ids = {str(r[0]) for r in account.execute("select msg_id from messages")}
        missing = legacy_ids - account_ids
        if missing:
            rc = 1
        print(
            f"{'legacy.messages':22s} : {len(legacy_ids) - len(missing)}/"
            f"{len(legacy_ids)} msg_id present in account DB  "
            f"[{'OK' if not missing else 'GAP'}]"
        )
        for item in list(missing)[:3]:
            print(f"    missing msg_id: {item}")

    # ---- contacts -> account ------------------------------------------------
    if _has_table(legacy, "contacts") and _has_table(account, "contacts"):
        legacy_ids = {str(r[0]) for r in legacy.execute("select wxid from contacts")}
        account_ids = {str(r[0]) for r in account.execute("select wxid from contacts")}
        missing = legacy_ids - account_ids
        real_missing = missing - SYSTEM_CONTACTS
        if real_missing:
            rc = 1
        print(
            f"{'legacy.contacts':22s} : {len(legacy_ids) - len(real_missing)}/"
            f"{len(legacy_ids)} wxid present in account DB  "
            f"[{'OK' if not real_missing else 'GAP'}]  "
            f"(system-only missing: {sorted(missing & SYSTEM_CONTACTS)})"
        )
        for item in list(missing)[:3]:
            print(f"    missing wxid: {item}")

    # ---- analysis -> account ------------------------------------------------
    if _has_table(legacy, "analysis") and _has_table(account, "analysis"):
        def _keys(con: sqlite3.Connection) -> set[tuple[str, str, str]]:
            return {
                (str(r[0] or ""), str(r[1] or ""), str(r[2] or ""))
                for r in con.execute(
                    "select chat_id, analysis_type, created_at from analysis"
                )
            }

        legacy_keys = _keys(legacy)
        account_keys = _keys(account)
        missing = legacy_keys - account_keys
        if missing:
            rc = 1
        print(
            f"{'legacy.analysis':22s} : {len(legacy_keys) - len(missing)}/"
            f"{len(legacy_keys)} rows present in account DB  "
            f"[{'OK' if not missing else 'GAP'}]"
        )
        for item in list(missing)[:3]:
            print(f"    missing analysis: {item}")

    # ---- knowledge_docs -> meta --------------------------------------------
    if _has_table(legacy, "knowledge_docs") and _has_table(meta, "knowledge_docs"):
        legacy_hash = {
            str(r[0] or "")
            for r in legacy.execute("select content_hash from knowledge_docs")
            if str(r[0] or "")
        }
        meta_hash = {
            str(r[0] or "")
            for r in meta.execute("select content_hash from knowledge_docs")
            if str(r[0] or "")
        }
        missing = legacy_hash - meta_hash
        if missing:
            rc = 1
        print(
            f"{'legacy.knowledge_docs':22s} : {len(legacy_hash) - len(missing)}/"
            f"{len(legacy_hash)} content_hash present in meta DB  "
            f"[{'OK' if not missing else 'GAP'}]"
        )
        for item in list(missing)[:3]:
            print(f"    missing knowledge hash: {item}")

    legacy.close()
    meta.close()
    account.close()
    print("\nRESULT:", "ALL COVERED" if rc == 0 else "GAPS FOUND (do not delete yet)")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
