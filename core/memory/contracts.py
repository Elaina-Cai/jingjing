"""Unified memory item contract for Phase 5 memory unification.

Physical tables stay separate by access pattern, but every provider returns
this shape so recall, permission checks and provenance are comparable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class MemoryItem:
    memory_id: str
    kind: str
    content: str
    occurred_at: datetime | None = None
    platform: str = ""
    bot_account_id: str = ""
    chat_id: str = ""
    chat_type: str = ""
    speaker: str = ""
    source_event_ids: tuple[str, ...] = ()
    source_message_ids: tuple[str, ...] = ()
    subject_person_ids: tuple[str, ...] = ()
    visibility_scope: tuple[str, ...] = ()
    confidence: float = 0.5
    importance: float = 0.5
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "kind": self.kind,
            "content": self.content,
            "occurred_at": self.occurred_at.isoformat()
            if self.occurred_at
            else "",
            "platform": self.platform,
            "bot_account_id": self.bot_account_id,
            "chat_id": self.chat_id,
            "chat_type": self.chat_type,
            "speaker": self.speaker,
            "source_event_ids": list(self.source_event_ids),
            "source_message_ids": list(self.source_message_ids),
            "subject_person_ids": list(self.subject_person_ids),
            "visibility_scope": list(self.visibility_scope),
            "confidence": self.confidence,
            "importance": self.importance,
            "metadata": dict(self.metadata),
        }


def memory_item_from_hit(hit: Any) -> MemoryItem:
    """Convert a MemoryGateway MemoryHit into the unified contract."""
    return MemoryItem(
        memory_id=str(getattr(hit, "memory_id", "") or ""),
        kind="personal",
        content=str(getattr(hit, "content", "") or ""),
        occurred_at=getattr(hit, "occurred_at", None),
        platform=str(getattr(hit, "platform", "") or ""),
        bot_account_id=str(getattr(hit, "bot_account_id", "") or ""),
        chat_id=str(getattr(hit, "chat_id", "") or ""),
        chat_type=str(getattr(hit, "chat_type", "") or ""),
        speaker=str(getattr(hit, "speaker", "") or ""),
        source_event_ids=tuple(getattr(hit, "source_event_ids", ()) or ()),
        subject_person_ids=tuple(getattr(hit, "subject_person_ids", ()) or ()),
        visibility_scope=tuple(getattr(hit, "visibility_scope", ()) or ()),
        confidence=min(1.0, max(0.0, float(getattr(hit, "score", 0.5) or 0.0))),
        importance=float(getattr(hit, "score", 0.0) or 0.0),
        metadata={
            "gateway_score": float(getattr(hit, "score", 0.0) or 0.0),
            "memory_type": str(getattr(hit, "event_type", "") or "episode"),
            "explicit_remember": bool(getattr(hit, "is_explicit_remember", False)),
            "projection_version": str(getattr(hit, "projection_version", "") or ""),
            "provenance_status": "source_linked" if getattr(hit, "source_event_ids", ()) else "legacy_unverified",
        },
    )


__all__ = ["MemoryItem", "memory_item_from_hit"]
