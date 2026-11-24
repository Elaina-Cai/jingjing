"""Backfill historical group join/leave notices into membership epochs.

Scans every registered account repository for group system notices, converts
them with the same parser used by live ingestion, and writes
``group_membership_periods`` / ``group_memberships``.

Examples:
    python scripts/phase2_membership_backfill.py
    python scripts/phase2_membership_backfill.py --page-size 2000 --max-pages 100
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill group membership epochs")
    parser.add_argument("--page-size", type=int, default=2000)
    parser.add_argument("--max-pages", type=int, default=200)
    args = parser.parse_args()

    from core.bootstrap import get_account_db_manager, get_meta_repo
    from core.terminal.group_membership_service import GroupMembershipService

    account_manager = get_account_db_manager()
    meta_repo = get_meta_repo()
    service = GroupMembershipService(meta_repo)
    totals = {"accounts": 0, "scanned": 0, "joined": 0, "left": 0, "unresolved": 0}

    for (platform, account_id), repo in account_manager.all_repos().items():
        totals["accounts"] += 1
        offset = 0
        for _page in range(max(1, int(args.max_pages))):
            try:
                rows = repo.messages.get_recent(
                    chat_type="group",
                    limit=max(1, int(args.page_size)),
                    offset=offset,
                )
            except Exception:
                break
            if not rows:
                break
            messages = [repo.messages.to_dict(row) for row in rows]
            stats = service.apply_messages(
                messages,
                platform=platform,
                account_id=account_id,
            )
            for key in ("scanned", "joined", "left", "unresolved"):
                totals[key] = totals.get(key, 0) + int(stats.get(key, 0) or 0)
            offset += len(rows)
            if len(rows) < int(args.page_size):
                break

    print(json.dumps(totals, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
