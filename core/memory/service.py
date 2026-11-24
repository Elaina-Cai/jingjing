"""MemoryService: thin application facade over MemoryHub.

Web/admin/scripts should depend on this instead of individual repositories.
"""

from __future__ import annotations

from typing import Any

from core.memory.contracts import MemoryItem
from core.memory.hub import MemoryHub, get_memory_hub
from core.memory.recall_router import recall_items_for_context
from safelog import warn


class MemoryService:
    def __init__(self, hub: MemoryHub | None = None):
        self._hub = hub

    @property
    def hub(self) -> MemoryHub:
        return self._hub or get_memory_hub()

    def search(
        self,
        context: Any,
        query: str = "",
        *,
        kinds: list[str] | tuple[str, ...] | None = None,
        limit: int = 8,
    ) -> list[MemoryItem]:
        return recall_items_for_context(
            context,
            query,
            kinds=kinds,
            limit=limit,
            hub=self.hub,
        )

    @staticmethod
    def as_dicts(items: list[MemoryItem]) -> list[dict[str, Any]]:
        return [item.as_dict() for item in items]

    def remember(
        self,
        context: Any,
        content: str,
        *,
        memory_type: str = "semantic",
        importance: float = 0.5,
        source: str = "web_memory_service",
        trusted: bool = False,
    ) -> MemoryItem | None:
        """Write one explicit MEMORY_WRITE event and project it immediately."""
        text = str(content or "").strip()
        if not text or context is None:
            return None
        from core.memory.recall_router import _principal_for_context
        from core.terminal.event_model import Event, EventType
        from core.terminal.event_runtime import default_event_store

        principal = _principal_for_context(context)
        from core.memory.explicit_write import remember_denial
        if not trusted and remember_denial(principal, context=context, memory_type=memory_type):
            return None
        try:
            importance_value = max(0.0, min(1.0, float(importance)))
        except (TypeError, ValueError):
            warn("core/memory/service.py:65 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
            importance_value = 0.5
        extra = getattr(context, "extra", {}) or {}
        chat_type = getattr(principal, "chat_type", "")
        chat_type = getattr(chat_type, "value", chat_type) or ("group" if extra.get("is_group") else "private")
        from agents.tools.memory import _explicit_visibility
        from core.terminal.principal import VisibilityScope

        if trusted:
            scope = VisibilityScope.GROUP_MEMBERS if str(chat_type) == "group" else VisibilityScope.OWNER_ONLY
            visibility = frozenset({scope})
        else:
            visibility = _explicit_visibility(principal)
        platform = str(getattr(principal, "platform", "") or extra.get("platform") or "")
        bot_account_id = str(getattr(principal, "bot_account_id", "") or extra.get("account_id") or "")
        chat_id = str(getattr(principal, "chat_id", "") or extra.get("chat_id") or "")
        actor_person_id = str(getattr(principal, "person_id", "") or extra.get("sender_id") or "")

        event = Event.new(
            event_type=EventType.MEMORY_WRITE,
            platform=platform,
            bot_account_id=bot_account_id,
            chat_id=chat_id,
            chat_type=str(chat_type),
            actor_person_id=actor_person_id,
            content=text,
            visibility_scope=visibility,
            metadata={
                "memory_type": str(memory_type or "semantic"),
                "importance": importance_value,
                "source": str(source or "web_memory_service"),
                "explicit_remember": True,
                "principal": principal.as_dict(),
            },
        )
        store = default_event_store()
        if store is None:
            return None
        from core.terminal.store_port import get_meta_repo
        from db.repo_memory_projections import MemoryProjectionRepository

        from core.memory.explicit_write import commit_explicit
        item = commit_explicit(event, store=store, repository=MemoryProjectionRepository(get_meta_repo().engine))
        return MemoryItem(
            memory_id=item.memory_id,
            kind="personal",
            content=item.content,
            occurred_at=item.occurred_at,
            platform=item.platform,
            bot_account_id=item.bot_account_id,
            chat_id=str(item.metadata.get("chat_id") or ""),
            chat_type=str(item.metadata.get("chat_type") or ""),
            subject_person_ids=item.subject_person_ids,
            speaker=str(item.metadata.get("speaker") or ""),
            source_event_ids=item.source_event_ids,
            visibility_scope=item.visibility_scope,
            confidence=item.confidence,
            importance=float(item.metadata.get("importance") or 0.5),
            metadata=item.metadata,
        )


__all__ = ["MemoryService"]
