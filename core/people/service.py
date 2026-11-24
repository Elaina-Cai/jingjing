"""Application service for person memory.

Wires an extractor and an aggregator to the repository ports.  It holds no
platform, DB, LLM or scheduling concerns: a worker in the scheduler (or a CLI
backfill script) can call ``ingest_event`` / ``refresh_profile`` for any
platform.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.contracts.events import MessageEvent
from core.contracts.person_memory import (
    PersonObservation,
    PersonProfileSnapshot,
    ProfileScopeType,
)
from core.people.aggregator import ProfileAggregator
from core.people.extractor import ObservationExtractor
from core.people.ports import (
    AsyncObservationExtractor,
    ObservationRepository,
    ProfileRepository,
)


def _profile_scope(observation: PersonObservation) -> ProfileScopeType:
    if observation.is_group_scope():
        return ProfileScopeType.GROUP
    if observation.is_private_scope():
        return ProfileScopeType.PRIVATE
    return ProfileScopeType.GLOBAL


class PersonMemoryService:
    def __init__(
        self,
        observations: ObservationRepository,
        profiles: ProfileRepository,
        extractor: ObservationExtractor | None,
        aggregator: ProfileAggregator | None,
        *,
        async_extractor: AsyncObservationExtractor | None = None,
    ) -> None:
        self._observations = observations
        self._profiles = profiles
        self._extractor = extractor
        self._aggregator = aggregator
        self._async_extractor = async_extractor

    # ------------------------------------------------------------------ ingest
    def ingest_event(
        self,
        event: MessageEvent,
        *,
        subject_person_id: str,
        speaker_person_id: str = "",
        evidence_id: str = "",
    ) -> list[PersonObservation]:
        if self._extractor is None:
            return []
        extracted = self._extractor.extract(
            event,
            subject_person_id=subject_person_id,
            speaker_person_id=speaker_person_id,
            evidence_id=evidence_id,
        )
        if not extracted:
            return []
        self._observations.save_many(extracted)
        return extracted

    async def ingest_event_async(
        self,
        event: MessageEvent,
        *,
        subject_person_id: str,
        speaker_person_id: str = "",
        evidence_id: str = "",
    ) -> list[PersonObservation]:
        if self._async_extractor is not None:
            extracted = await self._async_extractor.extract(
                event,
                subject_person_id=subject_person_id,
                speaker_person_id=speaker_person_id,
                evidence_id=evidence_id,
            )
        elif self._extractor is not None:
            extracted = self._extractor.extract(
                event,
                subject_person_id=subject_person_id,
                speaker_person_id=speaker_person_id,
                evidence_id=evidence_id,
            )
        else:
            return []
        if not extracted:
            return []
        self._observations.save_many(extracted)
        return extracted

    # ----------------------------------------------------------------- profile
    def refresh_profiles_for_observations(
        self,
        observations,
    ) -> list[PersonProfileSnapshot]:
        """Refresh global + scope profiles for every person touched.

        The ingestion worker calls this after writing observations.  The method
        is deliberately small and port-only; scheduling and feature flags stay
        in the caller.
        """
        if not observations or self._aggregator is None:
            return []

        scopes: dict[tuple, None] = {}
        for observation in observations:
            person_id = str(getattr(observation, "person_id", "") or "")
            if not person_id:
                continue
            platform = str(getattr(observation, "platform", "") or "")
            account_id = str(getattr(observation, "account_id", "") or "")
            base = (person_id, platform, account_id)
            scopes[base + (ProfileScopeType.GLOBAL, "")] = None
            if observation.is_group_scope():
                scopes[base + (ProfileScopeType.GROUP, observation.occurred_scope_id)] = None
            elif observation.is_private_scope():
                scopes[base + (ProfileScopeType.PRIVATE, observation.occurred_scope_id)] = None

        results: list[PersonProfileSnapshot] = []
        for (person_id, platform, account_id, scope_type, scope_id) in scopes:
            current = self._profiles.get_profile(
                person_id,
                platform=platform,
                account_id=account_id,
                scope_type=scope_type,
                scope_id=scope_id,
            )
            version = (current.version + 1) if current is not None else 1
            results.append(self.refresh_profile(
                person_id,
                platform=platform,
                account_id=account_id,
                scope_type=scope_type,
                scope_id=scope_id,
                since_days=0,
                version=version,
            ))
        return results

    def refresh_profile(
        self,
        person_id: str,
        *,
        platform: str,
        account_id: str,
        scope_type: ProfileScopeType,
        scope_id: str = "",
        since_days: int = 180,
        version: int = 1,
    ) -> PersonProfileSnapshot:
        since = None
        if since_days and int(since_days) > 0:
            since = datetime.now() - timedelta(days=int(since_days))
        # 画像聚合读的是"这个人所有会话的观察"（折进快照），所以走**非召回**的
        # list_for_projection；面向会话的读只能走 list_for_person（R-I1/R-I3 的
        # 行级过滤在那条路上，缺 scope 即拒绝）。
        observations = self._observations.list_for_projection(
            person_id,
            platform=platform,
            account_id=account_id,
            since=since,
            limit=1000,
        )
        if scope_type == ProfileScopeType.GLOBAL:
            scoped = [row for row in observations]
        else:
            scope_value = scope_type.value
            scoped = [
                row for row in observations
                if row.occurred_scope_type.value == scope_value
                and row.occurred_scope_id == scope_id
            ]
        profile = self._aggregator.aggregate(
            scoped,
            person_id=person_id,
            platform=platform,
            account_id=account_id,
            scope_type=scope_type,
            scope_id=scope_id,
            version=version,
        )
        return self._profiles.save_profile(profile)
