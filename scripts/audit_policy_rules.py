#!/usr/bin/env python
"""Audit `policy_rules` rows that still carry the retired `analysis_only` mode.

WP-01a: `ReplyMode.ANALYSIS_ONLY` was a no-op (router treated it exactly like
NEVER).  The enum is deleted; this script finds rule rows that would silently
fall back to the default mode after the deletion, and can rewrite them.

    python scripts/audit_policy_rules.py          # dry-run (read-only)
    python scripts/audit_policy_rules.py --fix    # rewrite payload_json
    python scripts/audit_policy_rules.py --db <path>   # extra db, repeatable

Exit code 1 when hits exist and --fix was not given.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


def candidate_dbs(extra: list[str]) -> list[Path]:
    paths: list[Path] = []
    meta = os.environ.get("META_DB_PATH") or str(ROOT / "data" / "yinyue_meta.db")
    paths.append(Path(meta))
    platforms = Path(os.environ.get("PLATFORMS_DIR") or str(ROOT / "data" / "platforms"))
    if platforms.is_dir():
        paths.extend(sorted(platforms.glob("*/*/account.db")))
    paths.extend(Path(p) for p in extra)
    seen: set[str] = set()
    out: list[Path] = []
    for path in paths:
        key = str(path.resolve()) if path.exists() else str(path)
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "select 1 from sqlite_master where type='table' and name=?", (name,)
    ).fetchone()
    return row is not None


def scan(db: Path) -> list[tuple]:
    if not db.exists():
        return []
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        if not _has_table(conn, "policy_rules"):
            return []
        cols = {row[1] for row in conn.execute("PRAGMA table_info(policy_rules)")}
        if "payload_json" not in cols:
            return []
        return conn.execute(
            "SELECT id, key, category, enabled, payload_json FROM policy_rules "
            "WHERE payload_json LIKE '%analysis_only%'"
        ).fetchall()
    finally:
        conn.close()


def rewrite(db: Path, rows: list[tuple]) -> int:
    conn = sqlite3.connect(str(db))
    try:
        changed = 0
        for rid, _key, _category, _enabled, payload in rows:
            new_payload = None
            try:
                data = json.loads(payload or "{}")
            except Exception:
                data = None
            if isinstance(data, dict) and str(data.get("reply_mode", "")).lower() == "analysis_only":
                data["reply_mode"] = "never"
                new_payload = json.dumps(data, ensure_ascii=False)
            else:
                text = str(payload or "")
                replaced = text.replace('"analysis_only"', '"never"').replace(
                    "'analysis_only'", "'never'"
                )
                if replaced != text:
                    new_payload = replaced
            if new_payload is not None:
                conn.execute(
                    "UPDATE policy_rules SET payload_json=? WHERE id=?",
                    (new_payload, rid),
                )
                changed += 1
        conn.commit()
        return changed
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fix", action="store_true", help="写回（默认只读）")
    parser.add_argument("--db", action="append", default=[], help="额外 db 路径，可重复")
    args = parser.parse_args()

    total = 0
    for db in candidate_dbs(args.db):
        rows = scan(db)
        if not rows:
            print(f"[ok] {db} 0 命中")
            continue
        print(f"[hit] {db} {len(rows)} 行")
        for rid, key, category, enabled, payload in rows:
            print(
                f"  id={rid} key={key} category={category} "
                f"enabled={enabled} payload={str(payload)[:200]}"
            )
        total += len(rows)
        if args.fix:
            print(f"[fix] {db} 改写 {rewrite(db, rows)} 行")

    print(f"total_hits={total}")
    if total and not args.fix:
        print("dry-run：确认后加 --fix 迁移（会写 policy_rules）")
        return 1
    return 0


if __name__ == "__main__":
    _stdout()
    raise SystemExit(main())
