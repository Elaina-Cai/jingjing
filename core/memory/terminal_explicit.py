"""Explicit long-term memory backed directly by ``terminal_events``.

The legacy ``memory_projections`` table is frozen in production.  Explicit
``remember`` writes must keep landing in the append-only fact layer, and recall
must be able to read those writes without going through ``MemoryGateway`` (which
is intentionally short-circuited when legacy projections are discarded).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.memory.contracts import MemoryItem
from core.memory.providers import _match
from core.terminal.event_model import Event, EventType
from core.terminal.event_store_db import _event_from_row
from db.meta_models import TerminalEvent


def _scope_values(event: Event) -> tuple[str, ...]:
    return tuple(
        sorted(
            str(scope.value if hasattr(scope, "value") else scope)
            for scope in (event.visibility_scope or ())
        )
    )


def memory_item_from_event(event: Event) -> MemoryItem:
    """Convert one explicit ``memory_write`` event to the unified contract."""
    metadata = dict(event.metadata or {})
    metadata["chat_id"] = event.chat_id
    metadata["chat_type"] = event.chat_type
    metadata.setdefault("source", event.platform)
    metadata.setdefault("memory_type", "semantic")
    metadata["explicit_remember"] = True
    principal = metadata.get("principal")
    if not isinstance(principal, dict):
        principal = {}
    speaker = str(
        metadata.get("speaker")
        or principal.get("display_name")
        or event.actor_person_id
        or ""
    )
    source_ids = metadata.get("source_event_ids")
    if isinstance(source_ids, (list, tuple)) and source_ids:
        sources = tuple(str(item) for item in source_ids if str(item).strip())
    else:
        sources = (event.event_id,)
    try:
        importance = max(0.0, min(1.0, float(metadata.get("importance") or 0.7)))
    except (TypeError, ValueError):
        importance = 0.7
    subject = (str(event.actor_person_id),) if event.actor_person_id else ()
    return MemoryItem(
        memory_id=str(metadata.get("memory_id") or f"proj:{event.event_id}"),
        kind=str(metadata.get("kind") or "personal"),
        content=str(event.content or ""),
        occurred_at=event.occurred_at,
        platform=str(event.platform or ""),
        bot_account_id=str(event.bot_account_id or ""),
        chat_id=str(event.chat_id or ""),
        chat_type=str(event.chat_type or ""),
        speaker=speaker,
        source_event_ids=sources,
        subject_person_ids=subject,
        visibility_scope=_scope_values(event),
        confidence=max(0.45, min(0.95, 0.55 + importance * 0.35)),
        importance=importance,
        metadata=metadata,
    )


def _principal_parts(principal: Any) -> tuple[str, str, str, str, bool, bool] | None:
    platform = str(getattr(principal, "platform", "") or "").strip()
    account_id = str(getattr(principal, "bot_account_id", "") or "").strip()
    person_id = str(getattr(principal, "person_id", "") or "").strip()
    raw_chat_type = getattr(principal, "chat_type", "")
    chat_type = str(getattr(raw_chat_type, "value", raw_chat_type) or "").strip().lower()
    is_group = bool(getattr(principal, "is_group", False)) or chat_type == "group"
    chat_id = str(getattr(principal, "chat_id", "") or "").strip()
    if is_group and not chat_id:
        chat_id = str(getattr(principal, "group_id", "") or "").strip()
    if not all((platform, account_id, chat_id, person_id)):
        return None
    return platform, account_id, chat_id, person_id, is_group, bool(getattr(principal, "is_owner", False))


def event_visible_to_principal(event: Event, principal: Any) -> bool:
    """Apply explicit-memory visibility; unknown or mixed scopes fail closed."""
    parts = _principal_parts(principal)
    if parts is None:
        return False
    platform, account_id, chat_id, person_id, is_group, is_owner = parts
    if (
        str(event.platform or "") != platform
        or str(event.bot_account_id or "") != account_id
        or str(event.chat_id or "") != chat_id
    ):
        return False
    scopes = set(_scope_values(event))
    if not scopes:
        return False
    if "owner_only" in scopes:
        return is_owner
    if "group_members" in scopes:
        return is_group and str(event.chat_id or "") == chat_id
    if "issuer_only" in scopes or "self" in scopes:
        return str(event.actor_person_id or "") == person_id or (is_owner and not is_group)
    if scopes == {"public"}:
        return True
    return False


class TerminalExplicitMemoryProvider:
    """Bounded, read-only recall for explicit ``memory_write`` events."""

    kind = "personal"
    SCAN_LIMIT = 500

    def __init__(self, engine: Any = None, *, scan_limit: int = SCAN_LIMIT):
        self._engine = engine
        try:
            value = int(scan_limit or self.SCAN_LIMIT)
        except (TypeError, ValueError):
            value = self.SCAN_LIMIT
        self.scan_limit = max(1, min(self.SCAN_LIMIT, value))

    def _resolved_engine(self) -> Any:
        engine = self._engine
        if engine is None:
            from core.terminal.store_port import get_meta_repo

            engine = getattr(get_meta_repo(), "engine", None)
        materialize = getattr(engine, "materialize", None)
        return materialize() if callable(materialize) else engine

    def _select(self, principal: Any):
        parts = _principal_parts(principal)
        if parts is None:
            return None
        platform, account_id, chat_id, _person_id, _is_group, _is_owner = parts
        return (
            select(TerminalEvent)
            .where(
                TerminalEvent.event_type == EventType.MEMORY_WRITE.value,
                TerminalEvent.platform == platform,
                TerminalEvent.bot_account_id == account_id,
                TerminalEvent.chat_id == chat_id,
            )
            .order_by(TerminalEvent.sequence.desc(), TerminalEvent.id.desc())
            .limit(self.scan_limit)
        )

    def recall(
        self,
        principal: Any,
        query: str = "",
        *,
        limit: int = 8,
        context: Any = None,
    ) -> list[MemoryItem]:
        statement = self._select(principal)
        if statement is None:
            return []
        try:
            max_items = max(1, int(limit or 1))
        except (TypeError, ValueError):
            max_items = 1
        engine = self._resolved_engine()
        if engine is None:
            return []
        hits: list[MemoryItem] = []
        with Session(engine) as session:
            for row in session.execute(statement).scalars():
                event = _event_from_row(row)
                if event.event_type != EventType.MEMORY_WRITE:
                    continue
                if not event_visible_to_principal(event, principal):
                    continue
                if query and not _match(query, str(event.content or "")):
                    continue
                hits.append(memory_item_from_event(event))
                if len(hits) >= max_items:
                    break
        return hits


__all__ = [
    "TerminalExplicitMemoryProvider",
    "event_visible_to_principal",
    "memory_item_from_event",
]
