from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from db.message_identity import global_message_id
from core.execution.provider import (
    InboundMessage,
    MediaInfo,
    MentionInfo,
    MiniProgramInfo,
    MsgType,
    Platform,
    PlatformTypeMapper,
    QuoteInfo,
    StickerInfo,
    LinkInfo,
    LocationInfo,
    ForwardInfo,
    TransferInfo,
)
from db.models import Message, MessageSource
from core.execution.platform_registry import all_platforms
from safelog import warn


def _build_platform_maps() -> tuple[dict[Platform, str], dict[str, Platform]]:
    p2s: dict[Platform, str] = {}
    for key, defn in all_platforms().items():
        try:
            p2s[Platform(key)] = defn.source
        except ValueError:
            pass  # noqa: silent（平台名探测：目录里出现未注册的平台属正常）
    s2p: dict[str, Platform] = {v: k for k, v in p2s.items()}
    return p2s, s2p


_PLATFORM_TO_SOURCE, _SOURCE_TO_PLATFORM = _build_platform_maps()

_EXTRA_STRUCT_KEYS = frozenset({
    "sticker_pack_name", "sticker_media",
    "link_description", "link_thumbnail_url",
    "location_address",
    "forward", "mini_program", "transfer",
    "is_self", "local_id",
})


def inbound_to_message(msg: InboundMessage) -> Message:
    extra_data: dict[str, Any] = dict(msg.extra)

    if msg.sticker.pack_name:
        extra_data["sticker_pack_name"] = msg.sticker.pack_name
    if msg.sticker.media.path or msg.sticker.media.url:
        extra_data["sticker_media"] = {
            "path": msg.sticker.media.path,
            "url": msg.sticker.media.url,
            "file_id": msg.sticker.media.file_id,
        }
    if msg.link.description:
        extra_data["link_description"] = msg.link.description
    if msg.link.thumbnail_url:
        extra_data["link_thumbnail_url"] = msg.link.thumbnail_url
    if msg.location.address:
        extra_data["location_address"] = msg.location.address
    if msg.forward.title or msg.forward.msg_count:
        extra_data["forward"] = {
            "title": msg.forward.title,
            "msg_count": msg.forward.msg_count,
            "summary": msg.forward.summary,
        }
    if msg.mini_program.app_id or msg.mini_program.title:
        extra_data["mini_program"] = {
            "app_id": msg.mini_program.app_id,
            "title": msg.mini_program.title,
            "description": msg.mini_program.description,
            "page_path": msg.mini_program.page_path,
            "thumbnail_url": msg.mini_program.thumbnail_url,
        }
    if msg.transfer.amount or msg.transfer.transaction_id:
        extra_data["transfer"] = {
            "amount": msg.transfer.amount,
            "currency": msg.transfer.currency,
            "transaction_id": msg.transfer.transaction_id,
            "status": msg.transfer.status,
        }

    extra_json = json.dumps(extra_data, ensure_ascii=False) if extra_data else None

    mentions_json = None
    if msg.mentions:
        mentions_json = json.dumps(
            [{"user_id": m.user_id, "user_name": m.user_name} for m in msg.mentions],
            ensure_ascii=False,
        )

    secondary_json = None
    if msg.secondary_types:
        secondary_json = json.dumps([t.value for t in msg.secondary_types])

    source = _PLATFORM_TO_SOURCE.get(msg.platform, msg.platform.value)

    try:
        msg_id_int = int(msg.msg_id) if msg.msg_id else 0
    except (ValueError, TypeError):
        warn("core/ingestion/message_bridge.py:105 (ValueError, TypeError)", err=repr(__import__("sys").exc_info()[1]))
        msg_id_int = 0

    try:
        reply_to_int = int(msg.reply_to) if msg.reply_to else None
    except (ValueError, TypeError):
        warn("core/ingestion/message_bridge.py:110 (ValueError, TypeError)", err=repr(__import__("sys").exc_info()[1]))
        reply_to_int = None

    account_id = msg.extra.get("account_id", "")
    global_id = global_message_id(
        msg.platform.value, account_id, msg.chat_id, msg.msg_id,
    )
    is_self = 1 if msg.extra.get("is_self") else 0
    local_id = msg.extra.get("local_id")
    if local_id is not None:
        try:
            local_id = int(local_id)
        except (ValueError, TypeError):
            warn("core/ingestion/message_bridge.py:122 (ValueError, TypeError)", err=repr(__import__("sys").exc_info()[1]))
            local_id = None

    return Message(
        id=global_id,
        msg_id=msg_id_int,
        local_id=local_id,
        sender_id=msg.user_id or None,
        sender_name=msg.user_name or None,
        chat_id=msg.chat_id or None,
        chat_type="group" if msg.is_group else "private",
        content=msg.content or None,
        msg_type=_resolve_raw_msg_type(msg),
        msg_sub_type=msg.extra.get("raw_sub_type"),
        msg_type_name=msg.msg_type.value,
        secondary_types=secondary_json,
        reply_to=reply_to_int,
        media_path=msg.media.path or None,
        media_url=msg.media.url or None,
        media_file_id=msg.media.file_id or None,
        media_mime=msg.media.mime_type or None,
        media_width=msg.media.width or None,
        media_height=msg.media.height or None,
        media_duration=msg.media.duration_seconds or None,
        sticker_id=msg.sticker.sticker_id or None,
        sticker_emoji=msg.sticker.emoji or None,
        link_url=msg.link.url or None,
        link_title=msg.link.title or None,
        location_lat=msg.location.latitude or None,
        location_lon=msg.location.longitude or None,
        location_name=msg.location.name or None,
        quote_reply_id=msg.quote.reply_to_id or None,
        quote_reply_user=msg.quote.reply_to_user or None,
        quote_reply_content=msg.quote.reply_content or None,
        mentions=mentions_json,
        created_at=msg.timestamp,
        is_self=is_self,
        source=source,
        extra=extra_json,
        synced_at=datetime.now(),
        account_id=account_id,
    )


def message_to_inbound(msg: Message) -> InboundMessage:
    platform = _SOURCE_TO_PLATFORM.get(msg.source or "", Platform.WECHAT)
    msg_type = MsgType(msg.msg_type_name) if msg.msg_type_name else MsgType.TEXT

    secondary_types: list[MsgType] = []
    if msg.secondary_types:
        try:
            for v in json.loads(msg.secondary_types):
                secondary_types.append(MsgType(v))
        except (json.JSONDecodeError, ValueError):
            warn("core/ingestion/message_bridge.py:175 (json.JSONDecodeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))

    mentions: list[MentionInfo] = []
    if msg.mentions:
        try:
            for m in json.loads(msg.mentions):
                mentions.append(MentionInfo(user_id=m.get("user_id", ""), user_name=m.get("user_name", "")))
        except (json.JSONDecodeError, ValueError):
            warn("core/ingestion/message_bridge.py:183 (json.JSONDecodeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))

    extra: dict[str, Any] = {}
    if msg.extra:
        try:
            extra = json.loads(msg.extra)
        except json.JSONDecodeError:
            warn("core/ingestion/message_bridge.py:190 json.JSONDecodeError", err=repr(__import__("sys").exc_info()[1]))

    sticker = StickerInfo(
        sticker_id=msg.sticker_id or "",
        emoji=msg.sticker_emoji or "",
    )
    if "sticker_pack_name" in extra:
        sticker.pack_name = extra["sticker_pack_name"]
    if "sticker_media" in extra:
        sm = extra["sticker_media"]
        sticker.media = MediaInfo(path=sm.get("path", ""), url=sm.get("url", ""), file_id=sm.get("file_id", ""))

    link = LinkInfo(
        url=msg.link_url or "",
        title=msg.link_title or "",
    )
    if "link_description" in extra:
        link.description = extra["link_description"]
    if "link_thumbnail_url" in extra:
        link.thumbnail_url = extra["link_thumbnail_url"]

    location = LocationInfo(
        latitude=msg.location_lat or 0.0,
        longitude=msg.location_lon or 0.0,
        name=msg.location_name or "",
    )
    if "location_address" in extra:
        location.address = extra["location_address"]

    quote = QuoteInfo(
        reply_to_id=msg.quote_reply_id or "",
        reply_to_user=msg.quote_reply_user or "",
        reply_content=msg.quote_reply_content or "",
    )

    forward = ForwardInfo()
    if "forward" in extra:
        fd = extra["forward"]
        forward = ForwardInfo(title=fd.get("title", ""), msg_count=fd.get("msg_count", 0), summary=fd.get("summary", ""))

    mini_program = MiniProgramInfo()
    if "mini_program" in extra:
        mp = extra["mini_program"]
        mini_program = MiniProgramInfo(
            app_id=mp.get("app_id", ""), title=mp.get("title", ""),
            description=mp.get("description", ""), page_path=mp.get("page_path", ""),
            thumbnail_url=mp.get("thumbnail_url", ""),
        )

    transfer = TransferInfo()
    if "transfer" in extra:
        tr = extra["transfer"]
        transfer = TransferInfo(
            amount=tr.get("amount", 0.0), currency=tr.get("currency", "CNY"),
            transaction_id=tr.get("transaction_id", ""), status=tr.get("status", ""),
        )

    clean_extra = {k: v for k, v in extra.items() if k not in _EXTRA_STRUCT_KEYS}
    if msg.local_id is not None:
        clean_extra["local_id"] = msg.local_id
    if msg.is_self:
        clean_extra["is_self"] = True
    if msg.msg_sub_type is not None:
        clean_extra["raw_sub_type"] = msg.msg_sub_type

    return InboundMessage(
        platform=platform,
        msg_id=str(msg.msg_id),
        chat_id=msg.chat_id or "",
        user_id=msg.sender_id or "",
        user_name=msg.sender_name or "",
        content=msg.content or "",
        msg_type=msg_type,
        secondary_types=secondary_types,
        is_group=(msg.chat_type == "group"),
        reply_to=str(msg.reply_to) if msg.reply_to else "",
        mentions=mentions,
        media=MediaInfo(
            path=msg.media_path or "", url=msg.media_url or "",
            file_id=msg.media_file_id or "", mime_type=msg.media_mime or "",
            width=msg.media_width or 0, height=msg.media_height or 0,
            duration_seconds=msg.media_duration or 0.0,
        ),
        sticker=sticker,
        link=link,
        location=location,
        quote=quote,
        forward=forward,
        mini_program=mini_program,
        transfer=transfer,
        timestamp=msg.created_at or datetime.now(),
        extra=clean_extra,
    )


def _resolve_raw_msg_type(msg: InboundMessage) -> int | None:
    if msg.platform == Platform.WECHAT:
        for (wx_type, _), mapped in PlatformTypeMapper._WECHAT_MAP.items():
            if mapped == msg.msg_type:
                return wx_type
    return None
