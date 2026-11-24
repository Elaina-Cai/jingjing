"""Evidence indexing service (P2).

``db/repo_evidence`` owns storage.  This service turns legacy messages or
canonical ``MessageEvent`` objects into evidence rows and resolves the sender
to a person when possible.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Iterable, Mapping, Sequence

from core.contracts import MessageEvent
from core.contracts.legacy import legacy_to_message_event
from safelog import warn

log = logging.getLogger(__name__)

_BOT_MESSAGE_SOURCES = frozenset({"yinyue_own", "yinyue_proxy"})


class EvidenceIndexer:
    def __init__(self, evidence: Any, people: Any = None):
        self._evidence = evidence
        self._people = people

    # ------------------------------------------------------------- single event
    def index_event(
        self,
        event: MessageEvent,
        *,
        role: str = "user",
        person_id: str = "",
        source: str = "message",
        session_id: int | None = None,
        topic_id: int | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> Any:
        return self._evidence.upsert(
            platform=event.platform,
            account_id=event.account_id,
            chat_id=event.chat.chat_id,
            message_id=event.message_id,
            person_id=person_id or None,
            role=role,
            source=source,
            kind=event.kind.value,
            content=event.content,
            session_id=session_id,
            topic_id=topic_id,
            created_at=event.created_at,
            metadata=metadata,
        )

    # ------------------------------------------------------------- send-log path
    def index_send_log(self, send_log: Any, *, account_id: str = "") -> Any:
        """Index a confirmed outbound send as assistant evidence.

        This is the direct mapping from the send audit trail to a platform
        message id, independent of the later message-sync projection.
        """
        confirmed = getattr(send_log, "confirmed_msg_id", None)
        if not confirmed:
            return None
        platform = str(getattr(send_log, "platform", "") or "unknown")
        chat_id = str(getattr(send_log, "chat_id", "") or "")
        account = account_id or str(getattr(send_log, "account", "") or "")
        content = str(getattr(send_log, "content", "") or "")
        kind = str(getattr(send_log, "msg_type", "") or "text")
        created_at = (
            getattr(send_log, "confirmed_at", None)
            or getattr(send_log, "sent_at", None)
            or getattr(send_log, "created_at", None)
        )
        metadata = {
            "send_log_id": getattr(send_log, "id", None),
            "trigger": getattr(send_log, "trigger", ""),
            "trigger_msg_id": getattr(send_log, "trigger_msg_id", None),
            "source": getattr(send_log, "source", ""),
        }
        return self._evidence.upsert(
            platform=platform,
            account_id=account,
            chat_id=chat_id,
            message_id=str(confirmed),
            person_id=None,
            role="assistant",
            source="send_log",
            kind=kind,
            content=content,
            created_at=created_at,
            metadata=metadata,
        )

    # ------------------------------------------------------------ legacy batch
    @staticmethod
    def role_for_message(message: Mapping[str, Any]) -> str:
        """Return ``user`` or ``assistant`` for a legacy message dict.

        ``is_self`` alone is not enough: the owner may use the same WeChat
        account directly.  Only messages claimed by a silver-moon send log are
        bot output; other self messages stay human.
        """
        try:
            is_self = bool(int(message.get("is_self") or 0))
        except (TypeError, ValueError):
            warn("core/evidence/indexer.py:105 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
            is_self = False
        if not is_self:
            return "user"
        source = str(message.get("msg_source") or "").strip().lower()
        if source in _BOT_MESSAGE_SOURCES:
            return "assistant"
        return "user"

    def _resolve_person_id(
        self,
        message: Mapping[str, Any],
        *,
        platform: str,
        account_id: str,
    ) -> str:
        if self._people is None:
            return ""
        try:
            person = self._people.resolve_message(
                dict(message), platform=platform, account_id=account_id,
            )
            return getattr(person, "person_id", "") or ""
        except Exception as exc:  # never block evidence indexing on identity
            log.debug("evidence_identity_resolve_failed: %s", exc)
            return ""

    def index_legacy_messages(
        self,
        messages: Sequence[Mapping[str, Any]] | Iterable[Mapping[str, Any]],
        *,
        platform: str = "",
        account_id: str = "",
        role_for: Callable[[Mapping[str, Any]], str] | None = None,
    ) -> int:
        written = 0
        for message in messages:
            if not isinstance(message, Mapping):
                continue
            event = legacy_to_message_event(
                message,
                platform=platform or str(message.get("source") or ""),
                account_id=account_id or str(message.get("account_id") or ""),
            )
            if not event.message_id:
                continue
            role = (role_for or self.role_for_message)(message)
            person_id = ""
            if role == "user":
                person_id = self._resolve_person_id(
                    message, platform=event.platform,
                    account_id=event.account_id,
                )
            try:
                self.index_event(
                    event,
                    role=role,
                    person_id=person_id,
                    source="message",
                    topic_id=message.get("topic_id"),
                )
                written += 1
            except Exception as exc:
                log.warning(
                    "evidence_index_failed message_id=%s error=%s",
                    event.message_id, str(exc)[:120],
                )
        return written

    # ---------------------------------------------------------- cursor worker
    def index_pending_for_account(
        self,
        account_repo: Any,
        *,
        platform: str,
        account_id: str,
        cursors: Any,
        batch_size: int = 500,
        max_batches: int = 10,
        role_for: Callable[[Mapping[str, Any]], str] | None = None,
    ) -> int:
        """Index messages after the persisted per-account cursor.

        The worker is resumable: each fully indexed page is committed to the
        cursor table before the next one is read.  A crash inside a page only
        causes that page to be read again, and ``message_evidence.upsert``
        makes the replay idempotent.  ``max_batches`` bounds one pass so a
        large first-time backfill cannot block the sync pipeline.

        Returns the number of evidence rows written during this call.
        """
        if cursors is None:
            return 0
        batch_size = max(1, int(batch_size or 1))
        max_batches = max(1, int(max_batches or 1))

        state = cursors.get(platform, account_id)
        after_synced_at = getattr(state, "last_synced_at", None) if state else None
        after_id = int(getattr(state, "last_message_id", 0) or 0) if state else 0
        base_total = int(getattr(state, "indexed_total", 0) or 0) if state else 0
        total_written = 0

        for _ in range(max_batches):
            rows = account_repo.messages.get_synced_after(
                after_synced_at=after_synced_at,
                after_id=after_id,
                limit=batch_size,
            )
            if not rows:
                break
            payload = [account_repo.messages.to_dict(row) for row in rows]
            written = self.index_legacy_messages(
                payload,
                platform=platform,
                account_id=account_id,
                role_for=role_for,
            )
            total_written += written

            last = rows[-1]
            last_payload = payload[-1] if payload else {}
            last_synced_at = getattr(last, "synced_at", None)
            last_row_id = getattr(last, "id", None)
            if isinstance(last_payload, Mapping):
                if last_synced_at is None:
                    last_synced_at = last_payload.get("synced_at")
                if last_row_id is None:
                    last_row_id = last_payload.get("id")
            if last_synced_at is None:
                # The repository contract filters NULL synced_at; this guard
                # keeps a custom repository from stalling the cursor forever.
                break
            after_synced_at = last_synced_at
            after_id = int(last_row_id or 0)
            state = cursors.save(
                platform=platform,
                account_id=account_id,
                last_synced_at=after_synced_at,
                last_message_id=after_id,
                indexed_total=base_total + total_written,
            )
            if len(rows) < batch_size:
                break

        return total_written

    # ---------------------------------------------------------- account helper
    def index_recent_for_account(
        self,
        account_repo: Any,
        *,
        platform: str,
        account_id: str,
        synced_since: Any = None,
        limit: int = 5000,
        page_size: int = 1000,
    ) -> int:
        """Index messages newly synced into one account repository.

        Pages through the sync window so a large backfill cannot silently
        leave messages unindexed after the first page.
        """
        written = 0
        offset = 0
        page_size = max(1, min(page_size, limit))
        try:
            while True:
                rows = account_repo.messages.get_recent(
                    synced_since=synced_since,
                    limit=page_size,
                    offset=offset,
                )
                if not rows:
                    break
                payload = [account_repo.messages.to_dict(row) for row in rows]
                written += self.index_legacy_messages(
                    payload,
                    platform=platform,
                    account_id=account_id,
                )
                if len(rows) < page_size:
                    break
                offset += len(rows)
                if offset >= limit:
                    break
        except Exception as exc:
            log.warning(
                "evidence_recent_query_failed platform=%s account_id=%s error=%s",
                platform, account_id, str(exc)[:120],
            )
        return written
