"""Platform-neutral observation extraction/aggregation tests.

Every example below uses ``platform="qq"`` and QQ-style ids.  No wxid,
``@chatroom`` or WeChat XML appears in the extraction path.
"""

from __future__ import annotations

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
from core.people.extractor import (
    CompositeObservationExtractor,
    RuleBasedObservationExtractor,
)

NOW = datetime(2026, 9, 18, 20, 0, tzinfo=timezone.utc)


def _qq_event(*, chat_type: ChatType, chat_id: str, content: str, message_id: str = "m1"):
    return MessageEvent.new(
        platform="qq",
        account_id="bot_10001",
        chat_id=chat_id,
        chat_type=chat_type,
        message_id=message_id,
        sender_id="qq_20001",
        sender_name="P6",
        kind=MessageKind.TEXT,
        content=content,
        created_at=NOW,
    )


def test_group_rule_extracts_plan() -> None:
    event = _qq_event(
        chat_type=ChatType.GROUP,
        chat_id="qq_group_openid_1",
        content="我明天下午要去上课，在一教西 1004",
    )
    observations = RuleBasedObservationExtractor().extract(
        event,
        subject_person_id="qq_p",
        speaker_person_id="qq_p",
        evidence_id="ev_1",
    )
    assert len(observations) == 1
    observation = observations[0]
    assert observation.kind == ObservationKind.PLAN
    assert observation.visibility == ObservationVisibility.GROUP_SHARED
    assert observation.platform == "qq"
    assert observation.account_id == "bot_10001"
    assert observation.occurred_scope_id == "qq_group_openid_1"
    assert observation.related_person_ids == ("qq_p",)
    assert observation.source_evidence_id == "ev_1"


def test_private_rule_extracts_preference() -> None:
    event = _qq_event(
        chat_type=ChatType.PRIVATE,
        chat_id="qq_friend_1",
        content="我喜欢冰美式，不喜欢太甜的",
    )
    observations = RuleBasedObservationExtractor().extract(
        event,
        subject_person_id="qq_friend_1",
        speaker_person_id="qq_friend_1",
        evidence_id="ev_2",
    )
    assert len(observations) == 1
    observation = observations[0]
    assert observation.kind == ObservationKind.PREFERENCE
    assert observation.visibility == ObservationVisibility.PRIVATE_ONLY
    assert observation.occurred_scope_type == ChatType.PRIVATE


def test_composite_extractor_keeps_first_unique() -> None:
    event = _qq_event(
        chat_type=ChatType.GROUP,
        chat_id="qq_group_openid_1",
        content="我明天要考试",
    )
    extractor = CompositeObservationExtractor(
        RuleBasedObservationExtractor(),
        RuleBasedObservationExtractor(),
    )
    observations = extractor.extract(
        event,
        subject_person_id="qq_p",
        speaker_person_id="qq_p",
        evidence_id="ev_3",
    )
    assert len(observations) == 1


def test_aggregator_builds_scoped_profile() -> None:
    plan = RuleBasedObservationExtractor().extract(
        _qq_event(
            chat_type=ChatType.GROUP,
            chat_id="qq_group_openid_1",
            content="我明天下午要去上课，在一教西 1004",
        ),
        subject_person_id="qq_p",
        speaker_person_id="qq_p",
    )[0]
    preference = RuleBasedObservationExtractor().extract(
        _qq_event(
            chat_type=ChatType.PRIVATE,
            chat_id="qq_friend_1",
            content="我喜欢冰美式",
            message_id="m2",
        ),
        subject_person_id="qq_p",
        speaker_person_id="qq_p",
    )[0]
    profile = RuleBasedProfileAggregator().aggregate(
        [plan, preference],
        person_id="qq_p",
        platform="qq",
        account_id="bot_10001",
        scope_type=ProfileScopeType.GLOBAL,
        scope_id="",
        version=3,
    )
    assert profile.observation_count == 2
    assert "已收集 2 条观察" in profile.summary
    assert profile.version == 3
    assert any(topic.startswith("plan.") for topic in profile.topics)


def test_rule_observation_id_is_stable() -> None:
    event = _qq_event(
        chat_type=ChatType.GROUP,
        chat_id="qq_group_openid_1",
        content="我明天下午要去上课",
    )
    extractor = RuleBasedObservationExtractor()
    first = extractor.extract(
        event, subject_person_id="qq_p", speaker_person_id="qq_p",
    )
    second = extractor.extract(
        event, subject_person_id="qq_p", speaker_person_id="qq_p",
    )
    assert first and second
    assert first[0].observation_id == second[0].observation_id


def test_extraction_has_no_group_suffix_assumption() -> None:
    event = _qq_event(
        chat_type=ChatType.GROUP,
        chat_id="not_a_chatroom_suffix",
        content="我明天要开会",
    )
    observations = RuleBasedObservationExtractor().extract(
        event,
        subject_person_id="qq_p",
        speaker_person_id="qq_p",
    )
    assert len(observations) == 1
    assert observations[0].is_group_scope() is True


def test_forwarded_chat_record_skips_rule_extraction() -> None:
    event = MessageEvent.new(
        platform="qq",
        account_id="bot_10001",
        chat_id="qq_group_openid_1",
        chat_type=ChatType.GROUP,
        message_id="m_forward",
        sender_id="qq_20001",
        sender_name="P6",
        kind=MessageKind.FORWARD,
        content=(
            "P6与鲸鲸的聊天记录\n"
            "鲸鲸: 我叫鲸鲸，我是鲸鲸狼族的圣女，我的名字是..."
        ),
        created_at=NOW,
    )
    observations = RuleBasedObservationExtractor().extract(
        event,
        subject_person_id="qq_p",
        speaker_person_id="qq_p",
    )
    assert observations == []


def test_quoted_bot_text_is_not_user_evidence() -> None:
    base = dict(
        platform="qq",
        account_id="bot_10001",
        chat_id="qq_group_openid_1",
        chat_type=ChatType.GROUP,
        sender_id="qq_20001",
        sender_name="P6",
        kind=MessageKind.QUOTE_REPLY,
        created_at=NOW,
    )
    quoted_only = MessageEvent.new(
        message_id="m_echo",
        content="[引用 鲸鲸: @P5 我是鲸鲸呀，咋，认不出来了？] 做一下自我介绍",
        **base,
    )
    assert (
        RuleBasedObservationExtractor().extract(
            quoted_only,
            subject_person_id="qq_p",
            speaker_person_id="qq_p",
            evidence_id="ev_echo",
        )
        == []
    )
    quoted_plus_user = MessageEvent.new(
        message_id="m_echo2",
        content="[引用 鲸鲸: 我是鲸鲸呀] 我明天下午要去上课",
        **base,
    )
    observations = RuleBasedObservationExtractor().extract(
        quoted_plus_user,
        subject_person_id="qq_p",
        speaker_person_id="qq_p",
        evidence_id="ev_echo2",
    )
    assert len(observations) == 1
    assert observations[0].kind == ObservationKind.PLAN
    assert "鲸鲸" not in observations[0].content
    print("  ok   quoted bot text is not extracted as user evidence")


def main() -> bool:
    tests = [
        test_group_rule_extracts_plan,
        test_quoted_bot_text_is_not_user_evidence,
        test_private_rule_extracts_preference,
        test_composite_extractor_keeps_first_unique,
        test_aggregator_builds_scoped_profile,
        test_rule_observation_id_is_stable,
        test_extraction_has_no_group_suffix_assumption,
        test_forwarded_chat_record_skips_rule_extraction,
    ]
    failed: list[str] = []
    for test in tests:
        try:
            test()
            print(f"  ok   {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed.append(test.__name__)
            print(f"  FAIL {test.__name__}: {exc!r}")
    print(f"person extraction checks: {len(tests) - len(failed)}/{len(tests)} passed")
    return not failed


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
