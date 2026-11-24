# -*- coding: utf-8 -*-
"""Read-only Authority coverage audit.

Phase 5 Track A made deterministic Authority the only identity path.  This
script answers the operational question that removal depends on:

    "For the configured owner accounts and for recent real inbound messages,
     can the deterministic IdentityService actually resolve the expected
     identities, and which senders fall back to UNKNOWN?"

It is intentionally read-only:

* no repository method is called that writes;
* account DBs are opened with ``mode=ro``;
* no event, memory projection or message row is created or updated.

Usage::

    python scripts/authority_coverage_audit.py
    python scripts/authority_coverage_audit.py --account wxid_xxx --limit 5000

Exit code is always ``0`` unless the audit itself crashes.  A low coverage
number is a data problem to investigate in the Web UI / backfill, not an
instruction to add an owner-id fallback.
"""
from __future__ import annotations

import argparse
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config.settings import config
from core.terminal.adapters import build_identity_service_from_bootstrap
from core.terminal.principal import RelationLevel


def _configured_owner_ids(account_config: Any) -> list[str]:
    owner_ids = list(getattr(account_config, "owner_wxids", []) or [])
    if not owner_ids and str(getattr(account_config, "platform", "")) == "wechat":
        owner_ids = list(config.wechat.get_owner_wxids())
    return [str(value).strip() for value in owner_ids if str(value).strip()]


def _account_db_path(account_config: Any) -> Path | None:
    raw = str(getattr(account_config, "db_path", "") or "").strip()
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_file() else None


def _audit_configured_owners(service: Any) -> None:
    print("\n=== configured owner resolution ===")
    for account in config.all_account_configs():
        platform = str(getattr(account, "platform", "") or "")
        account_id = str(getattr(account, "account_id", "") or "")
        owner_ids = _configured_owner_ids(account)
        if not owner_ids:
            continue
        resolved = []
        missed = []
        for raw_id in owner_ids:
            principal = service.resolve_principal(
                platform=platform,
                bot_account_id=account_id,
                chat_id=raw_id,
                chat_type="private",
                sender_id=raw_id,
                sender_name="",
            )
            if principal.relation_to_bot == RelationLevel.OWNER:
                resolved.append(raw_id)
            else:
                missed.append(raw_id)
        status = "OK" if not missed else "MISS"
        print(
            f"[{status}] {platform}/{account_id}: "
            f"configured={len(owner_ids)} resolved={len(resolved)} missed={missed}"
        )


def _incoming_groups(
    path: Path, account_id: str
) -> list[tuple[str, str, str, str, str, int]]:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = {
            str(row[0])
            for row in con.execute(
                "select name from sqlite_master where type='table'"
            )
        }
        if "messages" not in tables:
            return []
        columns = {
            str(row[1]) for row in con.execute('pragma table_info("messages")')
        }
        if "sender_id" not in columns:
            return []
        where = "coalesce(sender_id,'') not in ('','self','unknown')"
        if "is_self" in columns:
            where += " and coalesce(is_self,0)=0"
        where += " and sender_id <> ?"
        source_expr = "coalesce(source,'wechat')" if "source" in columns else "'wechat'"
        rows = con.execute(
            f"""
            select
                coalesce(chat_type,'private') as chat_type,
                coalesce(chat_id,'') as chat_id,
                coalesce(sender_id,'') as sender_id,
                coalesce(sender_name,'') as sender_name,
                {source_expr} as source,
                count(*) as n
            from messages
            where {where}
            group by chat_type, chat_id, sender_id
            order by n desc
            """,
            (account_id,),
        ).fetchall()
        return [
            (
                str(chat_type or "private"),
                str(chat_id or ""),
                str(sender_id or ""),
                str(sender_name or ""),
                str(source or "wechat"),
                int(n or 0),
            )
            for chat_type, chat_id, sender_id, sender_name, source, n in rows
        ]
    finally:
        con.close()


def _audit_incoming_messages(service: Any, *, account_id: str, path: Path) -> None:
    groups = _incoming_groups(path, account_id)
    if not groups:
        print(f"\n--- {account_id}: no readable incoming messages ---")
        return
    relations: Counter[str] = Counter()
    message_rows: Counter[str] = Counter()
    low_authority: list[tuple[str, str, str, str, int]] = []
    for chat_type, chat_id, sender_id, sender_name, source, count in groups:
        is_group = chat_type == "group"
        principal = service.resolve_principal(
            platform=source,
            bot_account_id=account_id,
            chat_id=chat_id,
            chat_type="group" if is_group else "private",
            sender_id=sender_id,
            sender_name=sender_name,
            group_id=chat_id if is_group else "",
        )
        relation = principal.relation_to_bot.value
        relations[relation] += 1
        message_rows[relation] += count
        if principal.relation_to_bot in (RelationLevel.UNKNOWN, RelationLevel.STRANGER):
            low_authority.append(
                (chat_type, chat_id, sender_id, sender_name, count)
            )
    print(
        f"\n--- {account_id}: incoming distinct_senders={len(groups)} "
        f"relations={dict(relations)} message_rows={dict(message_rows)} ---"
    )
    for chat_type, chat_id, sender_id, sender_name, count in low_authority[:20]:
        print(
            "  low_authority "
            f"chat_type={chat_type} chat_id={chat_id!r} "
            f"sender_id={sender_id!r} sender_name={sender_name!r} rows={count}"
        )
    if len(low_authority) > 20:
        print(f"  ... {len(low_authority) - 20} more low-authority groups")


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Authority coverage audit")
    parser.add_argument(
        "--account",
        default="",
        help="Only audit this bot account id (default: every yinyue account).",
    )
    parser.add_argument(
        "--include-owner-accounts",
        action="store_true",
        help="Also scan DBs of accounts whose role is not yinyue.",
    )
    args = parser.parse_args()

    service = build_identity_service_from_bootstrap()
    person_identities = getattr(service, "_person_identities", {})
    relationships = getattr(service, "_relationships", {})
    scoped = getattr(service, "_person_identity_scopes", {})
    memberships = getattr(service, "_group_memberships", {})
    print(
        "identity service loaded: "
        f"person_identities={len(person_identities)} "
        f"scoped_identities={len(scoped)} "
        f"relationships={len(relationships)} "
        f"memberships={len(memberships)}"
    )
    print("configured owner accounts:", {
        platform: len(owner_ids)
        for platform, owner_ids in config.get_all_owner_accounts().items()
    })

    _audit_configured_owners(service)

    for account in config.all_account_configs():
        account_id = str(getattr(account, "account_id", "") or "")
        if args.account and account_id != args.account:
            continue
        role = str(getattr(account, "role", "") or "")
        if role != "yinyue" and not args.include_owner_accounts:
            continue
        path = _account_db_path(account)
        if path is None:
            continue
        _audit_incoming_messages(service, account_id=account_id, path=path)

    print(
        "\nNote: UNKNOWN is the safe, intended result for unresolved senders; "
        "the correct follow-up is identity backfill, never an owner-id fallback."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
