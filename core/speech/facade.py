from __future__ import annotations

import dataclasses
from typing import Any

from core.speech.domain import (
    OutboundDraft,
    PolicyDecision,
    SpeechSnapshot,
    TurnContext,
)
from core.speech.guards import GuardChain
from core.speech.resolver import (
    AudienceResolver,
    ConductResolver,
    EngagementResolver,
    PersonaResolver,
)
from core.speech.store import SpeechStore, get_speech_store


class SpeechFacade:
    """Single application-service entry point for speech quality.

    Exactly one snapshot is resolved per turn.  All prompt/guard work for that
    turn consumes the immutable snapshot, so a Web edit during generation can
    never mix an old persona with new rules.
    """

    def __init__(self, store: SpeechStore):
        self.store = store
        self.audience_resolver = AudienceResolver()
        self.persona_resolver = PersonaResolver(store)
        self.conduct_resolver = ConductResolver(store)
        self.engagement_resolver = EngagementResolver(store)
        self.guard = GuardChain()
        self._cache: dict[tuple, SpeechSnapshot] = {}

    def resolve(self, turn: TurnContext, repo: Any) -> SpeechSnapshot:
        audience = self.audience_resolver.resolve(turn)
        turn = dataclasses.replace(turn, audience=audience)
        revision = self.store.get_revision()
        cache_key = (
            revision,
            turn.target.key,
            audience.value,
            turn.is_owner,
            turn.is_mentioned,
            turn.trigger_reason,
            turn.msg_type,
            turn.sender_role,
            turn.relationship_tier,
            turn.intimacy,
        )
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        persona = self.persona_resolver.resolve(turn, repo)
        policy = self.conduct_resolver.resolve(turn, persona)
        snapshot = SpeechSnapshot(
            turn=turn,
            audience=audience,
            persona=persona,
            policy=policy,
        )
        self._cache[cache_key] = snapshot
        # Keep the cache bounded for long-running scheduler processes.
        if len(self._cache) > 512:
            for key in list(self._cache.keys())[:128]:
                self._cache.pop(key, None)
        return snapshot

    def resolve_engagement(self, turn: TurnContext, repo: Any = None):
        return self.engagement_resolver.resolve(turn, repo)

    def evaluate(self, draft: OutboundDraft,
                 snapshot: SpeechSnapshot) -> PolicyDecision:
        return self.guard.evaluate(draft, snapshot.policy)

    def invalidate(self) -> None:
        self.store.invalidate()
        self._cache.clear()


_FACADE: SpeechFacade | None = None


def get_speech_facade(store: SpeechStore | None = None) -> SpeechFacade:
    global _FACADE
    if store is not None:
        _FACADE = SpeechFacade(store)
        return _FACADE
    if _FACADE is None:
        _FACADE = SpeechFacade(get_speech_store())
    return _FACADE
