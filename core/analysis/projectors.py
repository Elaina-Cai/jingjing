"""Pure event -> analysis projection rules for Phase 5 Track B.

Nothing in this module performs I/O, calls an LLM, or writes a legacy
``analysis`` / ``memories`` row.  Workers persist these drafts to
``analysis_projections``; tests can exercise the rules with plain event lists.
Every draft has at least one ``source_event_id`` by construction.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

from core.analysis.event_reader import (
    ConversationWindow,
    aware,
    group_conversations,
    is_analysis_event,
    latest_sequence,
)
from core.terminal.event_model import Event, EventType, Sensitivity
from core.terminal.principal import VisibilityScope


PROJECTION_MESSAGE = "message_analysis"
PROJECTION_GROUP = "group_summary"
PROJECTION_DIGEST = "daily_digest"

_URGENT_MARKERS = (
    "紧急",
    "尽快",
    "马上",
    "立刻",
    "重要",
    "急事",
    "必须",
    "urgent",
)
_ATTENTION_MARKERS = (
    "@",
    "在吗",
    "帮我",
    "帮忙",
    "麻烦",
    "请问",
    "求助",
    "急",
    "需要回复",
    "等你",
)
_ACTION_MARKERS = (
    "?",
    "？",
    "吗",
    "怎么",
    "什么",
    "哪里",
    "帮我",
    "请",
    "麻烦",
    "需要",
    "记得",
    "安排",
    "处理",
    "回复",
)
_RELATION_MARKERS = (
    "分手",
    "和好",
    "喜欢",
    "讨厌",
    "爱你",
    "想你",
    "朋友",
    "情侣",
    "结婚",
    "一起",
    "聚会",
    "约",
    "照顾",
)
_STOPWORDS = frozenset(
    {
        "一个",
        "一些",
        "这个",
        "那个",
        "什么",
        "怎么",
        "可以",
        "不是",
        "没有",
        "就是",
        "还是",
        "已经",
        "今天",
        "明天",
        "昨天",
        "现在",
        "我们",
        "你们",
        "他们",
        "自己",
        "因为",
        "所以",
        "但是",
        "如果",
        "然后",
        "真的",
        "一下",
        "时候",
        "知道",
        "觉得",
        "应该",
        "可能",
        "不会",
        "需要",
    }
)


@dataclass(frozen=True)
class AnalysisProjectionDraft:
    """One explainable analysis result ready for persistence."""

    projection_id: str
    projection_type: str
    content: str
    summary: str = ""
    platform: str = ""
    bot_account_id: str = ""
    chat_id: str = ""
    chat_type: str = ""
    occurred_at: datetime | None = None
    source_event_ids: tuple[str, ...] = ()
    visibility_scope: tuple[str, ...] = (VisibilityScope.OWNER_ONLY.value,)
    sensitivity: str = Sensitivity.NORMAL.value
    confidence: float = 0.5
    event_sequence: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "projection_id": self.projection_id,
            "projection_type": self.projection_type,
            "content": self.content,
            "summary": self.summary,
            "platform": self.platform,
            "bot_account_id": self.bot_account_id,
            "chat_id": self.chat_id,
            "chat_type": self.chat_type,
            "occurred_at": self.occurred_at.isoformat()
            if self.occurred_at
            else "",
            "source_event_ids": list(self.source_event_ids),
            "visibility_scope": list(self.visibility_scope),
            "sensitivity": self.sensitivity,
            "confidence": self.confidence,
            "event_sequence": self.event_sequence,
            "metadata": dict(self.metadata),
        }


def _stable_key(parts: Iterable[Any]) -> str:
    material = "\x1f".join(str(part or "") for part in parts)
    return hashlib.sha1(material.encode("utf-8")).hexdigest()[:12]


def _event_text(event: Event) -> str:
    content = str(event.content or "").strip()
    if content:
        return content
    if event.event_type in (EventType.ACTION, EventType.TOOL_CALL):
        action = str(event.action_payload.get("action") or "").strip()
        return f"执行了 {action}" if action else ""
    return ""


def _event_speaker(event: Event) -> str:
    from core.terminal.speaker import resolve_speaker

    return resolve_speaker(
        event.event_type,
        event.actor_person_id,
        metadata=event.metadata,
        actor_account_id=event.actor_account_id,
    )


def _source_event_ids(events: Iterable[Event]) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for event in events:
        if event.event_id:
            seen.setdefault(str(event.event_id), None)
    return tuple(seen)


def _visibility_scope(events: Iterable[Event]) -> tuple[str, ...]:
    scopes: dict[str, None] = {}
    for event in events:
        for scope in event.visibility_scope or ():
            scopes.setdefault(str(scope.value if hasattr(scope, "value") else scope), None)
    if not scopes:
        return (VisibilityScope.OWNER_ONLY.value,)
    return tuple(sorted(scopes))


def _day_key(event: Event | None) -> str:
    when = aware(event.occurred_at if event is not None else None)
    return when.date().isoformat()


def _latest_event(events: Iterable[Event]) -> Event | None:
    latest: Event | None = None
    for event in events:
        if latest is None:
            latest = event
            continue
        if aware(event.occurred_at) >= aware(latest.occurred_at):
            latest = event
    return latest


def _keywords(texts: Iterable[str], *, limit: int = 8) -> list[str]:
    """Deterministic keyword candidates without extra dependencies."""
    counter: Counter[str] = Counter()
    for text in texts:
        value = str(text or "")
        for word in re.findall(r"[A-Za-z0-9_]{2,}", value):
            lowered = word.lower()
            if lowered not in _STOPWORDS:
                counter[lowered] += 1
        for run in re.findall(r"[\u4e00-\u9fff]{2,}", value):
            for size in (2, 3):
                if len(run) < size:
                    continue
                for index in range(0, len(run) - size + 1):
                    token = run[index:index + size]
                    if token not in _STOPWORDS:
                        counter[token] += 1
    ranked = sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    return [token for token, _count in ranked[: max(1, int(limit))]]


def _contains_any(text: str, markers: Iterable[str]) -> bool:
    value = str(text or "")
    return any(marker in value for marker in markers)


def _summarize_events(events: Iterable[Event], *, limit: int = 5) -> str:
    lines: list[str] = []
    for event in list(events)[-max(1, int(limit)):]:
        text = _event_text(event)
        if not text:
            continue
        text = re.sub(r"\s+", " ", text).strip()
        speaker = _event_speaker(event)
        lines.append(f"{speaker}: {text[:120]}" if speaker else text[:120])
    return " | ".join(lines)[:320] if lines else ""


def _relation_clues(texts: Iterable[str], *, limit: int = 5) -> list[str]:
    clues: dict[str, None] = {}
    for text in texts:
        value = str(text or "")
        sentences = [
            part.strip()
            for part in re.split(r"[。！？!?；;\n]+", value)
            if part.strip()
        ]
        for sentence in sentences:
            if _contains_any(sentence, _RELATION_MARKERS):
                clues.setdefault(sentence[:120], None)
                if len(clues) >= limit:
                    return list(clues)
    return list(clues)


def _message_importance(events: list[Event]) -> str:
    inbound = [event for event in events if event.event_type != EventType.REPLY]
    if not inbound:
        return "low"
    if any(event.event_type in (EventType.REQUEST, EventType.COMMAND) for event in inbound):
        return "high"
    texts = [_event_text(event) for event in inbound]
    if any(_contains_any(text, _URGENT_MARKERS) for text in texts):
        return "high"
    for event in inbound:
        principal = (event.metadata or {}).get("principal")
        if isinstance(principal, dict) and str(principal.get("relation_to_bot") or "") == "owner":
            return "medium"
    if any(event.event_type == EventType.QUESTION for event in inbound):
        return "medium"
    if len(inbound) >= 3 or sum(len(text) for text in texts) >= 80:
        return "medium"
    return "low"


def _message_action_needed(events: list[Event], importance: str) -> bool:
    inbound = [event for event in events if event.event_type != EventType.REPLY]
    if any(event.event_type in (EventType.REQUEST, EventType.COMMAND) for event in inbound):
        return True
    if importance == "high":
        return True
    return any(_contains_any(_event_text(event), _ACTION_MARKERS) for event in inbound)


def _message_needs_owner_attention(events: list[Event], action_needed: bool) -> bool:
    if not action_needed:
        return False
    for event in events:
        if event.event_type == EventType.REPLY:
            continue
        principal = (event.metadata or {}).get("principal")
        if isinstance(principal, dict) and str(principal.get("relation_to_bot") or "") == "owner":
            return True
    return any(_contains_any(_event_text(event), _ATTENTION_MARKERS) for event in events)


def project_message_analysis(events: Iterable[Event]) -> AnalysisProjectionDraft | None:
    """Build one private-chat analysis from a single conversation window."""
    conversation = [event for event in events if is_analysis_event(event)]
    if not conversation:
        return None
    inbound = [event for event in conversation if event.event_type != EventType.REPLY]
    if not inbound:
        return None

    first = conversation[0]
    platform = str(first.platform or "")
    account_id = str(first.bot_account_id or "")
    chat_id = str(first.chat_id or "")
    if not chat_id:
        return None
    latest = _latest_event(conversation)
    day = _day_key(latest)
    texts = [_event_text(event) for event in inbound]
    topics = _keywords(texts)

    importance = _message_importance(conversation)
    action_needed = _message_action_needed(conversation, importance)
    owner_attention = _message_needs_owner_attention(conversation, action_needed)
    summary = _summarize_events(inbound)
    if not summary:
        summary = f"{len(inbound)} 条私聊消息"
    content = (
        f"私聊 {chat_id} [{day}] 重要度={importance}，"
        f"{'需要主人关注' if owner_attention else '暂不需要主人介入'}。"
        f"{summary}"
    )[:800]
    return AnalysisProjectionDraft(
        projection_id=(
            "analysis:message:"
            + _stable_key((platform, account_id, chat_id, day))
        ),
        projection_type=PROJECTION_MESSAGE,
        content=content,
        summary=summary or content,
        platform=platform,
        bot_account_id=account_id,
        chat_id=chat_id,
        chat_type=str(first.chat_type or "private"),
        occurred_at=aware(latest.occurred_at if latest is not None else None),
        source_event_ids=_source_event_ids(conversation),
        visibility_scope=_visibility_scope(conversation),
        sensitivity=Sensitivity.NORMAL.value,
        confidence=0.60,
        event_sequence=latest_sequence(conversation),
        metadata={
            "importance": importance,
            "action_needed": action_needed,
            "needs_owner_attention": owner_attention,
            "topics": topics,
            "message_count": len(conversation),
            "inbound_count": len(inbound),
            "source": "terminal_events",
            "rule_version": 1,
        },
    )


def project_group_summary(events: Iterable[Event]) -> AnalysisProjectionDraft | None:
    """Build one group summary from a single group conversation window."""
    conversation = [event for event in events if is_analysis_event(event)]
    if not conversation:
        return None
    inbound = [event for event in conversation if event.event_type != EventType.REPLY]
    if not inbound:
        return None

    first = conversation[0]
    platform = str(first.platform or "")
    account_id = str(first.bot_account_id or "")
    chat_id = str(first.chat_id or "")
    if not chat_id:
        return None
    latest = _latest_event(conversation)
    day = _day_key(latest)
    texts = [_event_text(event) for event in inbound]
    topics = _keywords(texts)
    members = sorted(
        {
            _event_speaker(event)
            for event in inbound
            if _event_speaker(event)
        }
    )
    relation_clues = _relation_clues(texts)
    attention = any(
        _contains_any(text, _ATTENTION_MARKERS) for text in texts
    )
    summary = _summarize_events(inbound)
    if not summary:
        summary = f"{len(inbound)} 条群消息"
    topic_text = "、".join(topics[:5]) if topics else "无明确话题"
    content = (
        f"群 {chat_id} [{day}] 共 {len(inbound)} 条新消息，"
        f"{len(members)} 位成员参与；话题：{topic_text}。"
        f"成员：{'、'.join(members[:8])}。"
        f"{summary}"
    )[:1200]
    return AnalysisProjectionDraft(
        projection_id=(
            "analysis:group:"
            + _stable_key((platform, account_id, chat_id, day))
        ),
        projection_type=PROJECTION_GROUP,
        content=content,
        summary=summary or content,
        platform=platform,
        bot_account_id=account_id,
        chat_id=chat_id,
        chat_type=str(first.chat_type or "group"),
        occurred_at=aware(latest.occurred_at if latest is not None else None),
        source_event_ids=_source_event_ids(conversation),
        visibility_scope=_visibility_scope(conversation),
        sensitivity=Sensitivity.NORMAL.value,
        confidence=0.55,
        event_sequence=latest_sequence(conversation),
        metadata={
            "message_count": len(conversation),
            "inbound_count": len(inbound),
            "member_count": len(members),
            "members": members[:20],
            "topics": topics,
            "relation_clues": relation_clues,
            "needs_owner_attention": bool(attention),
            "source": "terminal_events",
            "rule_version": 1,
        },
    )


def project_message_analyses(events: Iterable[Event]) -> list[AnalysisProjectionDraft]:
    """Project every private conversation in a raw event window."""
    drafts: list[AnalysisProjectionDraft] = []
    for window in group_conversations(events):
        if window.chat_type and window.chat_type != "private":
            continue
        draft = project_message_analysis(window.events)
        if draft is not None:
            drafts.append(draft)
    return drafts


def project_group_summaries(events: Iterable[Event]) -> list[AnalysisProjectionDraft]:
    """Project every group conversation in a raw event window."""
    drafts: list[AnalysisProjectionDraft] = []
    for window in group_conversations(events):
        if window.chat_type != "group":
            continue
        draft = project_group_summary(window.events)
        if draft is not None:
            drafts.append(draft)
    return drafts


def project_conversation(
    window: ConversationWindow,
) -> AnalysisProjectionDraft | None:
    """Route one already-grouped window through the correct pure rule."""
    if window.chat_type == "group":
        return project_group_summary(window.events)
    return project_message_analysis(window.events)


def project_events(events: Iterable[Event]) -> list[AnalysisProjectionDraft]:
    """Project message and group analysis drafts in a stable order."""
    materialized = list(events)
    drafts = project_message_analyses(materialized)
    drafts.extend(project_group_summaries(materialized))
    drafts.sort(key=lambda draft: (draft.projection_type, draft.projection_id))
    return drafts
