"""Shadow backfill: WeChat account messages -> person observations.

This file is the WeChat adapter. It builds canonical ``MessageEvent`` objects
from an account database and hands them to the platform-neutral person memory
service. No core module imports WeChat or SQLAlchemy from this script.

Examples::

    python scripts/backfill_person_observations.py --dry-run --limit 500
    python scripts/backfill_person_observations.py --limit 500
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.contracts.events import ChatType, MessageEvent, MessageKind
from core.identity.platforms import chat_type_for
from core.people.aggregator import RuleBasedProfileAggregator
from core.people.extractor import RuleBasedObservationExtractor
from core.people.service import PersonMemoryService
from db.meta_models import init_meta_db
from db.repo_people import PeopleRepository
from db.repo_person_memory import PersonMemoryRepository

_KIND_MAP = {
    "text": MessageKind.TEXT,
    "image": MessageKind.IMAGE,
    "sticker": MessageKind.STICKER,
    "voice": MessageKind.VOICE,
    "video": MessageKind.VIDEO,
    "file": MessageKind.FILE,
    "link": MessageKind.LINK,
    "location": MessageKind.LOCATION,
    "contact_card": MessageKind.CONTACT_CARD,
    "quote_reply": MessageKind.QUOTE_REPLY,
    "forward": MessageKind.FORWARD,
    "system": MessageKind.SYSTEM,
    "pat": MessageKind.PAT,
    "mini_program": MessageKind.MINI_PROGRAM,
    "transfer": MessageKind.TRANSFER,
}


def _kind(value):
    return _KIND_MAP.get(str(value or "").strip().lower(), MessageKind.UNKNOWN)


def _parse_dt(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace(" ", "T"))
    except ValueError:
        return None


def _chat_type(platform, value, chat_id):
    return chat_type_for(platform, value, chat_id)


def _discover_account_db(account_db, account_id):
    if account_db:
        path = Path(account_db)
        if not path.is_absolute():
            path = ROOT / path
        return path if path.exists() else None
    root = ROOT / "data" / "platforms" / "wechat"
    candidates = sorted(root.glob("*/account.db"))
    if account_id:
        candidates = [p for p in candidates if p.parent.name == account_id]
    return candidates[0] if candidates else None


def _build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--meta-db", default="data/yinyue_meta.db")
    parser.add_argument("--account-db", default="")
    parser.add_argument("--account-id", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--after-id", type=int, default=0)
    parser.add_argument("--include-self", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main():
    args = _build_parser().parse_args()
    meta_db = Path(args.meta_db)
    if not meta_db.is_absolute():
        meta_db = ROOT / meta_db
    if not meta_db.exists():
        print("[error] meta database not found: %s" % meta_db)
        return 1

    account_db = _discover_account_db(args.account_db, args.account_id)
    if account_db is None:
        print("[error] no WeChat account.db found")
        return 1
    account_id = args.account_id or account_db.parent.name

    sql = (
        "SELECT id, msg_id, chat_id, chat_type, sender_id, sender_name, content, "
        "msg_type_name, created_at FROM messages WHERE id > ? "
    )
    params = [args.after_id]
    if not args.include_self:
        sql += "AND COALESCE(is_self, 0) = 0 "
    sql += "ORDER BY id ASC LIMIT ?"
    params.append(args.limit if args.limit and args.limit > 0 else 1000000)

    with sqlite3.connect(str(account_db)) as con:
        rows = con.execute(sql, params).fetchall()

    extractor = RuleBasedObservationExtractor()
    matched = 0
    scanned = 0
    skipped_no_sender = 0
    skipped_empty = 0
    by_kind = {}

    engine = init_meta_db(str(meta_db))
    try:
        people = PeopleRepository(engine)
        service = None
        if not args.dry_run:
            repo = PersonMemoryRepository(engine)
            service = PersonMemoryService(
                repo, repo, extractor, RuleBasedProfileAggregator(),
            )

        next_after_id = args.after_id
        for row in rows:
            (row_id, msg_id, chat_id, chat_type, sender_id, sender_name,
             content, msg_type_name, created_at) = row
            next_after_id = int(row_id)
            scanned += 1
            sender_id = str(sender_id or "").strip()
            if not sender_id:
                skipped_no_sender += 1
                continue
            content = str(content or "").strip()
            if not content:
                skipped_empty += 1
                continue

            person = people.resolve_message(
                {
                    "sender_id": sender_id,
                    "sender_name": sender_name or "",
                    "chat_id": chat_id,
                    "chat_type": "group" if _chat_type("wechat", chat_type, chat_id) == ChatType.GROUP else "private",
                    "account_id": account_id,
                    "source": "wechat",
                },
                platform="wechat",
                account_id=account_id,
            )
            if person is None:
                skipped_no_sender += 1
                continue

            event = MessageEvent.new(
                platform="wechat",
                account_id=account_id,
                chat_id=str(chat_id or ""),
                chat_type=_chat_type("wechat", chat_type, chat_id),
                message_id=str(msg_id),
                sender_id=sender_id,
                sender_name=str(sender_name or ""),
                kind=_kind(msg_type_name),
                content=content,
                created_at=_parse_dt(created_at),
            )
            extracted = extractor.extract(
                event,
                subject_person_id=person.person_id,
                speaker_person_id=person.person_id,
                evidence_id="",
            )
            if not extracted:
                continue
            matched += len(extracted)
            for observation in extracted:
                by_kind[observation.kind.value] = by_kind.get(observation.kind.value, 0) + 1
            if service is not None:
                service.ingest_event(
                    event,
                    subject_person_id=person.person_id,
                    speaker_person_id=person.person_id,
                    evidence_id="",
                )

        print(json.dumps({
            "dry_run": bool(args.dry_run),
            "account_db": str(account_db),
            "account_id": account_id,
            "scanned": scanned,
            "matched_observations": matched,
            "skipped_no_sender": skipped_no_sender,
            "skipped_empty": skipped_empty,
            "by_kind": by_kind,
            "next_after_id": next_after_id,
        }, ensure_ascii=False, indent=2))
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
