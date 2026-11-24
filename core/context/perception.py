# -*- coding: utf-8 -*-
"""①感知模块（批 A4）：入站消息 → ②原始消息账。

设计图口径：收消息/拍/@/引用；提谁/几点/原文。
这里只落**结构事实与原文**，不做语义判断（targets/genre 归 ③理解）；
同一 ``msg_id`` 幂等，②侧只保最近一段（``PERCEPTION_*`` 上限）。

闸门复用 GROUP_SCENE 作用域：off / 私聊 / 作用域外不建模型、不写事件，
接线前行为逐字节不变；shadow/on 都只记账（本模块不改任何对答）。
"""
from __future__ import annotations

import json
from typing import Any, Mapping
from safelog import warn

__all__ = ["perception_payload", "record_perception"]

_MEDIA_TYPES = frozenset({
    "image", "voice", "video", "file", "sticker", "link", "location",
    "contact_card", "forward", "mini_program", "voice_call", "transfer",
    "music", "video_channel", "moments_share", "poll",
})
_SYSTEM_TYPES = frozenset({"system", "group_notice"})
_MEDIA_HINT_KEYS = ("media_path", "media_url", "sticker_id", "link_url")
_MEDIA_KEYS = (
    "media_mime", "media_width", "media_height", "media_duration",
    "sticker_id", "sticker_emoji", "link_url", "link_title", "location_name",
)


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def _json(value: Any) -> Any:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return json.loads(value)
    except Exception:  # noqa: BLE001 - 坏 JSON 当没有，不猜
        warn("core/context/perception.py:40 Exception", err=repr(__import__("sys").exc_info()[1]))
        return None


def _mentions_of(msg: Mapping[str, Any]) -> tuple[str, ...]:
    out: list[str] = []

    def add(name: Any) -> None:
        text = _clean(name)
        if text and text not in out:
            out.append(text)

    def feed(raw: Any) -> None:
        if isinstance(raw, str):
            raw = _json(raw)
        if not isinstance(raw, (list, tuple)):
            return
        for item in raw:
            if isinstance(item, Mapping):
                add(item.get("user_name") or item.get("user_id"))
            else:
                add(item)

    feed(msg.get("mentions"))
    extra = _json(msg.get("extra"))
    if isinstance(extra, Mapping):
        feed(extra.get("mentioned_wxids"))
    return tuple(out)


def _media_of(msg: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in _MEDIA_KEYS:
        value = msg.get(key)
        if value is None or value == "" or value == 0:
            continue
        out[key] = value
    ref = _clean(
        msg.get("media_path") or msg.get("media_url") or msg.get("media_file_id")
    )
    if ref:
        out["ref"] = ref
    return out


def _kind_of(msg: Mapping[str, Any], content: str) -> str:
    from core.context.chat_window import is_pat_content, is_revoke_content

    msg_type = _clean(msg.get("msg_type_name")).lower()
    if msg_type == "pat" or is_pat_content(content):
        return "pat"
    if msg_type == "revoke" or is_revoke_content(content):
        return "revoke"
    if msg_type in _MEDIA_TYPES or any(msg.get(k) for k in _MEDIA_HINT_KEYS):
        return "media"
    if msg_type in _SYSTEM_TYPES:
        return "system"
    return "text"


def _at_of(msg: Mapping[str, Any]) -> str:
    raw = str(msg.get("created_at") or "")
    if not raw:
        return ""
    try:
        from core import temporal

        parsed = temporal.parse_datetime(raw)
    except Exception:  # noqa: BLE001 - 解析失败保留原始戳，不猜
        warn("core/context/perception.py:108 Exception", err=repr(__import__("sys").exc_info()[1]))
        parsed = None
    return parsed.strftime("%Y-%m-%d %H:%M") if parsed is not None else raw


def perception_payload(
    msg: Mapping[str, Any], *, account_id: str = "", platform: str = "",
) -> dict[str, Any]:
    """把一条入站消息（scheduler 的 msg 字典形状）折成①的原始事实。"""
    content = str(msg.get("content") or "")
    return {
        "who": _clean(msg.get("sender_name")) or _clean(msg.get("sender_id")),
        "who_id": _clean(msg.get("sender_id")),
        "text": content,
        "kind": _kind_of(msg, content),
        "msg_type": _clean(msg.get("msg_type_name")),
        "msg_id": _clean(msg.get("msg_id")),
        "reply_to": _clean(msg.get("quote_reply_id") or msg.get("reply_to")),
        "quote_user": _clean(msg.get("quote_reply_user")),
        "quote_text": str(msg.get("quote_reply_content") or ""),
        "mentions": _mentions_of(msg),
        "media": _media_of(msg),
        "platform": _clean(platform or msg.get("source")),
        "account_id": _clean(account_id),
        "at": _at_of(msg),
    }


def record_perception(
    msg: Mapping[str, Any], *, account_id: str = "", platform: str = "",
) -> bool:
    """①接线口：作用域内把一条入站消息记进②；返回是否新落一条。

    闸门 = ``scene_shadow_enabled``（GROUP_SCENE 非 off + 群聊 + 作用域），
    关/作用域外直接返回 False，不创建世界模型。同一 msg_id 幂等。
    """
    if not isinstance(msg, Mapping):
        return False
    chat_id = _clean(msg.get("chat_id"))
    if not chat_id:
        return False
    is_group = _clean(msg.get("chat_type")) == "group"
    from core.context.scene_read import scene_shadow_enabled

    if not scene_shadow_enabled(chat_id, is_group):
        return False
    from core.context.situation import note_perception
    from core.execution.data_channels import get_world

    model = get_world(chat_id)
    before = len(model.events)
    note_perception(model, **perception_payload(
        msg, account_id=account_id, platform=platform,
    ))
    return len(model.events) > before
