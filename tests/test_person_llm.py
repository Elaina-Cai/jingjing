"""Tests for the core LLM extractor and async service wiring.

The fake completion returns JSON exactly like a real adapter would, so the
prompt/parse contract is tested without importing any LLM SDK.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.contracts.events import ChatType, MessageEvent, MessageKind
from core.contracts.person_memory import (
    ObservationKind,
    ObservationVisibility,
    ProfileScopeType,
)
from core.people.aggregator import RuleBasedProfileAggregator
from core.people.llm_extractor import LlmObservationExtractor
from core.people.service import PersonMemoryService

NOW = datetime(2026, 9, 18, 20, 0, tzinfo=timezone.utc)


class FakeCompletion:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0

    async def complete(self, *, system: str, user: str, agent_name: str = "") -> str:
        self.calls += 1
        return self.text


class InMemoryObservationRepository:
    def __init__(self):
        self.rows = []

    def save_many(self, observations):
        existing = {row.observation_id: row for row in self.rows}
        for row in observations:
            existing[row.observation_id] = row
        self.rows = list(existing.values())
        return len(observations)

    def list_for_person(self, person_id, *, scope=None, since=None, limit=200):
        """召回读口（R-I3）：没有合法 scope 就什么都不返回。"""
        if scope is None or scope.problem():
            return []
        return [
            row for row in self.rows
            if person_id in row.related_person_ids
        ][:limit]

    def list_for_projection(self, person_id, *, platform="", account_id="", since=None, limit=1000):
        """内部聚合读口（非召回）：refresh_profile 用它折画像快照。"""
        return [
            row for row in self.rows
            if person_id in row.related_person_ids
            and (not platform or row.platform == platform)
            and (not account_id or row.account_id == account_id)
        ][:limit]


class InMemoryProfileRepository:
    def __init__(self):
        self.rows = {}

    def get_profile(self, person_id, *, platform, account_id, scope_type, scope_id):
        return self.rows.get((person_id, platform, account_id, scope_type.value, scope_id))

    def save_profile(self, profile):
        self.rows[(profile.person_id, profile.platform, profile.account_id,
                   profile.scope_type.value, profile.scope_id)] = profile
        return profile


def _event() -> MessageEvent:
    return MessageEvent.new(
        platform="qq",
        account_id="bot_10001",
        chat_id="qq_group_1",
        chat_type=ChatType.GROUP,
        message_id="m_llm_1",
        sender_id="qq_p",
        sender_name="P6",
        kind=MessageKind.TEXT,
        content="我最近在准备考试，周末喜欢去图书馆",
        created_at=NOW,
    )


async def _async_cases() -> None:
    completion = FakeCompletion(
        "```json\n"
        "[\n"
        '  {"kind": "event", "content": "最近在准备考试", '
        '"normalized_key": "event.exam", "confidence": 0.9, "importance": 0.8},\n'
        '  {"kind": "preference", "content": "周末喜欢去图书馆", '
        '"normalized_key": "preference.library", "confidence": 0.8}\n'
        "]\n"
        "```"
    )
    extractor = LlmObservationExtractor(completion, max_items=5)
    first = await extractor.extract(
        _event(), subject_person_id="qq_p", speaker_person_id="qq_p", evidence_id="ev_llm",
    )
    assert len(first) == 2
    assert first[0].kind in (ObservationKind.EVENT, ObservationKind.PREFERENCE)
    assert first[0].visibility == ObservationVisibility.GROUP_SHARED
    assert first[0].source_evidence_id == "ev_llm"
    assert first[0].model_version == "llm_v1"

    # Same message + same model must produce stable ids on replay.
    second = await extractor.extract(
        _event(), subject_person_id="qq_p", speaker_person_id="qq_p", evidence_id="ev_llm",
    )
    assert {row.observation_id for row in first} == {
        row.observation_id for row in second
    }

    # Async service must persist through the repository port.
    repo = InMemoryObservationRepository()
    profiles = InMemoryProfileRepository()
    service = PersonMemoryService(
        repo, profiles, None, RuleBasedProfileAggregator(), async_extractor=extractor,
    )
    written = await service.ingest_event_async(
        _event(), subject_person_id="qq_p", speaker_person_id="qq_p", evidence_id="ev_llm",
    )
    assert len(written) == 2
    assert len(repo.rows) == 2
    profile = service.refresh_profile(
        "qq_p", platform="qq", account_id="bot_10001",
        scope_type=ProfileScopeType.GROUP, scope_id="qq_group_1",
    )
    assert profile.observation_count == 2

    # Invalid JSON is a no-op, never an exception.
    bad = LlmObservationExtractor(FakeCompletion("这不是 JSON"), max_items=5)
    assert await bad.extract(
        _event(), subject_person_id="qq_p", speaker_person_id="qq_p",
    ) == []


def test_llm_extractor_and_async_service() -> None:
    asyncio.run(_async_cases())


def main() -> bool:
    tests = [test_llm_extractor_and_async_service]
    failed: list[str] = []
    for test in tests:
        try:
            test()
            print(f"  ok   {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed.append(test.__name__)
            print(f"  FAIL {test.__name__}: {exc!r}")
    print(f"person llm checks: {len(tests) - len(failed)}/{len(tests)} passed")
    return not failed


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
