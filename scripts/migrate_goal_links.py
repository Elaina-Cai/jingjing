#!/usr/bin/env python
"""把 work_item_created 事件里的 goal_id 回填到隔离的 goal_links 表。

默认 dry-run：只读 terminal_events，报告将写入多少条；``--apply`` 才建表/写行。
只允许沙箱路径；生产库必须额外显式 ``--allow-production``（本轮不使用）。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SANDBOX_ROOTS = (ROOT / ".test_tmp", Path(tempfile.gettempdir()) / "yinyue-legacy-import")
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS goal_links (
    subject_kind TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    goal_id TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    created_at DATETIME,
    PRIMARY KEY (subject_kind, subject_id, goal_id)
)
"""


def _inside_sandbox(path: Path) -> bool:
    resolved = path.resolve()
    for base in SANDBOX_ROOTS:
        if resolved.is_relative_to(base.resolve()):
            return True
    return False


def _rows(conn: sqlite3.Connection) -> tuple[list[dict[str, str]], int]:
    links: list[dict[str, str]] = []
    invalid = 0
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "terminal_events" in tables:
        for payload_text, at in conn.execute(
            "SELECT action_payload_json, occurred_at FROM terminal_events "
            "WHERE event_type='work_item_created' ORDER BY id"
        ):
            try:
                payload = json.loads(payload_text or "{}")
            except ValueError:
                invalid += 1
                continue
            work_item_id = str(payload.get("work_item_id") or "").strip()
            goal_id = str(payload.get("goal_id") or "").strip()
            if not work_item_id or not goal_id:
                invalid += 1
                continue
            links.append({
                "subject_kind": "work_item",
                "subject_id": work_item_id,
                "goal_id": goal_id,
                "at": str(at or ""),
            })
    if "job_events" in tables:
        for payload_text, at in conn.execute(
            "SELECT payload, ts FROM job_events WHERE kind='created' ORDER BY ts"
        ):
            try:
                payload = json.loads(payload_text or "{}")
            except ValueError:
                invalid += 1
                continue
            job_id = str(payload.get("job_id") or "").strip()
            goal_id = str(payload.get("goal_id") or "").strip()
            if not job_id or not goal_id:
                invalid += 1
                continue
            links.append({
                "subject_kind": "job",
                "subject_id": job_id,
                "goal_id": goal_id,
                "at": str(at or ""),
            })
    return links, invalid


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
        print(json.dumps({"ok": False, "error": "outside_sandbox_requires_allow_production", "db": str(path)}, ensure_ascii=False))
        return 2
    conn = sqlite3.connect(str(path))
    try:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "terminal_events" not in tables and "job_events" not in tables:
            print(json.dumps({"ok": False, "error": "event_tables_missing", "db": str(path)}, ensure_ascii=False))
            return 2
        links, invalid = _rows(conn)
        has_table = bool(
            conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='goal_links'").fetchone()
        )
        existing = 0
        if has_table:
            existing = int(conn.execute("SELECT COUNT(*) FROM goal_links").fetchone()[0])
        if not args.apply:
            print(json.dumps({
                "ok": True,
                "dry_run": True,
                "db": str(path),
                "table_exists": has_table,
                "would_link": len(links),
                "existing": existing,
                "invalid": invalid,
                "links": links,
            }, ensure_ascii=False))
            return 0
        with conn:
            conn.execute(CREATE_SQL)
            for link in links:
                conn.execute(
                    "INSERT OR IGNORE INTO goal_links "
                    "(subject_kind, subject_id, goal_id, source, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        link["subject_kind"],
                        link["subject_id"],
                        link["goal_id"],
                        "work_item_created" if link["subject_kind"] == "work_item" else "job_created",
                        link["at"] or None,
                    ),
                )
        total = int(conn.execute("SELECT COUNT(*) FROM goal_links").fetchone()[0])
        print(json.dumps({
            "ok": True,
            "dry_run": False,
            "db": str(path),
            "table_exists": True,
            "would_link": len(links),
            "existing": existing,
            "invalid": invalid,
            "linked_total": total,
        }, ensure_ascii=False))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
