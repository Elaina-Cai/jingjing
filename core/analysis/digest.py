"""Daily digest assembly for Phase 5 Track B.

This is the event-stream replacement for ``DailyDigestAgent``.  It reads only
analysis projections and terminal events; it must not call ``recall_memories``
or any legacy memory/analysis table.  The digest keeps the source event ids for
the whole day and is owner-facing.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Iterable

from core.analysis.event_reader import aware, is_analysis_event
from core.analysis.projectors import (
    PROJECTION_DIGEST,
    PROJECTION_GROUP,
    PROJECTION_MESSAGE,
    AnalysisProjectionDraft,
    _event_text,
    _keywords,
    _summarize_events,
)
from core.terminal.event_model import Event
from core.terminal.principal import VisibilityScope
from safelog import warn


def _attr(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _draft_date(value: Any) -> date | None:
    occurred_at = _attr(value, "occurred_at")
    if isinstance(occurred_at, datetime):
        return aware(occurred_at).date()
    metadata = _attr(value, "metadata", {}) or {}
    raw_date = _attr(metadata, "date", "")
    if raw_date:
        try:
            return date.fromisoformat(str(raw_date))
        except ValueError:
            warn("core/analysis/digest.py:45 ValueError", err=repr(__import__("sys").exc_info()[1]))
    return None


def _source_ids(values: Iterable[Any]) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for value in values:
        for event_id in _attr(value, "source_event_ids", ()) or ():
            if event_id:
                seen.setdefault(str(event_id), None)
    return tuple(seen)


def _event_day(event: Event) -> date:
    return aware(event.occurred_at).date()


def _clip(value: Any, limit: int = 180) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def _analysis_lines(analyses: Iterable[Any], *, limit: int = 12) -> list[str]:
    lines: list[str] = []
    for analysis in analyses:
        summary = _clip(_attr(analysis, "summary") or _attr(analysis, "content"))
        if summary:
            lines.append(f"- {summary}")
        if len(lines) >= limit:
            break
    return lines


@dataclass
class DigestBuilder:
    """Pure digest rule; persistence belongs to the worker."""

    def build(
        self,
        target_date: date,
        *,
        events: Iterable[Event] = (),
        analyses: Iterable[Any] = (),
        person_observations: Iterable[Any] = (),
        now: datetime | None = None,
    ) -> AnalysisProjectionDraft | None:
        day_events = [
            event
            for event in events
            if isinstance(event, Event) and _event_day(event) == target_date
        ]
        day_events = [event for event in day_events if is_analysis_event(event)]
        day_analyses = [
            analysis
            for analysis in analyses
            if _draft_date(analysis) == target_date
        ]

        source_event_ids = _source_ids([*day_events, *day_analyses])
        if not source_event_ids:
            # The Track B contract forbids writing an analysis row without an
            # event provenance anchor.
            return None

        private_events = [
            event for event in day_events if str(event.chat_type or "") == "private"
        ]
        group_events = [
            event for event in day_events if str(event.chat_type or "") == "group"
        ]
        private_analyses = [
            analysis
            for analysis in day_analyses
            if str(_attr(analysis, "projection_type", "")) == PROJECTION_MESSAGE
        ]
        group_analyses = [
            analysis
            for analysis in day_analyses
            if str(_attr(analysis, "projection_type", "")) == PROJECTION_GROUP
        ]

        lines = [
            f"# {target_date.isoformat()} 微信日报",
            "",
            (
                f"今日事件 {len(day_events)} 条"
                f"（私聊 {len(private_events)} / 群聊 {len(group_events)}）；"
                f"私聊分析 {len(private_analyses)} 条，群摘要 {len(group_analyses)} 条。"
            ),
        ]

        lines.append("")
        lines.append("## 需要关注")
        attention_lines: list[str] = []
        for analysis in [*private_analyses, *group_analyses]:
            metadata = _attr(analysis, "metadata", {}) or {}
            if metadata.get("needs_owner_attention") or metadata.get("action_needed"):
                text = _clip(_attr(analysis, "summary") or _attr(analysis, "content"), 200)
                if text:
                    attention_lines.append(f"- {text}")
        lines.extend(attention_lines[:8] or ["- 无明确需要主人处理的事项"])

        lines.append("")
        lines.append("## 私聊要闻")
        message_lines = _analysis_lines(private_analyses)
        if not message_lines and private_events:
            message_lines = [f"- {_clip(_summarize_events(private_events), 260)}"]
        lines.extend(message_lines or ["- 无新的私聊分析"])

        lines.append("")
        lines.append("## 群聊话题")
        group_lines = _analysis_lines(group_analyses)
        if not group_lines and group_events:
            group_lines = [f"- {_clip(_summarize_events(group_events), 260)}"]
        lines.extend(group_lines or ["- 无新的群聊摘要"])

        topic_counter: Counter[str] = Counter()
        for analysis in [*private_analyses, *group_analyses]:
            metadata = _attr(analysis, "metadata", {}) or {}
            for topic in metadata.get("topics") or ():
                topic_counter[str(topic)] += 1
        if not topic_counter and day_events:
            for topic in _keywords(_event_text(event) for event in day_events):
                topic_counter[topic] += 1
        top_topics = [topic for topic, _count in topic_counter.most_common(8)]
        lines.append("")
        lines.append("## 话题趋势")
        lines.append("- " + ("、".join(top_topics) if top_topics else "无明确话题"))

        observation_count = 0
        lines.append("")
        lines.append("## 人物观察")
        lines.append("- 无新的人物观察")
        try:
            observation_count = sum(1 for _ in person_observations)
        except TypeError:
            warn("core/analysis/digest.py:181 TypeError", err=repr(__import__("sys").exc_info()[1]))
            observation_count = 0
        if observation_count:
            lines[-1] = f"- 新增 {observation_count} 条人物观察，详见人物画像页"

        content = "\n".join(lines)[:6000]
        latest_at = max(
            (aware(event.occurred_at) for event in day_events),
            default=None,
        )
        occurred_at = latest_at or datetime.combine(
            target_date,
            datetime.max.time(),
        )
        return AnalysisProjectionDraft(
            projection_id=f"analysis:daily_digest:{target_date.isoformat()}",
            projection_type=PROJECTION_DIGEST,
            content=content,
            summary=(
                f"{target_date.isoformat()}：{len(day_events)} 条事件，"
                f"{len(private_analyses)} 条私聊分析，{len(group_analyses)} 条群摘要"
            ),
            platform="",
            bot_account_id="",
            chat_id="daily_digest",
            chat_type="",
            occurred_at=occurred_at,
            source_event_ids=source_event_ids,
            visibility_scope=(VisibilityScope.OWNER_ONLY.value,),
            confidence=0.65,
            event_sequence=max(
                (int(event.sequence or 0) for event in day_events),
                default=0,
            ),
            metadata={
                "date": target_date.isoformat(),
                "event_count": len(day_events),
                "private_count": len(private_events),
                "group_count": len(group_events),
                "analysis_count": len(day_analyses),
                "source": "terminal_events",
                "rule_version": 1,
                "topics": top_topics,
            },
        )


def build_daily_digest(
    target_date: date,
    *,
    events: Iterable[Event] = (),
    analyses: Iterable[Any] = (),
    person_observations: Iterable[Any] = (),
    now: datetime | None = None,
) -> AnalysisProjectionDraft | None:
    """Convenience wrapper around :class:`DigestBuilder`."""
    return DigestBuilder().build(
        target_date,
        events=events,
        analyses=analyses,
        person_observations=person_observations,
        now=now,
    )
