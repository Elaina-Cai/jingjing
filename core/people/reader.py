"""Read-only person profile queries.

This is the only core entry point a chat tool needs.  It wraps
``PersonContextBuilder`` and returns a prompt-ready/scoped view; visibility
rules stay in ``core.people.visibility``.
"""

from __future__ import annotations

from core.contracts.person_memory import (
    PersonContext,
    PersonContextRequest,
)
from core.people.context import PersonContextBuilder
from core.people.ports import ObservationRepository, ProfileRepository


class PersonProfileReader:
    def __init__(
        self,
        observations: ObservationRepository,
        profiles: ProfileRepository,
    ) -> None:
        self._builder = PersonContextBuilder(observations, profiles)

    def read(
        self,
        request: PersonContextRequest,
        *,
        owner_cross_account: bool = False,
    ) -> PersonContext:
        return self._builder.build(
            request,
            owner_cross_account=owner_cross_account,
        )

    @staticmethod
    def render_text(context: PersonContext) -> str:
        block = (context.prompt_block or "").strip()
        if not block:
            return ""
        lines = [block]
        if context.source_evidence_ids:
            lines.append(
                "[证据] " + "、".join(context.source_evidence_ids[:8])
            )
        return "\n".join(lines)
