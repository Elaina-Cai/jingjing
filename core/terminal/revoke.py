"""Parse WeChat revoke notices into stable, scope-neutral facts."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable

from core.terminal.event_model import Event, HUMAN_SPEECH_TYPES


_REVOKE_RE = re.compile(
    r"<sysmsg\b[^>]*\btype\s*=\s*[\"']revokemsg[\"'][^>]*>"
    r"(.*?)(?:</sysmsg>|$)",
    re.I | re.S,
)
_CONTENT_RE = re.compile(r"<content>(.*?)</content>", re.I | re.S)
_REVOKE_LOCAL_WINDOW = timedelta(seconds=120)


@dataclass(frozen=True)
class RevokeNotice:
    reference_external_msg_id: str = ""
    actor_label: str = ""
    is_self: bool = False
    notice: str = ""
    revoke_time: str = ""


def event_is_self(event: Event) -> bool:
    try:
        return bool(int((event.metadata or {}).get("is_self") or 0))
    except (TypeError, ValueError):
        return False


def event_sender_name(event: Event) -> str:
    metadata = event.metadata or {}
    direct = str(metadata.get("sender_name") or "").strip()
    if direct:
        return direct
    principal = metadata.get("principal") or {}
    return str(principal.get("display_name") or principal.get("name") or "").strip()


def choose_revoke_target(
    notice: RevokeNotice,
    occurred_at: datetime,
    events: Iterable[Event],
) -> Event | None:
    """Choose one plausible human message, preserving ambiguity as no match."""
    actor = " ".join(str(notice.actor_label or "").split()).casefold()
    candidates: dict[str, Event] = {}
    for event in events:
        if event.event_type not in HUMAN_SPEECH_TYPES:
            continue
        if "revokemsg" in str(event.content or "").casefold():
            continue
        if event_is_self(event) != notice.is_self:
            continue
        if not notice.is_self:
            sender = " ".join(event_sender_name(event).split()).casefold()
            if not sender or sender != actor:
                continue
        delta = occurred_at - event.occurred_at
        if abs(delta.total_seconds()) > _REVOKE_LOCAL_WINDOW.total_seconds():
            continue
        candidates[str(event.event_id)] = event
    return next(iter(candidates.values())) if len(candidates) == 1 else None


def _tag(body: str, name: str) -> str:
    match = re.search(
        rf"<{re.escape(name)}\b[^>]*>(.*?)</{re.escape(name)}>",
        body,
        re.I | re.S,
    )
    if match is None:
        return ""
    value = match.group(1).strip()
    if value.startswith("<![CDATA[") and value.endswith("]]>"):
        value = value[9:-3]
    return " ".join(html.unescape(value).split())


def parse_revoke_notice(content: object) -> RevokeNotice | None:
    """Return a revoke fact for a WeChat ``revokemsg`` row.

    The current payload exposes the notice text and ``revoketime``. It usually
    omits ``newmsgid``, which is why callers must treat an empty reference as
    unknown rather than inventing one.
    """
    raw = str(content or "")
    outer = _REVOKE_RE.search(raw)
    if outer is None:
        return None
    body = outer.group(1)
    notice = _tag(body, "content")
    if not notice:
        return None
    is_self = bool(re.match(r'^["“]?你["”]?\s*撤回', notice))
    actor_label = ""
    if not is_self:
        match = re.match(
            r'^["“]?(.+?)["”]?\s*撤回(?:了)?一条消息',
            notice,
        )
        if match is not None:
            actor_label = match.group(1).strip()
    return RevokeNotice(
        reference_external_msg_id=_tag(body, "newmsgid"),
        actor_label=actor_label,
        is_self=is_self,
        notice=notice,
        revoke_time=_tag(body, "revoketime"),
    )
