"""Canonical event and identity contracts.

These types describe *what happened* and *who was involved* without any
knowledge of WeChat, Discord or the current repository layout.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping
from safelog import warn


def _span_int(value: Any, default: int = -1) -> int:
    """把序列化来的偏移收敛成 int；None/垃圾 → ``default``（未知 span）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        warn("core/contracts/events.py:19 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
        return default


def observation_event_id(
    *,
    platform: str,
    account_id: str,
    chat_id: str,
    external_id: str,
) -> str:
    """平台观察的稳定事件 id：四元组缺一返回空串，不做随机兜底。

    重拉、重启、重复回调都必须得到同一个 id；返回空串表示「这条观察没有平台
    唯一键」，调用方要么自己定义采集端稳定键，要么明确按临时事件处理。
    不能拿正文 hash 顶替：同一句话重发是两次发生，不是同一条。
    """
    parts = [
        str(platform or ""),
        str(account_id or ""),
        str(chat_id or ""),
        str(external_id or ""),
    ]
    if not all(parts):
        return ""
    return "evt:" + hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:32]


class ChatType(StrEnum):
    PRIVATE = "private"
    GROUP = "group"
    CHANNEL = "channel"
    UNKNOWN = "unknown"


class MessageKind(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    STICKER = "sticker"
    VOICE = "voice"
    VIDEO = "video"
    FILE = "file"
    LINK = "link"
    LOCATION = "location"
    CONTACT_CARD = "contact_card"
    QUOTE_REPLY = "quote_reply"
    FORWARD = "forward"
    SYSTEM = "system"
    PAT = "pat"
    MINI_PROGRAM = "mini_program"
    TRANSFER = "transfer"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ConversationRef:
    """Stable address of a conversation."""

    platform: str
    account_id: str
    chat_id: str
    chat_type: ChatType = ChatType.UNKNOWN
    thread_id: str = ""

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.platform, self.account_id, self.chat_id)

    @property
    def is_group(self) -> bool:
        return self.chat_type == ChatType.GROUP

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "account_id": self.account_id,
            "chat_id": self.chat_id,
            "chat_type": self.chat_type.value,
            "thread_id": self.thread_id,
        }


@dataclass(frozen=True)
class PersonRef:
    """Stable identity of one person inside one platform account."""

    platform: str
    account_id: str
    person_id: str

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.platform, self.account_id, self.person_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "account_id": self.account_id,
            "person_id": self.person_id,
        }


@dataclass(frozen=True)
class MentionRef:
    """One explicit @-mention, if the platform exposes one."""

    user_id: str = ""
    display_name: str = ""
    person_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "display_name": self.display_name,
            "person_id": self.person_id,
        }


@dataclass(frozen=True)
class EvidenceRef:
    """Pointer back to the raw evidence behind a conclusion.

    ``quoted_text`` is a *rendering* of the evidence and may be truncated
    (``MessageEvent.evidence_ref`` keeps the first 200 chars).  ``start`` /
    ``end`` are the optional half-open offsets of the **full evidence region**
    inside the original text addressed by ``message_id``; ``-1`` means
    "not recorded" (data written before spans existed).  With a span the
    original is always one slice away: read the message by ``message_id`` and
    take ``text[start:end]`` -- even when ``quoted_text`` was cut short.
    """

    source: str
    message_id: str
    account_id: str = ""
    chat_id: str = ""
    quoted_text: str = ""
    start: int = -1
    end: int = -1

    def __post_init__(self) -> None:
        start, end = _span_int(self.start), _span_int(self.end)
        if start < 0 or end < start:
            start, end = -1, -1
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)

    @property
    def has_span(self) -> bool:
        return self.start >= 0 and self.end > self.start

    @property
    def full_span(self) -> tuple[int, int] | None:
        """整个证据区间的偏移（可能比 ``quoted_text`` 长）。"""
        return (self.start, self.end) if self.has_span else None

    @property
    def quote_span(self) -> tuple[int, int] | None:
        """已存下的 ``quoted_text`` 自身落在原文的哪一段。"""
        if not self.has_span:
            return None
        return (self.start, min(self.end, self.start + len(self.quoted_text)))

    @property
    def truncated(self) -> bool:
        """``quoted_text`` 比它指向的区间短（截断过）。"""
        return self.has_span and (self.end - self.start) > len(self.quoted_text)

    def with_span(self, start: int, end: int) -> "EvidenceRef":
        """同一份证据换个 span（读时透镜用，不改原文）。"""
        return replace(self, start=start, end=end)

    def slice_from(self, text: str) -> str:
        """从原文取回证据；没有 span / 原文为空时退回 ``quoted_text``。"""
        if not self.has_span or not text:
            return self.quoted_text
        return text[self.start:self.end]

    def to_dict(self) -> dict[str, Any]:
        # 无 span 时与旧序列化逐字一致（不多这两个键）。
        out = {
            "source": self.source,
            "message_id": self.message_id,
            "account_id": self.account_id,
            "chat_id": self.chat_id,
            "quoted_text": self.quoted_text,
        }
        if self.has_span:
            out["start"] = self.start
            out["end"] = self.end
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "EvidenceRef":
        """旧数据（无 start/end）与新数据（有 span）都能读。"""
        payload = dict(data or {})
        return cls(
            source=str(payload.get("source") or ""),
            message_id=str(payload.get("message_id") or ""),
            account_id=str(payload.get("account_id") or ""),
            chat_id=str(payload.get("chat_id") or ""),
            quoted_text=str(payload.get("quoted_text") or ""),
            start=_span_int(payload.get("start")),
            end=_span_int(payload.get("end")),
        )


@dataclass(frozen=True)
class MessageEvent:
    """Platform-neutral inbound event.

    ``raw`` keeps the adapter payload for audit/debugging, but decision code
    must only rely on the typed fields above so platform coupling cannot leak
    upwards.
    """

    event_id: str
    platform: str
    account_id: str
    chat: ConversationRef
    message_id: str
    sender_id: str = ""
    sender_name: str = ""
    kind: MessageKind = MessageKind.UNKNOWN
    content: str = ""
    reply_to_id: str = ""
    mentions: tuple[MentionRef, ...] = ()
    media_refs: tuple[str, ...] = ()
    created_at: datetime | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def is_group(self) -> bool:
        return self.chat.is_group

    @property
    def conversation_key(self) -> tuple[str, str, str]:
        return self.chat.key

    def evidence_ref(self) -> EvidenceRef:
        content = self.content or ""
        # quoted_text 仍是 200 字截断（既有行为）；start/end 指向原文完整区间，
        # 于是截断后也能靠 message_id + [start:end] 回到原文。
        return EvidenceRef(
            source=self.platform,
            message_id=self.message_id,
            account_id=self.account_id,
            chat_id=self.chat.chat_id,
            quoted_text=content[:200],
            start=0 if content else -1,
            end=len(content),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "platform": self.platform,
            "account_id": self.account_id,
            "message_id": self.message_id,
            "sender_id": self.sender_id,
            "sender_name": self.sender_name,
            "kind": self.kind.value,
            "content": self.content,
            "reply_to_id": self.reply_to_id,
            "mentions": [m.to_dict() for m in self.mentions],
            "media_refs": list(self.media_refs),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "conversation": self.chat.to_dict(),
        }

    @classmethod
    def new(
        cls,
        *,
        platform: str,
        account_id: str,
        chat_id: str,
        chat_type: ChatType,
        message_id: str,
        event_id: str = "",
        **kwargs: Any,
    ) -> "MessageEvent":
        import uuid

        return cls(
            event_id=event_id
            or observation_event_id(
                platform=platform,
                account_id=account_id,
                chat_id=chat_id,
                external_id=message_id,
            )
            or uuid.uuid4().hex,
            platform=platform,
            account_id=account_id,
            chat=ConversationRef(
                platform=platform,
                account_id=account_id,
                chat_id=chat_id,
                chat_type=chat_type,
            ),
            message_id=message_id,
            **kwargs,
        )
