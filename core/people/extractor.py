"""Platform-neutral observation extraction.

The extractor receives a canonical ``MessageEvent`` plus canonical person ids
resolved by the identity layer.  It never imports DB, LLM or platform code.
A future LLM extractor can implement the same ``ObservationExtractor`` protocol
and be combined with this rule-based baseline.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Protocol, Sequence

from core.contracts.events import ChatType, MessageEvent, MessageKind
from core.contracts.person_memory import (
    ObservationKind,
    ObservationVisibility,
    PersonObservation,
)
from core.people.ids import stable_observation_id
from safelog import warn


class ObservationExtractor(Protocol):
    def extract(
        self,
        event: MessageEvent,
        *,
        subject_person_id: str,
        speaker_person_id: str = "",
        evidence_id: str = "",
    ) -> list[PersonObservation]:
        """Return zero or more observations derived from one event."""


_RULE_ROWS: tuple[tuple[ObservationKind, tuple[str, ...], float], ...] = (
    (
        ObservationKind.PREFERENCE,
        ("喜欢", "最爱", "讨厌", "不喜欢", "不吃", "爱喝", "爱好", "受不了", "偏爱"),
        0.72,
    ),
    (
        ObservationKind.IDENTITY,
        ("我叫", "我是", "我的名字", "就读", "住在", "老家", "专业是", "生日是"),
        0.75,
    ),
    (
        ObservationKind.PLAN,
        ("明天", "后天", "下周", "周末", "今晚", "下午", "早上", "几点", "要去", "打算", "计划", "准备"),
        0.66,
    ),
    (
        ObservationKind.EVENT,
        ("考试", "上课", "开会", "作业", "截止", "提交", "活动", "比赛", "面试", "约了"),
        0.62,
    ),
)

_TEXT_KINDS = frozenset({
    MessageKind.TEXT,
    MessageKind.QUOTE_REPLY,
    MessageKind.FORWARD,
    MessageKind.SYSTEM,
})


_QUOTE_BLOCK_RE = re.compile(r"\[引用[^\]]*\]")


def _strip_quoted_blocks(content: str) -> str:
    """2026-09-22 去回声：引用块里可能是鲸鲸自己的话，不能当用户证据。

    引用格式形如 ``[引用 鲸鲸: …] 用户真正说的话``；只保留引用外的
    用户文本，引用块（或未闭合的引用开头）一律丢弃。
    """
    value = str(content or "")
    if "[引用" not in value:
        return value.strip()
    value = _QUOTE_BLOCK_RE.sub(" ", value)
    marker = value.find("[引用")
    if marker != -1:
        value = value[:marker]
    return value.strip()


def _is_forwarded_chat_record(content: str) -> bool:
    """Heuristic: ``X与Y的聊天记录`` forwards quote other speakers.

    Rule-based keyword extraction cannot know who said which quoted line; a
    forward titled "P6与鲸鲸的聊天记录" made 鲸鲸's self-introduction look
    like an identity observation about P6.  Skip those; ordinary forwarded
    text without a chat-record header still goes through normal extraction.
    """
    head = str(content or "").strip()[:80]
    return "的聊天记录" in head or head.endswith("聊天记录")


def _scope_visibility(chat_type: ChatType) -> ObservationVisibility:
    if chat_type in (ChatType.GROUP, ChatType.CHANNEL):
        return ObservationVisibility.GROUP_SHARED
    return ObservationVisibility.PRIVATE_ONLY


class RuleBasedObservationExtractor:
    """Cheap, deterministic first pass that runs before any LLM extraction."""

    model_version = "rule_v1"

    def extract(
        self,
        event: MessageEvent,
        *,
        subject_person_id: str,
        speaker_person_id: str = "",
        evidence_id: str = "",
    ) -> list[PersonObservation]:
        subject_person_id = str(subject_person_id or speaker_person_id or "").strip()
        if not subject_person_id:
            return []
        if event.kind not in _TEXT_KINDS:
            return []
        content = _strip_quoted_blocks(str(event.content or ""))
        if not content:
            return []
        if event.kind == MessageKind.FORWARD and _is_forwarded_chat_record(content):
            return []

        matched: list[tuple[ObservationKind, int, float]] = []
        for kind, keywords, confidence in _RULE_ROWS:
            hits = sum(1 for keyword in keywords if keyword in content)
            if hits:
                matched.append((kind, hits, min(0.95, confidence + 0.05 * (hits - 1))))
        if not matched:
            return []

        # Most hits wins; ties resolve by the rule-table order (stable).
        rule_index = {
            kind: index for index, (kind, _keywords, _confidence) in enumerate(_RULE_ROWS)
        }
        kind, hits, confidence = max(
            matched,
            key=lambda item: (item[1], -rule_index.get(item[0], len(_RULE_ROWS))),
        )
        mentioned = tuple(
            mention.person_id
            for mention in (event.mentions or ())
            if mention.person_id and mention.person_id not in (subject_person_id, speaker_person_id)
        )
        observed_at = event.created_at or datetime.now()
        return [
            PersonObservation(
                observation_id=stable_observation_id(
                    model_version=self.model_version,
                    platform=event.platform,
                    account_id=event.account_id,
                    scope_type=event.chat.chat_type.value,
                    scope_id=event.chat.chat_id,
                    message_id=event.message_id,
                    kind=kind.value,
                    content=content,
                ),
                person_id=subject_person_id,
                platform=event.platform,
                account_id=event.account_id,
                occurred_scope_type=event.chat.chat_type,
                occurred_scope_id=event.chat.chat_id,
                speaker_person_id=speaker_person_id or subject_person_id,
                mentioned_person_ids=mentioned,
                source_evidence_id=str(evidence_id or ""),
                source_message_id=str(event.message_id or ""),
                observed_at=observed_at,
                kind=kind,
                content=content[:500],
                raw_quote=content[:200],
                normalized_key=f"{kind.value}.rule_v1",
                confidence=confidence,
                importance=0.5,
                visibility=_scope_visibility(event.chat.chat_type),
                status="active",
                model_version=self.model_version,
                evidence_ids=(str(evidence_id),) if evidence_id else (),
            )
        ]


class CompositeObservationExtractor:
    """Run several extractors and keep the richest result.

    A future LLM extractor can be appended without changing callers; failures
    of one extractor never block the others.
    """

    def __init__(self, *extractors: ObservationExtractor) -> None:
        self._extractors = tuple(extractors)

    def extract(
        self,
        event: MessageEvent,
        *,
        subject_person_id: str,
        speaker_person_id: str = "",
        evidence_id: str = "",
    ) -> list[PersonObservation]:
        results: list[PersonObservation] = []
        seen: set[tuple[str, str, str]] = set()
        for extractor in self._extractors:
            try:
                current = extractor.extract(
                    event,
                    subject_person_id=subject_person_id,
                    speaker_person_id=speaker_person_id,
                    evidence_id=evidence_id,
                )
            except Exception:
                warn("core/people/extractor.py:214 Exception", err=repr(__import__("sys").exc_info()[1]))
                continue
            for observation in current or ():
                key = (
                    observation.person_id,
                    observation.normalized_key,
                    observation.content[:120],
                )
                if key in seen:
                    continue
                seen.add(key)
                results.append(observation)
        return results


class SyncObservationExtractorAdapter:
    """Expose a synchronous extractor through the async port."""

    def __init__(self, extractor: ObservationExtractor) -> None:
        self._extractor = extractor

    async def extract(
        self,
        event: MessageEvent,
        *,
        subject_person_id: str,
        speaker_person_id: str = "",
        evidence_id: str = "",
    ) -> list[PersonObservation]:
        return self._extractor.extract(
            event,
            subject_person_id=subject_person_id,
            speaker_person_id=speaker_person_id,
            evidence_id=evidence_id,
        )


class CompositeAsyncObservationExtractor:
    """Run several async extractors without letting one failure block others."""

    def __init__(self, *extractors: object) -> None:
        self._extractors = tuple(extractors)

    async def extract(
        self,
        event: MessageEvent,
        *,
        subject_person_id: str,
        speaker_person_id: str = "",
        evidence_id: str = "",
    ) -> list[PersonObservation]:
        results: list[PersonObservation] = []
        seen: set[tuple[str, str, str]] = set()
        for extractor in self._extractors:
            try:
                current = await extractor.extract(
                    event,
                    subject_person_id=subject_person_id,
                    speaker_person_id=speaker_person_id,
                    evidence_id=evidence_id,
                )
            except Exception:
                warn("core/people/extractor.py:275 Exception", err=repr(__import__("sys").exc_info()[1]))
                continue
            for observation in current or ():
                key = (
                    observation.person_id,
                    observation.normalized_key,
                    observation.content[:120],
                )
                if key in seen:
                    continue
                seen.add(key)
                results.append(observation)
        return results
