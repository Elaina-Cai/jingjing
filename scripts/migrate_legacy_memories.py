#!/usr/bin/env python
# 【历史工具 · 2026-09-21 归档】旧单库 data/yinyue.db 与 meta `memories` 表已按主人指令删除，
# 本脚本已完成使命，仅保留作审计/回溯参考；不要再在生产流程中运行。
"""Backfill legacy ``memories`` / ``core_memories`` into terminal_events.

Phase 5 Track C: old explicit memories must become MEMORY_WRITE events and
memory projections before the old tables can be frozen or archived.

The script is additive and idempotent:

* dry-run by default;
* deterministic event ids ``legacy:memory:<source>:<id>``;
* same-content rows are skipped by default to avoid duplicate recall;
* explicit ``--apply`` writes events and immediate memory projections;
* old tables are never deleted.

Examples:
    python scripts/migrate_legacy_memories.py
    python scripts/migrate_legacy_memories.py --apply
    python scripts/migrate_legacy_memories.py --apply --include-db data/yinyue.db
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.terminal.event_model import Event, EventType  # noqa: E402
from core.terminal.event_store_db import TerminalEventStore  # noqa: E402
from core.terminal.memory_projection import MemoryProjector  # noqa: E402
from core.terminal.principal import VisibilityScope  # noqa: E402
from db.meta_repo import MetaRepositoryManager  # noqa: E402
from db.repo_memory_projections import MemoryProjectionRepository  # noqa: E402


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value or "").strip()
    if text:
        try:
            parsed = datetime.fromisoformat(text.replace(" ", "T"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _norm_content(value: Any) -> str:
    return " ".join(str(value or "").split())


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _covered_by_existing(normalized: str, existing: list[str]) -> bool:
    """True when a projection already represents this exact legacy memory.

    Projections store ``content[:500]``, so a long legacy memory counts as
    covered only when an existing projection is a prefix of it.  Generic
    substring matching used to hide distinct memories whose text merely showed
    up inside another projection (found in the 2026-09-21 coverage audit).
    """
    if not normalized:
        return False
    for item in existing:
        if not item:
            continue
        if normalized == item:
            return True
        if len(item) >= 300 and normalized.startswith(item):
            return True
    return False


def _memory_events(
    db_path: Path,
    *,
    source_label: str,
    seen: set[str],
    existing: list[str],
    stats: dict[str, int],
):
    if not db_path.exists():
        return
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        if _table_exists(conn, "memories"):
            for row in conn.execute(
                "SELECT id, persona_id, content, memory_type, importance, "
                "created_at, source_platform, source_account_id, source_chat_id "
                "FROM memories ORDER BY id ASC"
            ):
                content = str(row["content"] or "").strip()
                normalized = _norm_content(content)
                if not content or not normalized or normalized in seen:
                    continue
                if _covered_by_existing(normalized, existing):
                    stats["skipped_existing"] += 1
                    continue
                seen.add(normalized)
                yield Event.new(
                    event_id=f"legacy:memory:{source_label}:{int(row['id'])}",
                    event_type=EventType.MEMORY_WRITE,
                    occurred_at=_parse_dt(row["created_at"]),
                    platform=str(row["source_platform"] or ""),
                    bot_account_id=str(row["source_account_id"] or ""),
                    chat_id=str(row["source_chat_id"] or ""),
                    actor_person_id="",
                    content=content,
                    visibility_scope=frozenset({VisibilityScope.OWNER_ONLY}),
                    metadata={
                        "memory_type": str(row["memory_type"] or "semantic"),
                        "importance": float(row["importance"] or 0.5),
                        "source": "legacy_import",
                        "legacy_source": source_label,
                        "legacy_memory_id": int(row["id"]),
                        "persona_id": int(row["persona_id"] or 0),
                        "explicit_remember": True,
                    },
                )

        if _table_exists(conn, "core_memories"):
            for row in conn.execute(
                "SELECT id, persona_id, block, content, updated_at "
                "FROM core_memories ORDER BY id ASC"
            ):
                content = str(row["content"] or "").strip()
                normalized = _norm_content(content)
                if not content or not normalized or normalized in seen:
                    continue
                if _covered_by_existing(normalized, existing):
                    stats["skipped_existing"] += 1
                    continue
                seen.add(normalized)
                block = str(row["block"] or "core")
                persona_id = int(row["persona_id"] or 0)
                yield Event.new(
                    event_id=f"legacy:core:{source_label}:{persona_id}:{block}",
                    event_type=EventType.MEMORY_WRITE,
                    occurred_at=_parse_dt(row["updated_at"]),
                    actor_person_id="",
                    content=content,
                    visibility_scope=frozenset({VisibilityScope.OWNER_ONLY}),
                    metadata={
                        "memory_type": "semantic",
                        "importance": 1.0,
                        "source": "legacy_core_memory",
                        "legacy_source": source_label,
                        "legacy_block": block,
                        "persona_id": persona_id,
                        "explicit_remember": True,
                    },
                )
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--meta-db",
        default=str(ROOT / "data" / "yinyue_meta.db"),
        help="target meta DB path",
    )
    parser.add_argument(
        "--include-db",
        action="append",
        default=[],
        help="legacy DB to migrate (repeatable); default data/yinyue.db",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write events/projections; without it the script only counts",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="max events to import; 0 = unlimited",
    )
    parser.add_argument(
        "--no-dedupe-existing",
        action="store_true",
        help="import legacy rows even when the content is already in memory_projections",
    )
    args = parser.parse_args()

    meta_db = Path(args.meta_db)
    if not meta_db.exists():
        print(f"meta DB not found: {meta_db}")
        return 2

    db_paths = [Path(path) for path in args.include_db]
    if not db_paths:
        db_paths = [ROOT / "data" / "yinyue.db"]
    # Also migrate the meta DB's own legacy memories table.
    if meta_db not in db_paths:
        db_paths.append(meta_db)

    meta = MetaRepositoryManager(str(meta_db))
    try:
        store = TerminalEventStore(meta.engine)
        projection_repo = MemoryProjectionRepository(meta.engine)
        projector = MemoryProjector(store, projection_repo)

        seen: set[str] = set()
        stats: dict[str, int] = {"skipped_existing": 0}
        imported = 0
        projected = 0
        sampled = 0
        limit = max(0, int(args.limit or 0))

        existing: list[str] = []
        if not args.no_dedupe_existing:
            try:
                existing = [
                    _norm_content(item.content)
                    for item in projection_repo.all(limit=5000)
                    if _norm_content(item.content)
                ]
            except Exception:
                existing = []

        for db_path in db_paths:
            source_label = db_path.stem
            for event in _memory_events(
                db_path,
                source_label=source_label,
                seen=seen,
                existing=existing,
                stats=stats,
            ):
                if limit and imported >= limit:
                    break
                if not args.apply:
                    imported += 1
                    if sampled < 5:
                        print(
                            f"[dry-run] {event.event_id} "
                            f"{event.content[:60]!r}"
                        )
                        sampled += 1
                    continue
                store.append(event)
                item = projector.project_event(event)
                if item is not None:
                    projection_repo.upsert(item)
                    projected += 1
                imported += 1

        mode = "apply" if args.apply else "dry-run"
        print(
            f"[{mode}] imported={imported} "
            f"projected={projected if args.apply else 'n/a'} "
            f"deduped={len(seen) - imported} "
            f"skipped_existing={stats['skipped_existing']}"
        )
        return 0
    finally:
        try:
            meta.engine.dispose()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
