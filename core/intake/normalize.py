"""任意输入 -> Observation。纯函数，不查库、不调模型。"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from core.intake.observation import (
    FORM_MEDIA,
    FORM_QUOTE,
    FORM_SYSTEM,
    FORM_TEXT,
    TEXT_MAX_CHARS,
    Observation,
    WHERE_GROUP,
    WHERE_PRIVATE,
)

log = logging.getLogger(__name__)

_GROUP_HINTS = ("group", "chatroom", "@chatroom", "channel")


def _get(src: Any, *names: str, default: Any = "") -> Any:
    for name in names:
        if isinstance(src, Mapping) and name in src and src[name] not in (None, ""):
            return src[name]
        value = getattr(src, name, None)
        if value not in (None, ""):
            return value
    return default


def _where(chat_id: str, chat_type: str) -> str:
    kind = str(chat_type or "").strip().lower()
    if kind.startswith("group") or kind in ("chatroom", "channel"):
        return WHERE_GROUP
    if kind.startswith("private"):
        return WHERE_PRIVATE
    low = str(chat_id or "").lower()
    return WHERE_GROUP if any(h in low for h in _GROUP_HINTS) else WHERE_PRIVATE


def _media(raw: Any) -> tuple[dict, ...]:
    if not raw:
        return ()
    out: list[dict] = []
    items = raw if isinstance(raw, (list, tuple)) else (raw,)
    for item in items:
        if isinstance(item, Mapping):
            out.append({str(k): v for k, v in item.items() if isinstance(k, str)})
        elif isinstance(item, str) and item.strip():
            out.append({"kind": "file", "name": item.strip()})
    return tuple(out[:4])


def _quoted(raw: Any) -> dict:
    if isinstance(raw, Mapping):
        return {"who": str(raw.get("who") or raw.get("by") or ""), "text": str(raw.get("text") or "")[:300]}
    return {}


def normalize(src: Any) -> Observation:
    """把 MessageEvent / dict / 平台行归一成 Observation（坏输入不抛，降级成空观察）。"""
    try:
        chat_id = str(_get(src, "chat_id", "conversation_id", "chat", default="") or "")
        chat_type = str(_get(src, "chat_type", "type", "kind", default="") or "")
        text = str(_get(src, "text", "content", "body", default="") or "")
        media = _media(_get(src, "media", "attachments", default=None))
        quoted = _quoted(_get(src, "quoted", "quote", "reply_to", default=None))
        system = _get(src, "system", default=None)
        mention = bool(_get(src, "mention_to_me", "mentioned", default=False))
        by = str(_get(src, "sender_name", "by", "display_name", default="") or "")
        by_id = str(_get(src, "sender_id", "by_id", "from", default="") or "")
        at = str(_get(src, "at", "ts", "occurred_at", default="") or "")
    except Exception as exc:
        log.warning("intake_normalize_failed error=%s", exc)
        return Observation()

    if system:
        form = FORM_SYSTEM
    elif media and not text.strip():
        form = FORM_MEDIA
    elif quoted and text.strip():
        form = FORM_QUOTE
    else:
        form = FORM_TEXT

    return Observation(
        where=_where(chat_id, chat_type),
        chat_id=chat_id,
        by=by,
        by_id=by_id,
        form=form,
        text=text[:TEXT_MAX_CHARS],
        media=media,
        quoted=quoted,
        mention_to_me=mention,
        mention_by=str(_get(src, "mention_by", default="") or ""),
        system={str(k): v for k, v in system.items()} if isinstance(system, Mapping) else ({"kind": str(system)} if system else {}),
        at=at,
    )
