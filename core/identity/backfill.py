"""Historical conversation identity backfill (P2).

New conversation rows carry ``platform / account_id / chat_id / sender_id``
directly.  Rows written before that change only have ``session_id``; the
session metadata already contains the platform and chat id for platform
conversations, and private-chat user rows can recover the sender from the chat
id.  Group senders need an explicit message lookup, which callers can inject as
``sender_matcher``.

The service is storage-agnostic: repositories are duck-typed, so the same
logic runs against the meta database and the legacy database.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from core.contracts.events import ChatType
from core.identity.platforms import chat_type_for
from safelog import warn

log = logging.getLogger(__name__)

SenderMatcher = Callable[[Any, str, str, str], str]
AccountResolver = Callable[[str, str, Mapping[str, Any]], str]

_PLATFORM_SESSION_SOURCES = frozenset({"", "web", "unknown", "system"})


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _session_metadata(session: Any) -> dict[str, Any]:
    raw = getattr(session, "metadata_", None)
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            warn("core/identity/backfill.py:45 json.JSONDecodeError", err=repr(__import__("sys").exc_info()[1]))
            return {}
        return dict(data) if isinstance(data, Mapping) else {}
    return {}


def _looks_like_group(platform: str, chat_id: str, metadata: Mapping[str, Any]) -> bool:
    resolved = chat_type_for(
        platform,
        str(metadata.get("chat_type") or ""),
        chat_id,
    )
    return resolved in (ChatType.GROUP, ChatType.CHANNEL)


@dataclass
class BackfillReport:
    scanned: int = 0
    updated: int = 0
    skipped: int = 0
    unresolved: int = 0
    sender_resolved: int = 0
    dry_run: bool = False
    next_after_id: int = 0
    remaining: int = 0
    fields: dict[str, int] = field(default_factory=dict)
    samples: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "updated": self.updated,
            "skipped": self.skipped,
            "unresolved": self.unresolved,
            "sender_resolved": self.sender_resolved,
            "dry_run": self.dry_run,
            "next_after_id": self.next_after_id,
            "remaining": self.remaining,
            "fields": dict(sorted(self.fields.items())),
            "samples": list(self.samples),
        }


class ConversationIdentityBackfiller:
    def __init__(
        self,
        conversations: Any,
        sessions: Any,
        *,
        sender_matcher: SenderMatcher | None = None,
        account_resolver: AccountResolver | None = None,
        default_platform: str = "",
        private_sender_fallback: bool = True,
        max_samples: int = 5,
    ):
        self._conversations = conversations
        self._sessions = sessions
        self._sender_matcher = sender_matcher
        self._account_resolver = account_resolver
        self._default_platform = default_platform
        self._private_sender_fallback = private_sender_fallback
        self._max_samples = max(0, int(max_samples or 0))

    # ------------------------------------------------------------------ public
    def run(
        self,
        *,
        batch_size: int = 200,
        limit: int = 0,
        after_id: int = 0,
        dry_run: bool = False,
    ) -> BackfillReport:
        batch_size = max(1, int(batch_size or 1))
        limit = max(0, int(limit or 0))
        cursor = max(0, int(after_id or 0))
        report = BackfillReport(dry_run=dry_run)

        while True:
            if limit and report.scanned >= limit:
                break
            page_limit = min(batch_size, limit - report.scanned) if limit else batch_size
            rows = self._conversations.list_missing_identity(
                after_id=cursor, limit=page_limit,
            )
            if not rows:
                break

            for row in rows:
                row_id = int(getattr(row, "id", 0) or 0)
                cursor = max(cursor, row_id)
                report.scanned += 1
                self._backfill_one(row, report, dry_run=dry_run)

            if len(rows) < page_limit:
                break

        report.next_after_id = cursor
        counter = getattr(self._conversations, "count_missing_identity", None)
        if callable(counter):
            try:
                report.remaining = int(counter())
            except Exception:
                warn("core/identity/backfill.py:146 Exception", err=repr(__import__("sys").exc_info()[1]))
                report.remaining = 0
        if report.updated:
            log.info(
                "conversation_identity_backfilled scanned=%d updated=%d "
                "unresolved=%d fields=%s dry_run=%s",
                report.scanned, report.updated, report.unresolved,
                report.fields, dry_run,
            )
        return report

    # ----------------------------------------------------------------- private
    def _backfill_one(
        self,
        row: Any,
        report: BackfillReport,
        *,
        dry_run: bool,
    ) -> None:
        session_id = getattr(row, "session_id", None)
        session = None
        if session_id:
            try:
                session = self._sessions.get(int(session_id))
            except Exception:
                warn("core/identity/backfill.py:170 Exception", err=repr(__import__("sys").exc_info()[1]))
                session = None
        metadata = _session_metadata(session)

        platform = (
            _clean(getattr(row, "platform", None))
            or _clean(metadata.get("platform"))
        )
        if not platform:
            source = _clean(getattr(session, "source", ""))
            if source not in _PLATFORM_SESSION_SOURCES:
                platform = source
        if not platform and self._default_platform:
            platform = _clean(self._default_platform)

        chat_id = (
            _clean(getattr(row, "chat_id", None))
            or _clean(metadata.get("chat_id"))
            or _clean(metadata.get("talker"))
        )
        if not platform or not chat_id:
            report.skipped += 1
            return

        account_id = (
            _clean(getattr(row, "account_id", None))
            or _clean(metadata.get("account_id"))
        )
        if not account_id and self._account_resolver is not None:
            try:
                account_id = _clean(
                    self._account_resolver(platform, chat_id, metadata)
                )
            except Exception as exc:
                log.debug("account_resolve_failed: %s", exc)

        updates: dict[str, str] = {}
        if not _clean(getattr(row, "platform", None)):
            updates["platform"] = platform
        if not _clean(getattr(row, "account_id", None)) and account_id:
            updates["account_id"] = account_id
        if not _clean(getattr(row, "chat_id", None)):
            updates["chat_id"] = chat_id

        role = _clean(getattr(row, "role", "")) or "user"
        if role == "user" and not _clean(getattr(row, "sender_id", None)):
            sender_id = self._resolve_sender(
                row, platform=platform, account_id=account_id,
                chat_id=chat_id, metadata=metadata,
            )
            if sender_id:
                updates["sender_id"] = sender_id
                report.sender_resolved += 1

        if not updates:
            report.unresolved += 1
            return

        if not dry_run:
            try:
                self._conversations.apply_identity(
                    int(getattr(row, "id", 0) or 0),
                    platform=updates.get("platform"),
                    account_id=updates.get("account_id"),
                    chat_id=updates.get("chat_id"),
                    sender_id=updates.get("sender_id"),
                )
            except Exception as exc:
                log.warning(
                    "conversation_identity_write_failed conversation_id=%s "
                    "error=%s",
                    getattr(row, "id", None), str(exc)[:120],
                )
                report.unresolved += 1
                return

        report.updated += 1
        for key in updates:
            report.fields[key] = report.fields.get(key, 0) + 1
        if len(report.samples) < self._max_samples:
            report.samples.append({
                "id": getattr(row, "id", None),
                "role": role,
                "platform": updates.get("platform", getattr(row, "platform", None)),
                "chat_id": updates.get("chat_id", getattr(row, "chat_id", None)),
                "account_id": updates.get(
                    "account_id", getattr(row, "account_id", None),
                ),
                "sender_id": updates.get(
                    "sender_id", getattr(row, "sender_id", None),
                ),
            })

    def _resolve_sender(
        self,
        row: Any,
        *,
        platform: str,
        account_id: str,
        chat_id: str,
        metadata: Mapping[str, Any],
    ) -> str:
        if self._sender_matcher is not None:
            try:
                found = _clean(
                    self._sender_matcher(row, platform, account_id, chat_id)
                )
            except Exception as exc:
                log.debug("sender_matcher_failed: %s", exc)
                found = ""
            if found:
                return found
        if self._private_sender_fallback and not _looks_like_group(platform, chat_id, metadata):
            return chat_id
        return ""
