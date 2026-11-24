from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum
from typing import Any


class WxMsgType(IntEnum):
    TEXT = 1
    IMAGE = 3
    VOICE = 34
    FRIEND_CONFIRM = 37
    CONTACT_CARD = 42
    VIDEO = 43
    EMOJI = 47
    LOCATION = 48
    SHARE_LINK = 49
    VOIP = 50
    INIT_MSG = 51
    MINI_VIDEO = 62
    SYSTEM = 10000
    REVOKE = 10002
    PAT = 266287972401


class WxSubType(IntEnum):
    SHARE_DEFAULT = 0
    FILE = 6
    GIF = 8
    MERGE_FORWARD = 19
    QUOTE_REPLY_33 = 33
    QUOTE_REPLY_36 = 36
    QUOTE_REPLY_57 = 57
    VIDEO_ACCOUNT = 63
    OTHER_87 = 87
    VIDEO_LIVE = 88
    TRANSFER = 2000
    RED_PACKET_COVER = 2003


def normalize_local_type(raw_type: int, raw_sub_type: int = 0) -> tuple[int, int]:
    """把微信 4.x 的 local_type 归一化成 (msg_type, sub_type)。

    微信 4.x 的部分消息把真正的类型 ID 存成一个超过 32 位的复合值，
    例如拍一拍是 0x3E00000031（266287972401）。这类值本身就是一个
    已定义的 ``WxMsgType``，不能先拆低 32 位再查表，否则会被误判成
    49/62（视频号）并丢弃。
    """
    raw_type = int(raw_type or 0)
    raw_sub_type = int(raw_sub_type or 0)

    if raw_type == int(WxMsgType.PAT):
        return int(WxMsgType.PAT), 0

    if raw_type > 0xFFFFFFFF:
        return raw_type & 0xFFFFFFFF, raw_sub_type or ((raw_type >> 32) & 0xFFFF)

    return raw_type & 0xFFFFFFFF, raw_sub_type



PROCESSABLE_TYPES = {
    (WxMsgType.TEXT, 0),
    (WxMsgType.IMAGE, 0),
    (WxMsgType.EMOJI, 0),
    (WxMsgType.SHARE_LINK, WxSubType.SHARE_DEFAULT),
    (WxMsgType.SHARE_LINK, WxSubType.FILE),
    (WxMsgType.SHARE_LINK, WxSubType.MERGE_FORWARD),
    (WxMsgType.SHARE_LINK, WxSubType.QUOTE_REPLY_33),
    (WxMsgType.SHARE_LINK, WxSubType.QUOTE_REPLY_36),
    (WxMsgType.SHARE_LINK, WxSubType.QUOTE_REPLY_57),
    (WxMsgType.SHARE_LINK, WxSubType.OTHER_87),
    # 2026-11-02：转账通知进世界。判据是「能不能还原成一句人话」——
    # 群里的转账在微信里就是一条消息卡片，在场的人会看见，且能还原成
    # 「微信转账 / 收到转账3.20元」，没有理由让它比引用回复更不可见。
    # 看得见 ≠ 要开口：接不接由两扇闸门判，接了以后模型仍可 [SILENT]。
    (WxMsgType.SHARE_LINK, WxSubType.TRANSFER),
    (WxMsgType.SYSTEM, 0),
    (WxMsgType.SYSTEM, 4),
    (WxMsgType.PAT, 0),
}

EXTRACT_TEXT_TYPES = {
    (WxMsgType.SHARE_LINK, WxSubType.MERGE_FORWARD),
    (WxMsgType.SHARE_LINK, WxSubType.QUOTE_REPLY_33),
    (WxMsgType.SHARE_LINK, WxSubType.QUOTE_REPLY_36),
    (WxMsgType.SHARE_LINK, WxSubType.QUOTE_REPLY_57),
    (WxMsgType.SHARE_LINK, 0),
    (WxMsgType.SHARE_LINK, WxSubType.OTHER_87),
}

SKIP_TYPES = {
    WxMsgType.VOICE,
    WxMsgType.FRIEND_CONFIRM,
    WxMsgType.CONTACT_CARD,
    WxMsgType.VIDEO,
    WxMsgType.LOCATION,
    WxMsgType.VOIP,
    WxMsgType.INIT_MSG,
    WxMsgType.MINI_VIDEO,
    WxMsgType.REVOKE,
}


@dataclass
class WxMessage:
    local_id: int
    msg_svr_id: int
    type: int
    sub_type: int
    is_sender: int
    create_time: int
    str_talker: str
    str_content: str
    display_content: str = ""
    compress_content: bytes = b""
    bytes_extra: bytes = b""
    real_sender_id: str = ""

    @property
    def is_group(self) -> bool:
        return self.str_talker.endswith("@chatroom")

    @property
    def chat_type(self) -> str:
        return "group" if self.is_group else "private"

    @property
    def is_processable(self) -> bool:
        if self.type in SKIP_TYPES:
            return False
        return (self.type, self.sub_type) in PROCESSABLE_TYPES or self.type == WxMsgType.TEXT

    @property
    def needs_extract(self) -> bool:
        return (self.type, self.sub_type) in EXTRACT_TEXT_TYPES

    @property
    def created_at(self) -> datetime:
        return datetime.fromtimestamp(self.create_time)


@dataclass
class WxContact:
    username: str
    nickname: str = ""
    remark: str = ""
    alias: str = ""
    is_group: bool = False
    avatar_url: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.username.endswith("@chatroom"):
            self.is_group = True
