"""Remap legacy ThreadMsgRef.msg_id values to global message identities.

After the message identity migration, ``thread_msg_ref.msg_id`` must reference
``Message.id`` (a global 63-bit identity), not the platform-local ``msg_id``.
This script scans every registered account database, builds a platform-id ->
global-id map, and rewrites meta thread refs in place.

Usage:
    python scripts/migrate_thread_refs.py --dry-run
    python scripts/migrate_thread_refs.py
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict

from sqlalchemy import select, update

# Allow running directly from the repository root.
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.bootstrap import get_account_db_manager, get_meta_repo
from core.config.settings import config
from db.meta_models import ThreadMsgRef


def build_platform_map() -> tuple[set[int], dict[int, list[int]]]:
    adm = get_account_db_manager()
    global_ids: set[int] = set()
    platform_to_global: dict[int, list[int]] = defaultdict(list)
    for (_platform, _account_id), repo in adm.all_repos().items():
        # Use the repository's engine directly so this script does not depend
        # on the repository class internals.
        from sqlalchemy import text
        with repo.engine.connect() as conn:
            for row in conn.execute(text(
                "SELECT id, msg_id, chat_id, account_id, source FROM messages"
            )):
                global_id = int(row[0])
                platform_msg_id = int(row[1]) if row[1] is not None else 0
                global_ids.add(global_id)
                platform_to_global[platform_msg_id].append(global_id)
    return global_ids, platform_to_global


def migrate(dry_run: bool = False) -> dict:
    global_ids, platform_map = build_platform_map()
    meta = get_meta_repo()
    updated = 0
    ambiguous = 0
    missing = 0
    already = 0

    with meta.threads._session() as session:
        refs = session.execute(select(ThreadMsgRef)).scalars().all()
        for ref in refs:
            current = int(ref.msg_id)
            if current in global_ids:
                already += 1
                continue
            candidates = platform_map.get(current) or []
            if len(candidates) == 1:
                if not dry_run:
                    ref.msg_id = candidates[0]
                updated += 1
            elif len(candidates) > 1:
                ambiguous += 1
            else:
                missing += 1
        if not dry_run:
            session.commit()

    result = {
        "thread_refs": len(refs) if 'refs' in locals() else 0,
        "updated": updated,
        "already_global": already,
        "ambiguous": ambiguous,
        "missing": missing,
        "dry_run": dry_run,
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    args = parser.parse_args()
    result = migrate(dry_run=args.dry_run)
    for key, value in result.items():
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
