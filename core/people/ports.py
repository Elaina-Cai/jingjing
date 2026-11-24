"""Ports for the person memory layer.

The services in ``core.people`` depend only on these protocols.  SQLAlchemy,
in-memory fakes, remote stores or future vector stores can implement them
without changing the domain code.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, Sequence

from core.contracts.events import MessageEvent
from core.contracts.person_memory import (
    PersonObservation,
    PersonProfileSnapshot,
    ProfileScopeType,
)


class AsyncObservationExtractor(Protocol):
    """Async extraction port (LLM-backed extractors implement this)."""

    async def extract(
        self,
        event: MessageEvent,
        *,
        subject_person_id: str,
        speaker_person_id: str = "",
        evidence_id: str = "",
    ) -> list[PersonObservation]:
        ...


class TextCompletionPort(Protocol):
    """Minimal text-completion port used by the in-core LLM extractor.

    Infrastructure adapters wrap the concrete LLM client.  The core extractor
    only sees ``complete(system=..., user=...)``.
    """

    async def complete(
        self,
        *,
        system: str,
        user: str,
        agent_name: str = "",
    ) -> str:
        ...


class ObservationRepository(Protocol):
    """Storage for atomic person observations."""

    def save_many(self, observations: Sequence[PersonObservation]) -> int:
        """Upsert observations; returns the number written."""

    def list_for_person(
        self,
        person_id: str,
        *,
        scope: Any = None,
        since: datetime | None = None,
        limit: int = 200,
        include_inactive: bool = False,
    ) -> list[PersonObservation]:
        """Return observations of a person **visible in the caller's scope**.

        ``scope`` is a ``core.people.visibility.VisibleScope`` and is required:
        R-I1/R-I2/R-I3 make "requester + current conversation" the only allowed
        read, so an adapter must return ``[]`` (fail closed) when it is missing
        or unusable instead of falling back to "everything about this person".
        """

    def list_for_projection(
        self,
        person_id: str,
        *,
        platform: str = "",
        account_id: str = "",
        since: datetime | None = None,
        limit: int = 1000,
        include_inactive: bool = False,
    ) -> list[PersonObservation]:
        """Internal read for profile aggregation — **not** a recall API.

        It returns every observation of one person in one tenant because a
        profile snapshot folds all scopes.  Session-facing reads must go through
        :meth:`list_for_person`; keeping the two apart is what stops the
        aggregation path from becoming a second, unfiltered recall path.
        """


class ProfileRepository(Protocol):
    """Storage for aggregated scoped profiles."""

    def get_profile(
        self,
        person_id: str,
        *,
        platform: str,
        account_id: str,
        scope_type: ProfileScopeType,
        scope_id: str,
    ) -> PersonProfileSnapshot | None:
        """Return the latest snapshot for one person + scope, or None."""

    def save_profile(self, profile: PersonProfileSnapshot) -> PersonProfileSnapshot:
        """Create or update a profile snapshot."""
