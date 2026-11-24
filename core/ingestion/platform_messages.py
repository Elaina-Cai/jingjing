from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

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
from safelog import warn


def _strip_wxid_prefix(xml_str: str) -> str:
    xml_start = xml_str.find("<?xml")
    if xml_start > 0:
        return xml_str[xml_start:]
    return xml_str


def _clean_quote_content(raw: str) -> str:
    """引用内容清洗：唯一 owner = core.conversation.quote_text（2026-10-14 修）。"""
    from core.conversation.quote_text import clean_quote_text

    return clean_quote_text(raw)


class PlatformMessage(ABC):
    platform: Platform

    @abstractmethod
    def to_inbound(self) -> InboundMessage: ...

    @classmethod
    @abstractmethod
    def from_raw(cls, raw: Any) -> PlatformMessage: ...


@dataclass
class WechatMessage(PlatformMessage):
    platform: Platform = field(default=Platform.WECHAT, init=False)
    msg_svr_id: int = 0
    local_id: int = 0
    type: int = 0
    sub_type: int = 0
    is_sender: int = 0
    create_time: int = 0
    str_talker: str = ""
    str_content: str = ""
    compress_content: bytes = b""
    bytes_extra: bytes = b""
    real_sender_id: str = ""
    sender_wxid: str = ""
    sender_name: str = ""
    extracted_content: str = ""
    media_path: str = ""
    emoji_md5: str = ""
    emoji_url: str = ""
    link_url: str = ""
    link_title: str = ""
    mentioned_wxids: list[str] = field(default_factory=list)
    voice_transcribed: bool = False
    voice_text: str = ""

    @property
    def msg_type(self) -> MsgType:
        return PlatformTypeMapper.from_wechat(self.type, self.sub_type)

    @property
    def is_group(self) -> bool:
        return self.str_talker.endswith("@chatroom")

    def to_inbound(self) -> InboundMessage:
        content = self.extracted_content
        if self.msg_type == MsgType.VOICE and self.voice_transcribed and self.voice_text:
            content = f"[语音] {self.voice_text}"

        extra: dict[str, Any] = {}
        if self.is_sender:
            extra["is_self"] = True
        if self.local_id:
            extra["local_id"] = self.local_id
        if self.sub_type:
            extra["raw_sub_type"] = self.sub_type
        if self.type == WxMsgType.EMOJI and self.emoji_url:
            extra["emoji_url"] = self.emoji_url
        if self.voice_transcribed:
            extra["voice_transcribed"] = True

        msg = InboundMessage(
            platform=Platform.WECHAT,
            msg_id=str(self.msg_svr_id),
            chat_id=self.str_talker,
            user_id=self.sender_wxid or self.real_sender_id,
            user_name=self.sender_name,
            content=content,
            msg_type=self.msg_type,
            is_group=self.is_group,
            mentions=[MentionInfo(user_id=wxid) for wxid in self.mentioned_wxids],
            timestamp=self._created_at,
            extra=extra,
        )

        if self.media_path or self.emoji_url:
            if self.msg_type == MsgType.STICKER:
                msg.sticker = StickerInfo(
                    sticker_id=self.emoji_md5,
                    media=MediaInfo(path=self.media_path, url=self.emoji_url),
                )
            else:
                msg.media = MediaInfo(path=self.media_path, url=self.emoji_url)

        if self.msg_type == MsgType.LINK and self.link_url:
            msg.link = LinkInfo(url=self.link_url, title=self.link_title)

        if self.msg_type == MsgType.QUOTE_REPLY:
            msg.quote = self._parse_quote_reply()

        if self.msg_type == MsgType.FORWARD:
            msg.forward = self._parse_forward()

        if self.msg_type == MsgType.MINI_PROGRAM:
            msg.mini_program = self._parse_mini_program()

        if self.msg_type == MsgType.TRANSFER:
            msg.transfer = self._parse_transfer()

        if self.msg_type == MsgType.LOCATION:
            msg.location = self._parse_location()

        return msg

    @property
    def _created_at(self):
        from datetime import datetime
        return datetime.fromtimestamp(self.create_time) if self.create_time else datetime.now()

    def _parse_quote_reply(self) -> QuoteInfo:
        try:
            import xml.etree.ElementTree as ET
            from db.content_extract import _decompress_xml
            xml_str = _decompress_xml(self)
            if not xml_str:
                return QuoteInfo()
            xml_str = _strip_wxid_prefix(xml_str)
            root = ET.fromstring(xml_str)
            ref = root.find(".//refermsg")
            if ref is not None:
                raw_content = ref.findtext("content", "")
                reply_content = _clean_quote_content(raw_content)
                return QuoteInfo(
                    reply_to_id=ref.findtext("svrid", ""),
                    reply_to_user=ref.findtext("fromusr", ""),
                    reply_content=reply_content,
                )
        except Exception as _:
            warn("core/ingestion/platform_messages.py:166 Exception", err=repr(_))
        return QuoteInfo()

    def _parse_forward(self) -> ForwardInfo:
        try:
            import xml.etree.ElementTree as ET
            from db.content_extract import _decompress_xml
            xml_str = _decompress_xml(self)
            if not xml_str:
                return ForwardInfo()
            xml_str = _strip_wxid_prefix(xml_str)
            root = ET.fromstring(xml_str)
            title = root.get("title", "")
            record = root.findall(".//record")
            return ForwardInfo(title=title, msg_count=len(record))
        except Exception as _:
            warn("core/ingestion/platform_messages.py:182 Exception", err=repr(_))
        return ForwardInfo()

    def _parse_mini_program(self) -> MiniProgramInfo:
        try:
            import xml.etree.ElementTree as ET
            from db.content_extract import _decompress_xml
            xml_str = _decompress_xml(self)
            if not xml_str:
                return MiniProgramInfo()
            xml_str = _strip_wxid_prefix(xml_str)
            root = ET.fromstring(xml_str)
            appmsg = root.find(".//appmsg")
            if appmsg is None:
                appmsg = root
            return MiniProgramInfo(
                app_id=appmsg.get("appid", ""),
                title=appmsg.findtext("title", ""),
                description=appmsg.findtext("des", ""),
                page_path=appmsg.get("pagepath", ""),
                thumbnail_url=appmsg.findtext("thumburl", ""),
            )
        except Exception as _:
            warn("core/ingestion/platform_messages.py:205 Exception", err=repr(_))
        return MiniProgramInfo()

    def _parse_transfer(self) -> TransferInfo:
        try:
            import xml.etree.ElementTree as ET
            raw = self.str_content or self.compress_content or b""
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            root = ET.fromstring(raw)
            return TransferInfo(
                amount=float(root.get("feeds", {}).get("amount", 0)),
                transaction_id=root.get("feeds", {}).get("transaction_id", ""),
                status=root.get("status", ""),
            )
        except Exception as _:
            warn("core/ingestion/platform_messages.py:221 Exception", err=repr(_))
        return TransferInfo()

    def _parse_location(self) -> LocationInfo:
        try:
            import xml.etree.ElementTree as ET
            raw = self.str_content or self.compress_content or b""
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            root = ET.fromstring(raw)
            return LocationInfo(
                latitude=float(root.get("lat", 0)),
                longitude=float(root.get("lng", 0)),
                name=root.get("poiname", ""),
                address=root.get("label", ""),
            )
        except Exception as _:
            warn("core/ingestion/platform_messages.py:238 Exception", err=repr(_))
        return LocationInfo()

    @classmethod
    def from_raw(cls, raw: Any) -> WechatMessage:
        from db.wx_schema import WxMessage
        if isinstance(raw, WxMessage):
            return cls(
                msg_svr_id=raw.msg_svr_id,
                local_id=raw.local_id,
                type=raw.type,
                sub_type=raw.sub_type,
                is_sender=raw.is_sender,
                create_time=raw.create_time,
                str_talker=raw.str_talker,
                str_content=raw.str_content,
                compress_content=raw.compress_content,
                bytes_extra=raw.bytes_extra,
                real_sender_id=raw.real_sender_id,
            )
        if isinstance(raw, dict):
            return cls(**{k: v for k, v in raw.items() if k in cls.__dataclass_fields__})
        raise TypeError(f"Cannot construct WechatMessage from {type(raw)}")


class WxMsgType:
    TEXT = 1
    IMAGE = 3
    VOICE = 34
    CONTACT_CARD = 42
    VIDEO = 43
    EMOJI = 47
    LOCATION = 48
    SHARE_LINK = 49
    VOIP = 50
    MINI_VIDEO = 62
    SYSTEM = 10000
    REVOKE = 10002


@dataclass
class QQMessage(PlatformMessage):
    platform: Platform = field(default=Platform.QQ, init=False)
    msg_id: str = ""
    seq: int = 0
    group_id: str = ""
    user_id: str = ""
    user_name: str = ""
    content: str = ""
    raw_type: str = "text"
    at_users: list[dict[str, str]] = field(default_factory=list)
    reply_seq: int = 0
    reply_content: str = ""
    media_url: str = ""
    media_file_id: str = ""
    face_id: str = ""
    json_data: str = ""
    timestamp: int = 0

    @property
    def msg_type(self) -> MsgType:
        return PlatformTypeMapper.from_qq(self.raw_type)

    @property
    def is_group(self) -> bool:
        return bool(self.group_id)

    def to_inbound(self) -> InboundMessage:
        from datetime import datetime

        extra: dict[str, Any] = {}
        if self.seq:
            extra["seq"] = self.seq
        if self.json_data:
            extra["json_data"] = self.json_data
        # 群/私聊都可能是纯数字 id，chat_id 加类型前缀，发送侧（QQProvider）
        # 才能决定调 send_group_msg 还是 send_private_msg。
        if self.group_id:
            extra["group_id"] = self.group_id
            chat_id = f"group:{self.group_id}"
        else:
            extra["user_id"] = self.user_id
            chat_id = f"user:{self.user_id}"
        extra["message_type"] = "group" if self.is_group else "private"

        msg = InboundMessage(
            platform=Platform.QQ,
            msg_id=self.msg_id or str(self.seq),
            chat_id=chat_id,
            user_id=self.user_id,
            user_name=self.user_name,
            content=self.content,
            msg_type=self.msg_type,
            is_group=self.is_group,
            mentions=[MentionInfo(user_id=a.get("qq", ""), user_name=a.get("nick", "")) for a in self.at_users],
            timestamp=datetime.fromtimestamp(self.timestamp) if self.timestamp else datetime.now(),
            extra=extra,
        )

        if self.media_url or self.media_file_id:
            msg.media = MediaInfo(url=self.media_url, file_id=self.media_file_id)

        if self.msg_type == MsgType.STICKER and self.face_id:
            msg.sticker = StickerInfo(sticker_id=self.face_id)

        if self.msg_type == MsgType.QUOTE_REPLY and self.reply_seq:
            msg.quote = QuoteInfo(
                reply_to_id=str(self.reply_seq),
                reply_content=self.reply_content,
            )

        if self.msg_type == MsgType.LINK and self.json_data:
            msg.link = self._parse_json_link()

        return msg

    def _parse_json_link(self) -> LinkInfo:
        try:
            import json
            data = json.loads(self.json_data)
            meta = data.get("meta", {})
            news = meta.get("news", {}) or meta.get("detail", {})
            return LinkInfo(
                url=news.get("jumpUrl", ""),
                title=news.get("title", ""),
                description=news.get("desc", ""),
                thumbnail_url=news.get("cover", ""),
            )
        except Exception as _:
            warn("core/ingestion/platform_messages.py:367 Exception", err=repr(_))
            return LinkInfo()

    @classmethod
    def from_raw(cls, raw: Any) -> QQMessage:
        if isinstance(raw, dict):
            return cls(**{k: v for k, v in raw.items() if k in cls.__dataclass_fields__})
        raise TypeError(f"Cannot construct QQMessage from {type(raw)}")


@dataclass
class TelegramMessage(PlatformMessage):
    platform: Platform = field(default=Platform.TELEGRAM, init=False)
    update_id: int = 0
    message_id: int = 0
    chat_id: str = ""
    user_id: str = ""
    user_name: str = ""
    first_name: str = ""
    content: str = ""
    raw_type: str = "text"
    reply_to_message_id: int = 0
    forward_from_id: str = ""
    entities: list[dict[str, Any]] = field(default_factory=list)
    photo_file_id: str = ""
    voice_file_id: str = ""
    video_file_id: str = ""
    document_file_id: str = ""
    sticker_file_id: str = ""
    sticker_emoji: str = ""
    sticker_set_name: str = ""
    audio_file_id: str = ""
    latitude: float = 0.0
    longitude: float = 0.0
    venue_title: str = ""
    venue_address: str = ""
    contact_name: str = ""
    contact_phone: str = ""
    timestamp: int = 0

    @property
    def msg_type(self) -> MsgType:
        return PlatformTypeMapper.from_telegram(self.raw_type)

    def to_inbound(self) -> InboundMessage:
        from datetime import datetime

        extra: dict[str, Any] = {}
        if self.update_id:
            extra["update_id"] = self.update_id
        if self.forward_from_id:
            extra["forward_from_id"] = self.forward_from_id

        display_name = self.user_name or self.first_name

        msg = InboundMessage(
            platform=Platform.TELEGRAM,
            msg_id=str(self.message_id),
            chat_id=self.chat_id,
            user_id=self.user_id,
            user_name=display_name,
            content=self.content,
            msg_type=self.msg_type,
            is_group=self.chat_id.startswith("-"),
            mentions=self._parse_mentions(),
            timestamp=datetime.fromtimestamp(self.timestamp) if self.timestamp else datetime.now(),
            extra=extra,
        )

        if self.photo_file_id:
            msg.media = MediaInfo(file_id=self.photo_file_id, mime_type="image/jpeg")
        elif self.voice_file_id:
            msg.media = MediaInfo(file_id=self.voice_file_id, mime_type="audio/ogg")
        elif self.video_file_id:
            msg.media = MediaInfo(file_id=self.video_file_id, mime_type="video/mp4")
        elif self.document_file_id:
            msg.media = MediaInfo(file_id=self.document_file_id)
        elif self.audio_file_id:
            msg.media = MediaInfo(file_id=self.audio_file_id, mime_type="audio/mpeg")

        if self.sticker_file_id:
            msg.sticker = StickerInfo(
                sticker_id=self.sticker_file_id,
                emoji=self.sticker_emoji,
                pack_name=self.sticker_set_name,
            )

        if self.msg_type == MsgType.QUOTE_REPLY and self.reply_to_message_id:
            msg.quote = QuoteInfo(reply_to_id=str(self.reply_to_message_id))

        if self.latitude:
            msg.location = LocationInfo(
                latitude=self.latitude,
                longitude=self.longitude,
                name=self.venue_title,
                address=self.venue_address,
            )

        if self.msg_type == MsgType.CONTACT_CARD and self.contact_name:
            msg.extra["contact_name"] = self.contact_name
            msg.extra["contact_phone"] = self.contact_phone

        return msg

    def _parse_mentions(self) -> list[MentionInfo]:
        result = []
        for ent in self.entities:
            if ent.get("type") == "mention" and "user" in ent:
                user = ent["user"]
                result.append(MentionInfo(
                    user_id=str(user.get("id", "")),
                    user_name=user.get("username", "") or user.get("first_name", ""),
                ))
        return result

    @classmethod
    def from_raw(cls, raw: Any) -> TelegramMessage:
        if isinstance(raw, dict):
            return cls(**{k: v for k, v in raw.items() if k in cls.__dataclass_fields__})
        raise TypeError(f"Cannot construct TelegramMessage from {type(raw)}")


_REGISTRY: dict[Platform, type[PlatformMessage]] = {
    Platform.WECHAT: WechatMessage,
    Platform.QQ: QQMessage,
    Platform.TELEGRAM: TelegramMessage,
}


def get_platform_message_class(platform: Platform) -> type[PlatformMessage]:
    cls = _REGISTRY.get(platform)
    if cls is None:
        raise ValueError(f"No PlatformMessage class registered for {platform.value}")
    return cls


def adapt(raw: Any, platform: Platform) -> InboundMessage:
    cls = get_platform_message_class(platform)
    platform_msg = cls.from_raw(raw)
    return platform_msg.to_inbound()
