"""Regression tests for the platform-neutral person memory layer.

The important design assertion is that all tests use ``platform="qq"`` and no
WeChat-specific field (wxid, @chatroom, real_sender_id, ...) appears in the
domain layer.  Swap the fake repositories for the SQLAlchemy adapter and the
same code path works for any platform.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.contracts.events import ChatType, MessageEvent, MessageKind
from core.contracts.person_memory import (
    ObservationKind,
    ObservationVisibility,
    PersonContextRequest,
    PersonObservation,
    PersonProfileSnapshot,
    ProfileScopeType,
)
from core.people.aggregator import RuleBasedProfileAggregator
from core.people.context import PersonContextBuilder
from core.people.extractor import RuleBasedObservationExtractor
from core.people.service import PersonMemoryService
from core.people.visibility import (
    VisibleScope,
    filter_visible_observations,
    observation_in_scope,
    observation_visible_to,
    rank_observations,
)

NOW = datetime(2026, 9, 18, 20, 0)


def _observation(
    observation_id: str,
    *,
    person_id: str,
    scope_type: ChatType,
    scope_id: str,
    visibility: ObservationVisibility,
    content: str,
    speaker_person_id: str = "",
    mentioned: tuple[str, ...] = (),
    confidence: float = 0.9,
    kind: ObservationKind = ObservationKind.FACT,
) -> PersonObservation:
    return PersonObservation(
        observation_id=observation_id,
        person_id=person_id,
        platform="qq",
        account_id="bot_10001",
        occurred_scope_type=scope_type,
        occurred_scope_id=scope_id,
        speaker_person_id=speaker_person_id,
        mentioned_person_ids=mentioned,
        source_evidence_id=f"ev_{observation_id}",
        source_message_id=f"msg_{observation_id}",
        observed_at=NOW,
        kind=kind,
        content=content,
        confidence=confidence,
        visibility=visibility,
    )


class InMemoryObservationRepository:
    def __init__(self, observations=()):
        self.rows = list(observations)

    def save_many(self, observations) -> int:
        existing = {row.observation_id: row for row in self.rows}
        for observation in observations:
            existing[observation.observation_id] = observation
        self.rows = list(existing.values())
        return len(observations)

    def list_for_person(self, person_id, *, scope=None, since=None, limit=200):
        """召回读口：与 SQL 适配器同口径——没有合法 scope 就什么都不返回。"""
        if scope is None or scope.problem():
            return []
        rows = [
            row for row in self.rows
            if person_id in row.related_person_ids
            and observation_in_scope(row, scope)
            and (since is None or (row.observed_at and row.observed_at >= since))
        ]
        rows.sort(
            key=lambda row: (row.observed_at or datetime.min.replace(tzinfo=timezone.utc)),
            reverse=True,
        )
        return rows[:limit]

    def list_for_projection(self, person_id, *, platform="", account_id="", since=None, limit=1000):
        """内部聚合读口（非召回）：按人 + 租户取全部范围。"""
        rows = [
            row for row in self.rows
            if person_id in row.related_person_ids
            and (not platform or row.platform == platform)
            and (not account_id or row.account_id == account_id)
            and (since is None or (row.observed_at and row.observed_at >= since))
        ]
        rows.sort(
            key=lambda row: (row.observed_at or datetime.min.replace(tzinfo=timezone.utc)),
            reverse=True,
        )
        return rows[:limit]


class InMemoryProfileRepository:
    def __init__(self):
        self.rows = {}

    def get_profile(self, person_id, *, platform, account_id, scope_type, scope_id):
        return self.rows.get((person_id, platform, account_id, scope_type.value, scope_id))

    def save_profile(self, profile):
        self.rows[(profile.person_id, profile.platform, profile.account_id,
                   profile.scope_type.value, profile.scope_id)] = profile
        return profile


def _sample_observations() -> list[PersonObservation]:
    return [
        _observation(
            "group_p", person_id="qq_p", scope_type=ChatType.GROUP,
            scope_id="qq_group_1", visibility=ObservationVisibility.GROUP_SHARED,
            content="明天下午有课，地点在一教西 1004",
            speaker_person_id="qq_p", kind=ObservationKind.PLAN,
        ),
        _observation(
            "private_p", person_id="qq_p", scope_type=ChatType.PRIVATE,
            scope_id="qq_p", visibility=ObservationVisibility.PRIVATE_ONLY,
            content="喜欢冰美式，不喜欢太甜",
            kind=ObservationKind.PREFERENCE,
        ),
        _observation(
            "private_q_about_p", person_id="qq_p", scope_type=ChatType.PRIVATE,
            scope_id="qq_q", visibility=ObservationVisibility.PRIVATE_ONLY,
            content="P5说P6明天不来上课",
            speaker_person_id="qq_q", kind=ObservationKind.OPINION,
        ),
        _observation(
            "group_q", person_id="qq_q", scope_type=ChatType.GROUP,
            scope_id="qq_group_1", visibility=ObservationVisibility.GROUP_SHARED,
            content="周末约了人打球",
            speaker_person_id="qq_q", kind=ObservationKind.EVENT,
        ),
    ]


def test_visibility_is_one_way_only() -> None:
    observations = {row.observation_id: row for row in _sample_observations()}

    # Group observation about P can be injected into P's private chat.
    assert observation_visible_to(
        observations["group_p"], platform="qq", account_id="bot_10001",
        chat_type=ChatType.PRIVATE, chat_id="qq_p", viewer_person_id="qq_p",
    ) is True

    # Same group observation cannot be injected into Q's private chat.
    assert observation_visible_to(
        observations["group_p"], platform="qq", account_id="bot_10001",
        chat_type=ChatType.PRIVATE, chat_id="qq_q", viewer_person_id="qq_q",
    ) is False

    # P's private observation cannot be injected into the group.
    assert observation_visible_to(
        observations["private_p"], platform="qq", account_id="bot_10001",
        chat_type=ChatType.GROUP, chat_id="qq_group_1", viewer_person_id="qq_p",
    ) is False

    # Another person's private message that mentions P is never visible to P.
    assert observation_visible_to(
        observations["private_q_about_p"], platform="qq", account_id="bot_10001",
        chat_type=ChatType.PRIVATE, chat_id="qq_p", viewer_person_id="qq_p",
    ) is False

    # But the same private message is visible in Q's own private chat.
    assert observation_visible_to(
        observations["private_q_about_p"], platform="qq", account_id="bot_10001",
        chat_type=ChatType.PRIVATE, chat_id="qq_q", viewer_person_id="qq_q",
    ) is True


def test_owner_override_can_see_everything_in_tenant() -> None:
    observations = _sample_observations()
    visible = filter_visible_observations(
        observations,
        platform="qq",
        account_id="bot_10001",
        chat_type=ChatType.PRIVATE,
        chat_id="qq_p",
        viewer_person_id="qq_p",
        viewer_is_owner=True,
    )
    assert {row.observation_id for row in visible} == {
        "group_p", "private_p", "private_q_about_p", "group_q",
    }


def test_context_builder_private_and_group() -> None:
    observations = InMemoryObservationRepository(_sample_observations())
    profiles = InMemoryProfileRepository()
    profiles.save_profile(PersonProfileSnapshot(
        profile_id="profile_private_p",
        person_id="qq_p",
        platform="qq",
        account_id="bot_10001",
        scope_type=ProfileScopeType.PRIVATE,
        scope_id="qq_p",
        summary="普通朋友，喜欢简洁语气",
        topics=("学习", "课程"),
        observation_count=2,
    ))
    builder = PersonContextBuilder(observations, profiles)

    private_context = builder.build(PersonContextRequest(
        platform="qq",
        account_id="bot_10001",
        chat_type=ChatType.PRIVATE,
        chat_id="qq_p",
        target_person_id="qq_p",
        viewer_person_id="qq_p",
        max_observations=8,
    ))
    assert "明天下午有课" in private_context.prompt_block
    assert "喜欢冰美式" in private_context.prompt_block
    assert "P5说P6" not in private_context.prompt_block
    assert "周末约了人打球" not in private_context.prompt_block
    assert private_context.source_evidence_ids

    group_context = builder.build(PersonContextRequest(
        platform="qq",
        account_id="bot_10001",
        chat_type=ChatType.GROUP,
        chat_id="qq_group_1",
        target_person_id="qq_p",
        viewer_person_id="qq_p",
        max_observations=8,
    ))
    assert "明天下午有课" in group_context.prompt_block
    assert "喜欢冰美式" not in group_context.prompt_block
    assert "P5说P6" not in group_context.prompt_block


def test_ranking_is_deterministic() -> None:
    rows = _sample_observations()
    ranked = rank_observations(rows, query="课 地点", current_scope_type=ChatType.GROUP,
                               current_scope_id="qq_group_1", now=NOW)
    assert ranked
    assert ranked[0].observation_id in {"group_p", "group_q"}


def test_service_wires_ports_without_platform_imports() -> None:
    observations = InMemoryObservationRepository()
    profiles = InMemoryProfileRepository()
    service = PersonMemoryService(
        observations,
        profiles,
        RuleBasedObservationExtractor(),
        RuleBasedProfileAggregator(),
    )
    event = MessageEvent.new(
        platform="qq",
        account_id="bot_10001",
        chat_id="qq_group_x",
        chat_type=ChatType.GROUP,
        message_id="m_service",
        sender_id="qq_p",
        sender_name="P6",
        kind=MessageKind.TEXT,
        content="我明天要去上课",
        created_at=NOW,
    )
    extracted = service.ingest_event(
        event,
        subject_person_id="qq_p",
        speaker_person_id="qq_p",
        evidence_id="ev_service",
    )
    assert len(extracted) == 1
    assert len(observations.rows) == 1

    profile = service.refresh_profile(
        "qq_p",
        platform="qq",
        account_id="bot_10001",
        scope_type=ProfileScopeType.GROUP,
        scope_id="qq_group_x",
    )
    assert profile.observation_count == 1
    assert profile.scope_type == ProfileScopeType.GROUP
    assert profile.platform == "qq"


def test_refresh_profiles_for_observations() -> None:
    observations = _sample_observations()
    repo = InMemoryObservationRepository(observations)
    profiles = InMemoryProfileRepository()
    service = PersonMemoryService(
        repo, profiles, None, RuleBasedProfileAggregator(),
    )
    refreshed = service.refresh_profiles_for_observations(observations)
    keys = {
        (profile.person_id, profile.scope_type.value, profile.scope_id)
        for profile in refreshed
    }
    assert ("qq_p", "global", "") in keys
    assert ("qq_p", "group", "qq_group_1") in keys
    assert ("qq_p", "private", "qq_p") in keys
    assert ("qq_p", "private", "qq_q") in keys
    assert ("qq_q", "global", "") in keys
    assert ("qq_q", "group", "qq_group_1") in keys

    first_versions = {
        (profile.scope_type.value, profile.scope_id): profile.version
        for profile in refreshed
    }
    refreshed_again = service.refresh_profiles_for_observations(observations)
    for profile in refreshed_again:
        key = (profile.scope_type.value, profile.scope_id)
        assert profile.version == first_versions[key] + 1


def test_sqlalchemy_adapter_roundtrip() -> None:
    from db.meta_models import init_meta_db
    from db.repo_person_memory import PersonMemoryRepository

    root = Path(__file__).resolve().parent.parent
    db_path = root / "data" / "_test_person_memory.db"
    for suffix in ("", "-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)
    engine = init_meta_db(str(db_path))
    try:
        repo = PersonMemoryRepository(engine)
        observations = _sample_observations()
        assert repo.save_many(observations) == 4
        # R-I3：行级过滤在数据层。P 在自己私聊里能看到"本群说他 + 他私聊说的"，
        # **看不到** Q 私聊里提到他的那条（旧断言把它当成可见，正是越界本身）。
        p_private = VisibleScope(
            platform="qq", account_id="bot_10001", requester_person_id="qq_p",
            chat_type=ChatType.PRIVATE, chat_id="qq_p",
        )
        loaded = repo.list_for_person("qq_p", scope=p_private)
        assert {row.observation_id for row in loaded} == {"group_p", "private_p"}
        # P 在群里只看到本群那条：他自己的私聊、Q 的私聊都不许进群。
        p_group = VisibleScope(
            platform="qq", account_id="bot_10001", requester_person_id="qq_p",
            chat_type=ChatType.GROUP, chat_id="qq_group_1",
        )
        group_loaded = repo.list_for_person("qq_p", scope=p_group)
        assert {row.observation_id for row in group_loaded} == {"group_p"}
        # 没有 scope = 拒读（不是"无过滤"）。
        assert repo.list_for_person("qq_p") == []
        # Private observations from Q must remain private to Q.
        q_private = VisibleScope(
            platform="qq", account_id="bot_10001", requester_person_id="qq_q",
            chat_type=ChatType.PRIVATE, chat_id="qq_q",
        )
        q_loaded = repo.list_for_person("qq_q", scope=q_private)
        assert {row.observation_id for row in q_loaded} == {"group_q", "private_q_about_p"}

        saved_profile = repo.save_profile(PersonProfileSnapshot(
            profile_id="",
            person_id="qq_p",
            platform="qq",
            account_id="bot_10001",
            scope_type=ProfileScopeType.GLOBAL,
            scope_id="",
            summary="QQ 平台联系人样例",
            version=2,
        ))
        assert saved_profile.profile_id
        loaded_profile = repo.get_profile(
            "qq_p", platform="qq", account_id="bot_10001",
            scope_type=ProfileScopeType.GLOBAL, scope_id="",
        )
        assert loaded_profile is not None
        assert loaded_profile.version == 2
    finally:
        engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            Path(str(db_path) + suffix).unlink(missing_ok=True)


def main() -> bool:
    tests = [
        test_visibility_is_one_way_only,
        test_owner_override_can_see_everything_in_tenant,
        test_context_builder_private_and_group,
        test_ranking_is_deterministic,
        test_service_wires_ports_without_platform_imports,
        test_refresh_profiles_for_observations,
        test_sqlalchemy_adapter_roundtrip,
    ]
    failed: list[str] = []
    for test in tests:
        try:
            test()
            print(f"  ok   {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed.append(test.__name__)
            print(f"  FAIL {test.__name__}: {exc!r}")
    print(f"person memory checks: {len(tests) - len(failed)}/{len(tests)} passed")
    return not failed


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
