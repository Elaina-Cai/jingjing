"""Backfill ``conversations.sender_name`` so group history stays attributable.

Why this exists
---------------
Migration 27 adds the column, but every row written before it is NULL, and the
940-odd group rows already in the meta database are exactly the ones that used
to render as an anonymous "用户".  Without this the fix only applies to new
turns, while the history the model actually reads stays unattributed -- and the
rolling summary built from it keeps the loss.

Where the names come from
-------------------------
``conversations.sender_id`` is already populated (a separate backfill did that),
so this only needs wxid -> display name:

* ``contacts`` in each account database -- the current nickname, so it wins;
* ``messages.sender_name`` -- a snapshot taken when the message arrived, used
  for anyone the contact book no longer carries.

Usage::

    python scripts/backfill_conversation_sender_name.py --dry-run
    python scripts/backfill_conversation_sender_name.py
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Stands in for "we could not work out who this was"; not an identity, so a row
# carrying one is left NULL and renders as a plain, unnamed speaker.
PLACEHOLDER_SENDERS = frozenset({"", "unknown", "?", "-", "none", "null", "0"})


def _tables(con: sqlite3.Connection) -> set[str]:
    return {r[0] for r in con.execute(
        "select name from sqlite_master where type='table'")}


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def _target_databases() -> tuple[list[Path], list[Path]]:
    """(databases holding conversations, databases holding message/contacts).

    They differ: conversations live in the shared meta database and in each
    account database, while messages and contacts only exist in the account
    databases.
    """
    from core.config.settings import config

    conversation_dbs: list[Path] = []
    source_dbs: list[Path] = []

    meta = Path(str(config.paths.meta_db_path))
    conversation_dbs.append(meta)
    source_dbs.append(meta)

    for account in config.all_account_configs():
        path = Path(str(account.db_path or ""))
        if not path.name:
            continue
        conversation_dbs.append(path)
        source_dbs.append(path)

    try:
        from core.bootstrap import get_legacy_db_path

        conversation_dbs.append(Path(get_legacy_db_path()))
    except Exception:
        pass

    def unique(paths: list[Path]) -> list[Path]:
        seen, out = set(), []
        for path in paths:
            key = str(path.resolve()).lower()
            if key in seen or not path.exists():
                continue
            seen.add(key)
            out.append(path)
        return out

    return unique(conversation_dbs), unique(source_dbs)


def build_name_map(source_dbs: list[Path]) -> tuple[dict[str, str], dict]:
    """wxid -> display name.  Contacts are read last so they win."""
    names: dict[str, str] = {}
    snapshots = 0
    contacts_read = 0

    # Message snapshots first: they are historical, and a later contact name
    # should override them.
    for path in source_dbs:
        try:
            con = sqlite3.connect(path)
        except sqlite3.Error:
            continue
        try:
            if "messages" not in _tables(con):
                continue
            cols = _columns(con, "messages")
            if not {"sender_id", "sender_name"} <= cols:
                continue
            # Ascending id so the newest snapshot for a wxid ends up last.
            rows = con.execute(
                "select sender_id, sender_name from messages "
                "where sender_name is not null and trim(sender_name) <> '' "
                "order by id asc"
            ).fetchall()
            for sender_id, name in rows:
                key = str(sender_id or "").strip()
                value = " ".join(str(name or "").split())
                if key and value and key.lower() not in PLACEHOLDER_SENDERS:
                    names[key] = value
                    snapshots += 1
        except sqlite3.Error:
            continue
        finally:
            con.close()

    for path in source_dbs:
        try:
            con = sqlite3.connect(path)
        except sqlite3.Error:
            continue
        try:
            if "contacts" not in _tables(con):
                continue
            cols = _columns(con, "contacts")
            if "wxid" not in cols:
                continue
            wanted = [c for c in ("remark", "nickname") if c in cols]
            if not wanted:
                continue
            rows = con.execute(
                f"select wxid, {', '.join(wanted)} from contacts"
            ).fetchall()
            for row in rows:
                key = str(row[0] or "").strip()
                value = ""
                for candidate in row[1:]:
                    value = " ".join(str(candidate or "").split())
                    if value:
                        break
                if key and value and key.lower() not in PLACEHOLDER_SENDERS:
                    names[key] = value
                    contacts_read += 1
        except sqlite3.Error:
            continue
        finally:
            con.close()

    return names, {"from_messages": snapshots, "from_contacts": contacts_read}


def backfill_one(path: Path, names: dict[str, str],
                 dry_run: bool) -> dict:
    report = {"db": str(path), "updated": 0, "already": 0,
              "unmapped": {}, "skipped": ""}
    try:
        con = sqlite3.connect(path)
    except sqlite3.Error as exc:
        report["skipped"] = f"cannot open: {exc}"
        return report
    try:
        if "conversations" not in _tables(con):
            report["skipped"] = "no conversations table"
            return report
        cols = _columns(con, "conversations")
        if "sender_name" not in cols:
            report["skipped"] = "sender_name missing; run migrations first"
            return report

        report["already"] = con.execute(
            "select count(*) from conversations "
            "where role='user' and sender_name is not null and sender_name <> ''"
        ).fetchone()[0]

        pending = con.execute(
            "select sender_id, count(*) from conversations "
            "where role='user' and (sender_name is null or sender_name = '') "
            "group by sender_id"
        ).fetchall()

        for sender_id, count in pending:
            key = str(sender_id or "").strip()
            name = names.get(key, "")
            if not name or key.lower() in PLACEHOLDER_SENDERS:
                report["unmapped"][key or "(null)"] = count
                continue
            report["updated"] += count
            if not dry_run:
                con.execute(
                    "update conversations set sender_name = ? "
                    "where role='user' and (sender_name is null or sender_name = '') "
                    "and sender_id = ?",
                    (name, sender_id),
                )
        if not dry_run and report["updated"]:
            con.commit()
    except sqlite3.Error as exc:
        report["skipped"] = f"error: {exc}"
    finally:
        con.close()
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--db", action="append", default=[],
                        help="only this database (repeatable)")
    args = parser.parse_args()

    conversation_dbs, source_dbs = _target_databases()
    names, stats = build_name_map(source_dbs)
    print(f"name map: {len(names)} people "
          f"(messages={stats['from_messages']}, contacts={stats['from_contacts']})")
    for key in sorted(names):
        print(f"    {key:28} -> {names[key]}")

    targets = [Path(p) for p in args.db] if args.db else conversation_dbs
    total = 0
    for path in targets:
        report = backfill_one(path, names, args.dry_run)
        label = "would update" if args.dry_run else "updated"
        print(f"\n{report['db']}")
        print(f"    already named : {report['already']}")
        print(f"    {label:14}: {report['updated']}")
        if report["skipped"]:
            print(f"    skipped       : {report['skipped']}")
        if report["unmapped"]:
            print(f"    unmapped      : {report['unmapped']}")
        total += report["updated"]

    print(f"\ntotal {label}: {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
