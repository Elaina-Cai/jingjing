"""Platform-neutral contracts for per-person observations and profiles.

This module is intentionally dependency-free.  It must not import DB, LLM,
platform adapters or repository code.  The platform-specific parts live in
adapters; this layer only describes *what a person did* and *who may see it*.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping

from core.contracts.events import ChatType


class ObservationKind(StrEnum):
    """What kind of person-related signal an observation carries."""

    IDENTITY = "identity"
    FACT = "fact"
    PREFERENCE = "preference"
    EVENT = "event"
    PLAN = "plan"
    RELATIONSHIP = "relationship"
    BEHAVIOR = "behavior"
    OPINION = "opinion"
    UNKNOWN = "unknown"


class ObservationStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"
    REDACTED = "redacted"


class ObservationVisibility(StrEnum):
    """Where an observation is allowed to travel.

    ``GROUP_SHARED`` is deliberately one-way:
      * it can be used in the group where it happened;
      * it can be injected into the subject person's private chat;
      * it must never leak into another group or another person's private chat.

    ``PRIVATE_ONLY`` never leaves the exact private conversation where it
    happened.  In particular, a private message from A that mentions B is not
    visible to B.
    """

    GROUP_SHARED = "group_shared"
    PRIVATE_ONLY = "private_only"
    OWNER_ONLY = "owner_only"
    SENSITIVE = "sensitive"


class ProfileScopeType(StrEnum):
    GLOBAL = "global"
    GROUP = "group"
    PRIVATE = "private"


@dataclass(frozen=True)
class PersonObservation:
    """One evidence-backed observation about one person.

    ``person_id`` is the subject of the observation.  ``speaker_person_id`` is
    who said/observed it.  Both are canonical ``person_id`` values scoped by
    ``platform + account_id``; no wxid, QQ number or nickname is assumed here.
    """

    observation_id: str
    person_id: str
    platform: str
    account_id: str
    occurred_scope_type: ChatType
    occurred_scope_id: str
    speaker_person_id: str = ""
    mentioned_person_ids: tuple[str, ...] = ()
    source_evidence_id: str = ""
    source_message_id: str = ""
    observed_at: datetime | None = None
    kind: ObservationKind = ObservationKind.UNKNOWN
    content: str = ""
    raw_quote: str = ""
    normalized_key: str = ""
    confidence: float = 0.5
    importance: float = 0.5
    visibility: ObservationVisibility = ObservationVisibility.GROUP_SHARED
    sensitivity: str = "normal"
    status: ObservationStatus = ObservationStatus.ACTIVE
    expires_at: datetime | None = None
    model_version: str = ""
    evidence_ids: tuple[str, ...] = ()
    extra: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def related_person_ids(self) -> tuple[str, ...]:
        """All people involved in the observation, de-duplicated and ordered."""
        values: list[str] = []
        for value in (self.person_id, self.speaker_person_id, *self.mentioned_person_ids):
            value = str(value or "").strip()
            if value and value not in values:
                values.append(value)
        return tuple(values)

    def is_private_scope(self) -> bool:
        return self.occurred_scope_type == ChatType.PRIVATE

    def is_group_scope(self) -> bool:
        return self.occurred_scope_type in (ChatType.GROUP, ChatType.CHANNEL)

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "person_id": self.person_id,
            "platform": self.platform,
            "account_id": self.account_id,
            "occurred_scope_type": self.occurred_scope_type.value,
            "occurred_scope_id": self.occurred_scope_id,
            "speaker_person_id": self.speaker_person_id,
            "mentioned_person_ids": list(self.mentioned_person_ids),
            "source_evidence_id": self.source_evidence_id,
            "source_message_id": self.source_message_id,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "kind": self.kind.value,
            "content": self.content,
            "raw_quote": self.raw_quote,
            "normalized_key": self.normalized_key,
            "confidence": self.confidence,
            "importance": self.importance,
            "visibility": self.visibility.value,
            "sensitivity": self.sensitivity,
            "status": self.status.value,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "model_version": self.model_version,
            "evidence_ids": list(self.evidence_ids),
        }


@dataclass(frozen=True)
class PersonProfileSnapshot:
    """An aggregated, scoped profile for one person."""

    profile_id: str
    person_id: str
    platform: str
    account_id: str
    scope_type: ProfileScopeType
    scope_id: str
    summary: str = ""
    style: str = ""
    topics: tuple[str, ...] = ()
    active_hours: tuple[str, ...] = ()
    emotion_tendency: str = ""
    relationship_tone: str = ""
    group_map: Mapping[str, str] = field(default_factory=dict, compare=False, repr=False)
    confidence: float = 0.5
    observation_count: int = 0
    version: int = 1
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "person_id": self.person_id,
            "platform": self.platform,
            "account_id": self.account_id,
            "scope_type": self.scope_type.value,
            "scope_id": self.scope_id,
            "summary": self.summary,
            "style": self.style,
            "topics": list(self.topics),
            "active_hours": list(self.active_hours),
            "emotion_tendency": self.emotion_tendency,
            "relationship_tone": self.relationship_tone,
            "group_map": dict(self.group_map),
            "confidence": self.confidence,
            "observation_count": self.observation_count,
            "version": self.version,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


@dataclass(frozen=True)
class PersonContextRequest:
    """A read-only request for person context in one concrete conversation."""

    platform: str
    account_id: str
    chat_type: ChatType
    chat_id: str
    target_person_id: str
    viewer_person_id: str = ""
    viewer_is_owner: bool = False
    query: str = ""
    max_observations: int = 12


@dataclass(frozen=True)
class PersonContext:
    """Result of a person-context lookup, ready for prompt assembly."""

    request: PersonContextRequest
    profile: PersonProfileSnapshot | None = None
    observations: tuple[PersonObservation, ...] = ()
    prompt_block: str = ""
    source_evidence_ids: tuple[str, ...] = ()
