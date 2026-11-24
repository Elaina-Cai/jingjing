from __future__ import annotations

import structlog
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, AsyncIterator, Callable
from safelog import warn

log = structlog.get_logger()


class Platform(Enum):
    WECHAT = "wechat"
    QQ = "qq"
    TELEGRAM = "telegram"
    DISCORD = "discord"
    WEB = "web"


class MsgType(Enum):
    TEXT = "text"
    IMAGE = "image"
    VOICE = "voice"
    VIDEO = "video"
    FILE = "file"
    STICKER = "sticker"
    LINK = "link"
    LOCATION = "location"
    CONTACT_CARD = "contact_card"
    QUOTE_REPLY = "quote_reply"
    FORWARD = "forward"
    SYSTEM = "system"
    MINI_PROGRAM = "mini_program"
    VOICE_CALL = "voice_call"
    TRANSFER = "transfer"
    MUSIC = "music"
    VIDEO_CHANNEL = "video_channel"
    MOMENTS_SHARE = "moments_share"
    GROUP_NOTICE = "group_notice"
    POLL = "poll"
    PAT = "pat"
    REVOKE = "revoke"


@dataclass
class MediaInfo:
    path: str = ""
    url: str = ""
    file_id: str = ""
    mime_type: str = ""
    file_name: str = ""
    file_size: int = 0
    width: int = 0
    height: int = 0
    duration_seconds: float = 0.0


@dataclass
class StickerInfo:
    sticker_id: str = ""
    pack_name: str = ""
    emoji: str = ""
    media: MediaInfo = field(default_factory=MediaInfo)


@dataclass
class LinkInfo:
    url: str = ""
    title: str = ""
    description: str = ""
    thumbnail_url: str = ""


@dataclass
class LocationInfo:
    latitude: float = 0.0
    longitude: float = 0.0
    name: str = ""
    address: str = ""


@dataclass
class QuoteInfo:
    reply_to_id: str = ""
    reply_to_user: str = ""
    reply_content: str = ""


@dataclass
class ForwardInfo:
    title: str = ""
    msg_count: int = 0
    summary: str = ""


@dataclass
class MiniProgramInfo:
    app_id: str = ""
    title: str = ""
    description: str = ""
    page_path: str = ""
    thumbnail_url: str = ""


@dataclass
class TransferInfo:
    amount: float = 0.0
    currency: str = "CNY"
    transaction_id: str = ""
    status: str = ""


@dataclass
class MentionInfo:
    user_id: str = ""
    user_name: str = ""


_MSG_TYPE_INFO_MAP: dict[MsgType, str] = {
    MsgType.TEXT: "text",
    MsgType.IMAGE: "media",
    MsgType.VOICE: "media",
    MsgType.VIDEO: "media",
    MsgType.FILE: "media",
    MsgType.STICKER: "sticker",
    MsgType.LINK: "link",
    MsgType.LOCATION: "location",
    MsgType.CONTACT_CARD: "contact",
    MsgType.QUOTE_REPLY: "quote",
    MsgType.FORWARD: "forward",
    MsgType.SYSTEM: "system",
    MsgType.MINI_PROGRAM: "link",
    MsgType.VOICE_CALL: "call",
    MsgType.TRANSFER: "transfer",
    MsgType.MUSIC: "link",
    MsgType.VIDEO_CHANNEL: "link",
    MsgType.MOMENTS_SHARE: "link",
    MsgType.GROUP_NOTICE: "system",
    MsgType.POLL: "interactive",
    MsgType.PAT: "interactive",
    MsgType.REVOKE: "system",
}


@dataclass
class InboundMessage:
    platform: Platform
    msg_id: str = ""
    chat_id: str = ""
    user_id: str = ""
    user_name: str = ""
    content: str = ""
    msg_type: MsgType = MsgType.TEXT
    secondary_types: list[MsgType] = field(default_factory=list)
    is_group: bool = False
    reply_to: str = ""
    mentions: list[MentionInfo] = field(default_factory=list)
    media: MediaInfo = field(default_factory=MediaInfo)
    sticker: StickerInfo = field(default_factory=StickerInfo)
    link: LinkInfo = field(default_factory=LinkInfo)
    location: LocationInfo = field(default_factory=LocationInfo)
    quote: QuoteInfo = field(default_factory=QuoteInfo)
    forward: ForwardInfo = field(default_factory=ForwardInfo)
    mini_program: MiniProgramInfo = field(default_factory=MiniProgramInfo)
    transfer: TransferInfo = field(default_factory=TransferInfo)
    timestamp: datetime = field(default_factory=datetime.now)
    extra: dict[str, Any] = field(default_factory=dict)
    account_id: str = ""

    @property
    def is_text(self) -> bool:
        return self.msg_type == MsgType.TEXT

    @property
    def is_media(self) -> bool:
        return _MSG_TYPE_INFO_MAP.get(self.msg_type) == "media"

    @property
    def has_link(self) -> bool:
        return self.msg_type == MsgType.LINK or bool(self.link.url)

    def to_storage_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "platform": self.platform.value,
            "msg_id": self.msg_id,
            "chat_id": self.chat_id,
            "user_id": self.user_id,
            "user_name": self.user_name,
            "content": self.content,
            "msg_type": self.msg_type.value,
            "is_group": self.is_group,
            "reply_to": self.reply_to,
            "timestamp": self.timestamp.isoformat() if self.timestamp else "",
            "account_id": self.account_id,
        }
        if self.secondary_types:
            d["secondary_types"] = [t.value for t in self.secondary_types]
        if self.mentions:
            d["mentions"] = [{"user_id": m.user_id, "user_name": m.user_name} for m in self.mentions]
        if self.media.path or self.media.url or self.media.file_id:
            d["media"] = {
                "path": self.media.path, "url": self.media.url,
                "file_id": self.media.file_id, "mime_type": self.media.mime_type,
                "file_name": self.media.file_name, "file_size": self.media.file_size,
                "width": self.media.width, "height": self.media.height,
                "duration_seconds": self.media.duration_seconds,
            }
        if self.sticker.sticker_id or self.sticker.media.path or self.sticker.emoji:
            d["sticker"] = {
                "sticker_id": self.sticker.sticker_id,
                "pack_name": self.sticker.pack_name,
                "emoji": self.sticker.emoji,
            }
        if self.link.url:
            d["link"] = {
                "url": self.link.url, "title": self.link.title,
                "description": self.link.description,
                "thumbnail_url": self.link.thumbnail_url,
            }
        if self.location.latitude or self.location.name:
            d["location"] = {
                "latitude": self.location.latitude,
                "longitude": self.location.longitude,
                "name": self.location.name, "address": self.location.address,
            }
        if self.quote.reply_to_id:
            d["quote"] = {
                "reply_to_id": self.quote.reply_to_id,
                "reply_to_user": self.quote.reply_to_user,
                "reply_content": self.quote.reply_content,
            }
        if self.forward.title or self.forward.msg_count:
            d["forward"] = {
                "title": self.forward.title,
                "msg_count": self.forward.msg_count,
                "summary": self.forward.summary,
            }
        if self.mini_program.app_id or self.mini_program.title:
            d["mini_program"] = {
                "app_id": self.mini_program.app_id,
                "title": self.mini_program.title,
                "description": self.mini_program.description,
                "page_path": self.mini_program.page_path,
                "thumbnail_url": self.mini_program.thumbnail_url,
            }
        if self.transfer.amount or self.transfer.transaction_id:
            d["transfer"] = {
                "amount": self.transfer.amount,
                "currency": self.transfer.currency,
                "transaction_id": self.transfer.transaction_id,
                "status": self.transfer.status,
            }
        if self.extra:
            d["extra"] = self.extra
        return d


@dataclass
class SendResult:
    ok: bool
    msg_id: str = ""
    error: str = ""
    #: 回执口径（与 core.contracts.delivery.Ack 同义）：
    #: platform_id = 拿到了平台消息号；unconfirmed = 发出去了但没有号；error / none。
    ack: str = "platform_id"
    #: 微信这条路的两个真实身份：本地行号与服务器消息号。
    #: 没有它们就只有「输入框空了」这一条弱证据（见 docs/decisions/出口.md §5.3）。
    local_id: str = ""
    server_id: str = ""


# ---------------------------------------------------------------------------
# Provider factory registry
# ---------------------------------------------------------------------------
# New platforms should import their provider module, call
# ``register_provider_factory`` and the runtime will build one instance per
# configured account automatically.  No scheduler/bootstrap switch statements.
_PROVIDER_FACTORIES: dict[str, Callable[[Any], "Provider"]] = {}


def register_provider_factory(platform: str, factory: Callable[[Any], "Provider"]) -> None:
    _PROVIDER_FACTORIES[platform] = factory


def create_provider(account_config: Any) -> "Provider | None":
    platform = str(getattr(account_config, "platform", "") or "")
    factory = _PROVIDER_FACTORIES.get(platform)
    if factory is None:
        return None
    try:
        return factory(account_config)
    except Exception as exc:
        log.warning("provider_factory_failed", platform=platform,
                    account_id=getattr(account_config, "account_id", ""),
                    error=str(exc)[:160])
        return None


class PlatformTypeMapper:
    _WECHAT_MAP: dict[tuple[int, int], MsgType] = {
        (1, 0): MsgType.TEXT,
        (3, 0): MsgType.IMAGE,
        (34, 0): MsgType.VOICE,
        (37, 0): MsgType.CONTACT_CARD,
        (42, 0): MsgType.CONTACT_CARD,
        (43, 0): MsgType.VIDEO,
        (47, 0): MsgType.STICKER,
        (48, 0): MsgType.LOCATION,
        (49, 0): MsgType.LINK,
        (49, 1): MsgType.LINK,
        (49, 4): MsgType.LINK,
        (49, 5): MsgType.LINK,
        (49, 6): MsgType.FILE,
        (49, 8): MsgType.STICKER,
        (49, 19): MsgType.FORWARD,
        (49, 33): MsgType.LINK,
        (49, 36): MsgType.LINK,
        (49, 50): MsgType.VIDEO,
        (49, 51): MsgType.MOMENTS_SHARE,
        (49, 53): MsgType.POLL,
        (49, 57): MsgType.QUOTE_REPLY,
        (49, 62): MsgType.VIDEO_CHANNEL,
        (49, 63): MsgType.VIDEO_CHANNEL,
        (49, 76): MsgType.MUSIC,
        (49, 87): MsgType.LINK,
        (49, 88): MsgType.VIDEO_CHANNEL,
        (49, 2000): MsgType.TRANSFER,
        (49, 2003): MsgType.LINK,
        (50, 0): MsgType.VOICE_CALL,
        (62, 0): MsgType.VIDEO,
        (10000, 0): MsgType.SYSTEM,
        (10000, 4): MsgType.PAT,
        (10000, 8000): MsgType.SYSTEM,
        (266287972401, 0): MsgType.PAT,
        (10002, 0): MsgType.REVOKE,
    }

    _QQ_MAP: dict[str, MsgType] = {
        "text": MsgType.TEXT,
        "image": MsgType.IMAGE,
        "voice": MsgType.VOICE,
        "video": MsgType.VIDEO,
        "file": MsgType.FILE,
        "face": MsgType.STICKER,
        "json": MsgType.LINK,
        "xml": MsgType.LINK,
        "at": MsgType.TEXT,
        "reply": MsgType.QUOTE_REPLY,
        "forward": MsgType.FORWARD,
        "location": MsgType.LOCATION,
        "poke": MsgType.PAT,
    }

    _TELEGRAM_MAP: dict[str, MsgType] = {
        "text": MsgType.TEXT,
        "photo": MsgType.IMAGE,
        "voice": MsgType.VOICE,
        "video": MsgType.VIDEO,
        "video_note": MsgType.VIDEO,
        "document": MsgType.FILE,
        "sticker": MsgType.STICKER,
        "animation": MsgType.STICKER,
        "audio": MsgType.MUSIC,
        "location": MsgType.LOCATION,
        "venue": MsgType.LOCATION,
        "contact": MsgType.CONTACT_CARD,
        "link": MsgType.LINK,
        "forward_date": MsgType.FORWARD,
        "reply_to_message": MsgType.QUOTE_REPLY,
        "invoice": MsgType.TRANSFER,
        "successful_payment": MsgType.TRANSFER,
        "poll": MsgType.POLL,
        "dice": MsgType.PAT,
    }

    @classmethod
    def from_wechat(cls, wx_type: int, wx_sub_type: int = 0) -> MsgType:
        return cls._WECHAT_MAP.get((wx_type, wx_sub_type),
                                   cls._WECHAT_MAP.get((wx_type, 0), MsgType.TEXT))

    @classmethod
    def from_qq(cls, qq_msg_type: str) -> MsgType:
        return cls._QQ_MAP.get(qq_msg_type, MsgType.TEXT)

    @classmethod
    def from_telegram(cls, tg_msg_type: str) -> MsgType:
        return cls._TELEGRAM_MAP.get(tg_msg_type, MsgType.TEXT)

    @classmethod
    def from_platform(cls, platform: Platform, raw_type: Any, raw_sub_type: Any = None) -> MsgType:
        if platform == Platform.WECHAT:
            return cls.from_wechat(int(raw_type), int(raw_sub_type or 0))
        if platform == Platform.QQ:
            return cls.from_qq(str(raw_type))
        if platform == Platform.TELEGRAM:
            return cls.from_telegram(str(raw_type))
        return MsgType.TEXT


class Provider(ABC):
    platform: Platform = Platform.WECHAT
    name: str = ""
    # Capability names used by tools to decide what they may call.
    capabilities: frozenset[str] = frozenset({"text"})

    @abstractmethod
    async def connect(self) -> bool: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    async def send(self, chat_id: str, content: str, filepath: str = "") -> SendResult: ...

    @abstractmethod
    async def receive(self) -> AsyncIterator[InboundMessage]:
        yield  # type: ignore

    async def send_sticker(self, chat_id: str, mood_id: str = "",
                           emotion: str = "", **kwargs) -> SendResult:
        return SendResult(
            ok=False,
            error=f"{self.platform.value} Provider 暂不支持表情包",
        )

    async def send_image(self, chat_id: str, filepath: str,
                         content: str = "") -> SendResult:
        return await self.send(chat_id, content, filepath=filepath)

    async def sync_directory(self, repo: Any = None) -> int:
        """可选钩子：同步平台通讯录（群/成员）。默认不做。"""
        return 0

    @property
    def is_connected(self) -> bool:
        return False


class WechatProvider(Provider):
    platform = Platform.WECHAT
    name = "wechat"
    capabilities = frozenset({"text", "file", "image", "sticker"})

    def __init__(self, db_path: str = "", msg_db_path: str = "",
                 contact_db_path: str = "", send_enabled: bool = False,
                 account_id: str = ""):
        self._db_path = db_path
        self._msg_db_path = msg_db_path
        self._contact_db_path = contact_db_path
        self._send_enabled = send_enabled
        self._account_id = account_id
        self._sender = None
        self._sync = None
        self._connected = False

    async def connect(self) -> bool:
        if not self._db_path:
            return False
        try:
            from db.sync_service import SyncService
            context = None
            try:
                from core.bootstrap import get_account_context
                context = get_account_context(self.platform.value, self._account_id)
            except Exception:
                warn("core/execution/provider.py:466 Exception", err=repr(__import__("sys").exc_info()[1]))
                context = None
            self._sync = SyncService(
                self._db_path,
                account_id=self._account_id,
                platform=self.platform.value,
                context=context,
            )
            self._connected = True
            if self._send_enabled:
                try:
                    from core.execution.wx_sender import WxSender
                    self._sender = WxSender.get_instance()
                    self._sender.connect()
                    log.info("wechat_sender_enabled")
                except Exception as e:
                    log.warning("wechat_sender_init_failed", error=str(e))
            return True
        except Exception as e:
            log.error("wechat_connect_failed", error=str(e))
            return False

    async def disconnect(self) -> None:
        if self._sender:
            self._sender.disconnect()
            self._sender = None
        self._connected = False
        self._sync = None

    async def send(self, chat_id: str, content: str, filepath: str = "") -> SendResult:
        if not self._send_enabled:
            return SendResult(ok=False, error="发送功能未启用，请在配置中设置 WECHAT_SEND_ENABLED=true")
        if not self._sender:
            try:
                from core.execution.wx_sender import WxSender
                self._sender = WxSender.get_instance()
            except Exception as e:
                warn("core/execution/provider.py:502 Exception", err=repr(e))
                return SendResult(ok=False, error=f"发送器初始化失败: {e}")

        import asyncio
        loop = asyncio.get_event_loop()

        # Give the UI automation the account this provider belongs to, so
        # display-name -> wxid resolution uses the right account's contacts.
        try:
            from core.execution import wx_chat
            wx_chat.set_account_id(self._account_id or "")
        except Exception:
            warn("core/execution/provider.py:513 Exception", err=repr(__import__("sys").exc_info()[1]))

        if filepath:
            result = await loop.run_in_executor(None, self._sender.send_file, chat_id, filepath)
        else:
            result = await loop.run_in_executor(None, self._sender.send_text, chat_id, content)

        if result.get("ok"):
            local_id = str(result.get("local_id") or "")
            server_id = str(result.get("server_id") or "")
            ack = str(result.get("ack") or "")
            if not ack:
                ack = "platform_id" if (server_id or local_id) else "unconfirmed"
            return SendResult(
                ok=True,
                msg_id=server_id or local_id,
                ack=ack,
                local_id=local_id,
                server_id=server_id,
            )
        return SendResult(ok=False, error=result.get("error", "发送失败"), ack="error")

    async def receive(self) -> AsyncIterator[InboundMessage]:
        from core.ingestion.platform_messages import WechatMessage
        from db.repository import RepositoryManager
        from db.models import Message

        if not self._sync:
            return
        repo = self._sync.repo
        with repo.messages._session() as session:
            rows = session.query(Message).order_by(Message.created_at.desc()).limit(100)
            for row in rows:
                wx_msg = WechatMessage(
                    msg_svr_id=row.msg_id,
                    local_id=row.local_id or 0,
                    type=row.msg_type or 0,
                    sub_type=row.msg_sub_type or 0,
                    is_sender=row.is_self,
                    create_time=int(row.created_at.timestamp()) if row.created_at else 0,
                    str_talker=row.chat_id or "",
                    sender_wxid=row.sender_id or "",
                    sender_name=row.sender_name or "",
                    extracted_content=row.content or "",
                    media_path=row.media_path or "",
                    emoji_md5=row.sticker_id or "",
                    emoji_url=row.media_url or "",
                    link_url=row.link_url or "",
                    link_title=row.link_title or "",
                )
                yield wx_msg.to_inbound()

    async def sync_messages(self, msg_db_path: str = "") -> int:
        if not self._sync:
            return 0
        path = msg_db_path or self._msg_db_path
        if not path:
            return 0
        try:
            return self._sync.sync_messages(path, source="wechat")
        except Exception as e:
            log.error("wechat_sync_failed", error=str(e))
            return 0

    async def sync_contacts(self, contact_db_path: str = "") -> int:
        if not self._sync:
            return 0
        path = contact_db_path or self._contact_db_path
        if not path:
            return 0
        try:
            return self._sync.sync_contacts(path, source="wechat")
        except Exception as e:
            log.error("wechat_contact_sync_failed", error=str(e))
            return 0

    @property
    def is_connected(self) -> bool:
        return self._connected


class ProviderManager:
    """Provider pool keyed by platform+account.

    A platform can have many accounts; the old ``dict[name]`` layout let the
    second WeChat account overwrite the first one.  The canonical key is now
    ``name:account_id`` (or just ``name`` for account-less providers), while
    ``get(name)`` keeps returning the first provider for legacy callers.
    """

    def __init__(self):
        self._providers: dict[str, Provider] = {}
        self._by_account: dict[tuple[str, str], Provider] = {}

    @staticmethod
    def _provider_key(provider: Provider) -> str:
        account_id = getattr(provider, "_account_id", "")
        return f"{provider.name}:{account_id}" if account_id else provider.name

    def register(self, provider: Provider):
        account_id = getattr(provider, "_account_id", "")
        self._providers[self._provider_key(provider)] = provider
        if account_id:
            key = (provider.platform.value, account_id)
            self._by_account[key] = provider
        log.info("provider_registered", name=provider.name,
                 platform=provider.platform.value, account_id=account_id)

    def get(self, name: str) -> Provider | None:
        direct = self._providers.get(name)
        if direct is not None:
            return direct
        for provider in self._providers.values():
            if provider.name == name:
                return provider
        return None

    def get_by_account(self, platform: str, account_id: str) -> Provider | None:
        return self._by_account.get((platform, account_id))

    def by_platform(self, platform: Platform) -> list[Provider]:
        return [p for p in self._providers.values()
                if p.platform == platform]

    def supports(self, platform: str, capability: str) -> bool:
        for p in self._providers.values():
            if p.platform.value != platform:
                continue
            if capability in getattr(p, "capabilities", ()):
                return True
        return False

    def capable_for(self, platform: str, capability: str) -> list[Provider]:
        return [
            p for p in self._providers.values()
            if p.platform.value == platform and capability in getattr(p, "capabilities", ())
        ]

    def any_capability(self, capability: str) -> bool:
        return any(
            capability in getattr(p, "capabilities", ())
            for p in self._providers.values()
        )

    @property
    def all(self) -> dict[str, Provider]:
        return dict(self._providers)

    async def connect_all(self) -> dict[str, bool]:
        results = {}
        for name, p in self._providers.items():
            try:
                results[name] = await p.connect()
            except Exception as e:
                results[name] = False
                log.error("provider_connect_failed", name=name, error=str(e))
        return results

    async def disconnect_all(self):
        for p in self._providers.values():
            try:
                await p.disconnect()
            except Exception as _:
                warn("core/execution/provider.py:676 Exception", err=repr(_))

    async def receive_all(self) -> AsyncIterator[InboundMessage]:
        for p in self._providers.values():
            try:
                async for msg in p.receive():
                    yield msg
            except Exception as _:
                warn("core/execution/provider.py:684 Exception", err=repr(_))


def _build_wechat_provider(ac: Any) -> Provider:
    return WechatProvider(
        account_id=getattr(ac, "account_id", ""),
        # The provider facade uses the account DB; the raw WeChat directory is
        # owned by the ingestion adapter.
        db_path=getattr(ac, "db_path", ""),
        send_enabled=bool(getattr(ac, "send_enabled", False)),
    )


register_provider_factory("wechat", _build_wechat_provider)
