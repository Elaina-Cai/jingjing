"""Deterministic profile aggregation.

This first pass uses only observations, so it is testable and platform-neutral.
A later LLM summarizer can implement the same ``ProfileAggregator`` protocol or
augment the produced snapshot, but storage and retrieval keep the same shape.
"""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import datetime, timezone
from typing import Protocol, Sequence

from core.contracts.person_memory import (
    PersonObservation,
    PersonProfileSnapshot,
    ProfileScopeType,
)


class ProfileAggregator(Protocol):
    def aggregate(
        self,
        observations: Sequence[PersonObservation],
        *,
        person_id: str,
        platform: str,
        account_id: str,
        scope_type: ProfileScopeType,
        scope_id: str = "",
        version: int = 1,
    ) -> PersonProfileSnapshot:
        """Build one profile snapshot from observations."""


class RuleBasedProfileAggregator:
    """Summarize observations into a scoped profile without an LLM."""

    def aggregate(
        self,
        observations: Sequence[PersonObservation],
        *,
        person_id: str,
        platform: str,
        account_id: str,
        scope_type: ProfileScopeType,
        scope_id: str = "",
        version: int = 1,
    ) -> PersonProfileSnapshot:
        rows = [row for row in observations if row.person_id == person_id]
        kind_counts = Counter(row.kind.value for row in rows)
        topic_keys = [
            row.normalized_key or row.kind.value
            for row in rows
            if (row.normalized_key or row.kind.value)
        ]
        topics = tuple(dict.fromkeys(topic_keys))[:8]
        latest = sorted(
            rows,
            key=lambda row: row.observed_at or datetime.min,
            reverse=True,
        )[:3]
        summary_parts: list[str] = []
        if rows:
            summary_parts.append(f"已收集 {len(rows)} 条观察")
            if kind_counts:
                summary_parts.append(
                    "类型分布：" + "、".join(
                        f"{kind}×{count}" for kind, count in kind_counts.most_common(4)
                    )
                )
            if latest:
                summary_parts.append(
                    "最近记忆：" + "；".join(
                        (row.content or row.raw_quote or "").strip()[:80]
                        for row in latest
                        if (row.content or row.raw_quote)
                    )
                )
        confidence = (
            sum(float(row.confidence or 0.0) for row in rows) / len(rows)
            if rows else 0.0
        )
        now = datetime.now()
        return PersonProfileSnapshot(
            profile_id=uuid.uuid4().hex,
            person_id=person_id,
            platform=platform,
            account_id=account_id,
            scope_type=scope_type,
            scope_id=scope_id,
            summary="；".join(part for part in summary_parts if part),
            style="",
            topics=topics,
            active_hours=(),
            emotion_tendency="",
            relationship_tone="",
            group_map={},
            confidence=confidence,
            observation_count=len(rows),
            version=max(1, int(version or 1)),
            created_at=now,
            updated_at=now,
        )
