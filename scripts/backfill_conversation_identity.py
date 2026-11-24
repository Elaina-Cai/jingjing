"""Backfill historical conversation identity (P2).

Usage examples::

    # dry run against the live meta database
    python scripts/backfill_conversation_identity.py --dry-run

    # real run with message-content sender matching
    python scripts/backfill_conversation_identity.py

    # legacy database, private-chat fallback only
    python scripts/backfill_conversation_identity.py ^
        --db data/yinyue.db --no-sender-match

The script is idempotent: only NULL identity columns are written, and the
resumable ``--after-id`` / reported ``next_after_id`` pair supports batched
runs on very large tables.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine

from core.identity import ConversationIdentityBackfiller
from db.migrations import run_meta_migrations
from db.repo_memory import ConversationRepository, SessionRepository


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/yinyue_meta.db",
                        help="SQLite database containing conversations")
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument("--limit", type=int, default=0,
                        help="max rows to scan in this run (0 = all)")
    parser.add_argument("--after-id", type=int, default=0,
                        help="resume after a previous run's next_after_id")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-migrations", action="store_true")
    parser.add_argument("--no-sender-match", action="store_true",
                        help="disable account-DB content matching")
    parser.add_argument("--no-private-fallback", action="store_true",
                        help="do not infer private-chat sender from chat_id")
    parser.add_argument("--default-platform", default="",
                        help="fallback platform for ambiguous sessions")
    parser.add_argument("--account-id", default="",
                        help="force one account id for every backfilled row")
    parser.add_argument("--window-seconds", type=int, default=180,
                        help="sender-match time window around created_at")
    return parser


def _make_sender_matcher(window_seconds: int):
    try:
        from core.config.settings import config
        from db.account_db_manager import AccountDBManager
        from db.unified_query import UnifiedQuery
    except Exception as exc:  # pragma: no cover - environment dependent
        print(f"[warn] sender matching disabled: {exc}")
        return None

    manager = AccountDBManager()
    for account in config.all_account_configs():
        try:
            manager.register(account.platform, account.account_id, account.db_path)
        except Exception as exc:
            print(f"[warn] cannot register {account.platform}:{account.account_id}: {exc}")
    if not manager.all_repos():
        print("[warn] no account repositories available; sender matching disabled")
        return None
    query = UnifiedQuery(meta_repo=None, account_db_manager=manager)
    delta = timedelta(seconds=max(1, int(window_seconds or 180)))

    def match(conversation, platform: str, account_id: str, chat_id: str) -> str:
        created = getattr(conversation, "created_at", None)
        content = str(getattr(conversation, "content", "") or "").strip()
        if created is None or not content:
            return ""
        try:
            results = query.search_messages(
                chat_id=chat_id,
                since=created - delta,
                until=created + delta,
                limit=30,
                platform=platform or None,
                account_id=account_id or None,
            )
        except Exception:
            return ""
        for item in results:
            message = item.data
            if int(getattr(message, "is_self", 0) or 0):
                continue
            if str(getattr(message, "content", "") or "").strip() != content:
                continue
            sender = str(getattr(message, "sender_id", "") or "").strip()
            if sender:
                return sender
        return ""

    return match


def _make_account_resolver(forced_account_id: str):
    if forced_account_id:
        return lambda platform, chat_id, metadata: forced_account_id
    try:
        from core.config.settings import config
        accounts = config.all_account_configs()
    except Exception:
        accounts = []

    def resolve(platform: str, chat_id: str, metadata) -> str:
        meta_account = str((metadata or {}).get("account_id") or "").strip()
        if meta_account:
            return meta_account
        candidates = [
            account.account_id
            for account in accounts
            if account.platform == platform and getattr(account, "role", "") == "yinyue"
        ]
        return candidates[0] if len(candidates) == 1 else ""

    return resolve


def main() -> int:
    args = _build_parser().parse_args()
    db_path = Path(args.db)
    if not db_path.is_absolute():
        db_path = ROOT / db_path
    if not db_path.exists():
        print(f"[error] database not found: {db_path}")
        return 1

    if not args.skip_migrations:
        run_meta_migrations(str(db_path))

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        backfiller = ConversationIdentityBackfiller(
            ConversationRepository(engine),
            SessionRepository(engine),
            sender_matcher=None if args.no_sender_match else _make_sender_matcher(
                args.window_seconds,
            ),
            account_resolver=_make_account_resolver(args.account_id),
            default_platform=args.default_platform,
            private_sender_fallback=not args.no_private_fallback,
        )
        report = backfiller.run(
            batch_size=args.batch_size,
            limit=args.limit,
            after_id=args.after_id,
            dry_run=args.dry_run,
        )
    finally:
        engine.dispose()

    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
