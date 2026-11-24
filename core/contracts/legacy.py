"""Adapter from the legacy message dict to the canonical MessageEvent.

The rest of the project currently passes ``repo.messages.to_dict(...)`` shaped
dictionaries through router/agent code.  This adapter is the single place that
knows how to translate that shape; new code never needs to inspect raw dicts.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Mapping

from core.contracts.events import (
    ChatType,
    ConversationRef,
    MentionRef,
    MessageEvent,
    MessageKind,
    observation_event_id,
)

from core.identity.platforms import chat_type_checked, chat_type_for
from safelog import warn

_KIND_BY_NAME: dict[str, MessageKind] = {
    "text": MessageKind.TEXT,
    "image": MessageKind.IMAGE,
    "sticker": MessageKind.STICKER,
    "voice": MessageKind.VOICE,
    "video": MessageKind.VIDEO,
    "file": MessageKind.FILE,
    "link": MessageKind.LINK,
    "location": MessageKind.LOCATION,
    "contact_card": MessageKind.CONTACT_CARD,
    "quote_reply": MessageKind.QUOTE_REPLY,
    "forward": MessageKind.FORWARD,
    "system": MessageKind.SYSTEM,
    "pat": MessageKind.PAT,
    "mini_program": MessageKind.MINI_PROGRAM,
    "transfer": MessageKind.TRANSFER,
}


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    raw = _as_text(value).strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        warn("core/contracts/legacy.py:60 ValueError", err=repr(__import__("sys").exc_info()[1]))
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            warn("core/contracts/legacy.py:65 ValueError", err=repr(__import__("sys").exc_info()[1]))
            continue
    return None


def _parse_mentions(raw: Any) -> tuple[MentionRef, ...]:
    if not raw:
        return ()
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            warn("core/contracts/legacy.py:76 json.JSONDecodeError", err=repr(__import__("sys").exc_info()[1]))
            return ()
    if not isinstance(raw, (list, tuple)):
        return ()
    mentions: list[MentionRef] = []
    for item in raw:
        if isinstance(item, Mapping):
            mentions.append(MentionRef(
                user_id=_as_text(item.get("user_id") or item.get("wxid") or ""),
                display_name=_as_text(item.get("user_name") or item.get("name") or ""),
                person_id=_as_text(item.get("person_id") or ""),
            ))
        elif item:
            mentions.append(MentionRef(user_id=_as_text(item)))
    return tuple(mentions)


def _chat_type(raw: Any) -> ChatType:
    value = _as_text(raw).strip().lower()
    if value == "group":
        return ChatType.GROUP
    if value == "private":
        return ChatType.PRIVATE
    if value == "channel":
        return ChatType.CHANNEL
    return ChatType.UNKNOWN


def _kind(msg: Mapping[str, Any]) -> MessageKind:
    name = _as_text(msg.get("msg_type_name") or "").strip().lower()
    if name in _KIND_BY_NAME:
        return _KIND_BY_NAME[name]
    raw_type = msg.get("msg_type")
    # Legacy numeric WeChat types, kept as a fallback for old payloads.
    numeric = {
        1: MessageKind.TEXT,
        3: MessageKind.IMAGE,
        34: MessageKind.VOICE,
        37: MessageKind.CONTACT_CARD,
        42: MessageKind.CONTACT_CARD,
        43: MessageKind.VIDEO,
        47: MessageKind.STICKER,
        48: MessageKind.LOCATION,
        49: MessageKind.LINK,
        50: MessageKind.VOICE,
        62: MessageKind.VIDEO,
        10000: MessageKind.SYSTEM,
        10002: MessageKind.SYSTEM,
    }
    try:
        return numeric.get(int(raw_type), MessageKind.UNKNOWN)
    except (TypeError, ValueError):
        warn("core/contracts/legacy.py:127 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
        return MessageKind.UNKNOWN


def _media_refs(msg: Mapping[str, Any]) -> tuple[str, ...]:
    refs = []
    for key in ("media_path", "media_url", "sticker_id", "link_url"):
        value = _as_text(msg.get(key))
        if value and value not in refs:
            refs.append(value)
    return tuple(refs)


def legacy_to_message_event(
    msg: Mapping[str, Any],
    *,
    platform: str = "",
    account_id: str = "",
    event_id: str = "",
    thread_id: str = "",
) -> MessageEvent:
    """Translate one legacy message dict into a canonical ``MessageEvent``."""
    platform = platform or _as_text(msg.get("source") or msg.get("platform")) or "unknown"
    account_id = account_id or _as_text(msg.get("account_id"))
    chat_id = _as_text(msg.get("chat_id") or msg.get("talker") or "")
    message_id = _as_text(msg.get("msg_id") or msg.get("id") or "")
    # 2026-10-10 GAP-2 + 2026-10-21：平台后缀只在 ``core.identity.platforms`` 定义一次。
    # 这里是它的**兼容兜底入口**：字段优先、形状兜底，冲突时平台自己说的算，
    # 但由 ``chat_type_checked`` 记一笔 ``chat_type_mismatch``（静默改判是这里最坏的错法）。
    chat_type = chat_type_checked(platform, msg.get("chat_type"), chat_id)
    if chat_type == ChatType.UNKNOWN:
        chat_type = ChatType.PRIVATE

    stable_id = observation_event_id(
        platform=platform,
        account_id=account_id,
        chat_id=chat_id,
        external_id=message_id,
    )
    return MessageEvent(
        event_id=event_id
        or stable_id
        or f"legacy:{platform}:{account_id or '-'}:{chat_id}:{uuid.uuid4().hex}",
        platform=platform,
        account_id=account_id,
        chat=ConversationRef(
            platform=platform,
            account_id=account_id,
            chat_id=chat_id,
            chat_type=chat_type,
            thread_id=_as_text(thread_id or msg.get("thread_id") or ""),
        ),
        message_id=message_id,
        sender_id=_as_text(msg.get("sender_id") or msg.get("sender_wxid") or ""),
        sender_name=_as_text(msg.get("sender_name") or ""),
        kind=_kind(msg),
        content=_as_text(msg.get("content") or ""),
        reply_to_id=_as_text(msg.get("reply_to") or msg.get("quote_reply_id") or ""),
        mentions=_parse_mentions(msg.get("mentions")),
        media_refs=_media_refs(msg),
        created_at=_parse_datetime(msg.get("created_at") or msg.get("timestamp")),
        raw=dict(msg),
    )
