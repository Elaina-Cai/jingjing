from datetime import datetime
from enum import StrEnum
from pathlib import Path
from sqlalchemy import (
    Column, Integer, Float, Text, DateTime, ForeignKey, UniqueConstraint, Index,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from safelog import warn


class Base(DeclarativeBase):
    pass  # noqa: silent（有意忽略，见 CONTRACT 第八条）


class ChatType(StrEnum):
    PRIVATE = "private"
    GROUP = "group"
    CHANNEL = "channel"
    SYSTEM = "system"


class MessageSource(StrEnum):
    WECHAT = "wechat"
    QQ = "qq"
    TELEGRAM = "telegram"
    DISCORD = "discord"
    DINGTALK = "dingtalk"
    FEISHU = "feishu"
    WEB = "web"


class MsgSource(StrEnum):
    USER_DIRECT = "user_direct"
    YINYUE_PROXY = "yinyue_proxy"
    YINYUE_OWN = "yinyue_own"


class SchemaVersion(Base):
    __tablename__ = "schema_version"

    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime)
    description: Mapped[str | None] = mapped_column(Text)


class Contact(Base):
    __tablename__ = "contacts"

    wxid: Mapped[str] = mapped_column(Text, primary_key=True)
    nickname: Mapped[str | None] = mapped_column(Text)
    remark: Mapped[str | None] = mapped_column(Text)
    avatar_path: Mapped[str | None] = mapped_column(Text)
    is_group: Mapped[int] = mapped_column(Integer, default=0)
    metadata_: Mapped[str | None] = mapped_column("metadata", Text)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)
    account_id: Mapped[str] = mapped_column(Text, default="", server_default="")


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint(
            "account_id", "source", "chat_id", "msg_id",
            name="ux_messages_identity",
        ),
    )

    # Surrogate primary key.  Platform message ids are only unique inside a
    # (platform, account, chat) tuple, so they must not be the global PK once
    # several platforms/accounts share the schema.
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    msg_id: Mapped[int] = mapped_column(Integer, index=True)
    local_id: Mapped[int | None] = mapped_column(Integer)
    sender_id: Mapped[str | None] = mapped_column(Text)
    sender_name: Mapped[str | None] = mapped_column(Text)
    chat_id: Mapped[str | None] = mapped_column(Text)
    chat_type: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str | None] = mapped_column(Text)
    msg_type: Mapped[int | None] = mapped_column(Integer)
    msg_sub_type: Mapped[int | None] = mapped_column(Integer)
    msg_type_name: Mapped[str | None] = mapped_column(Text)
    secondary_types: Mapped[str | None] = mapped_column(Text)
    reply_to: Mapped[int | None] = mapped_column(Integer)
    media_path: Mapped[str | None] = mapped_column(Text)
    media_url: Mapped[str | None] = mapped_column(Text)
    media_file_id: Mapped[str | None] = mapped_column(Text)
    media_mime: Mapped[str | None] = mapped_column(Text)
    media_width: Mapped[int | None] = mapped_column(Integer)
    media_height: Mapped[int | None] = mapped_column(Integer)
    media_duration: Mapped[float | None] = mapped_column(Float)
    sticker_id: Mapped[str | None] = mapped_column(Text)
    sticker_emoji: Mapped[str | None] = mapped_column(Text)
    link_url: Mapped[str | None] = mapped_column(Text)
    link_title: Mapped[str | None] = mapped_column(Text)
    location_lat: Mapped[float | None] = mapped_column(Float)
    location_lon: Mapped[float | None] = mapped_column(Float)
    location_name: Mapped[str | None] = mapped_column(Text)
    quote_reply_id: Mapped[str | None] = mapped_column(Text)
    quote_reply_user: Mapped[str | None] = mapped_column(Text)
    quote_reply_content: Mapped[str | None] = mapped_column(Text)
    mentions: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)
    is_self: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(Text, default=MessageSource.WECHAT)
    extra: Mapped[str | None] = mapped_column(Text)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime)
    topic_id: Mapped[int | None] = mapped_column(Integer, index=True)
    msg_source: Mapped[str | None] = mapped_column(Text)
    account_id: Mapped[str] = mapped_column(Text, default="", server_default="")


class MessageMedia(Base):
    """One piece of media on one message, tracked until it is actually usable.

    Why this is a table and not just ``messages.media_path``
    -------------------------------------------------------
    The path used to be resolved exactly once, while the message was being
    ingested, and whatever came back was final.  Measured on live traffic:
    WeChat wrote the thumbnail of a group image 1.1s after it was sent and the
    thumbnail of a private image 27s after it was sent, while the sync pass that
    ingests both ran about 6s after they were sent.  The group one resolved, the
    private one did not -- and because the md5 that would have made a second
    attempt possible was never stored, it stayed unresolved forever.

    A row here exists for every media message regardless of whether the first
    attempt worked, so the clues survive and a later pass can try again once the
    file appears.  ``status`` is the state machine: pending -> resolved, or
    missing/unsupported when retrying cannot help.
    """

    __tablename__ = "message_media"
    __table_args__ = (
        Index("ix_message_media_pending", "status", "next_retry_at"),
        Index("ix_message_media_message", "message_id"),
        Index("ix_message_media_chat", "chat_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # messages.id -- the surrogate key, so a resolve can write media_path back.
    message_id: Mapped[int | None] = mapped_column(Integer)
    # Platform message id (WeChat server_id), for matching a re-read raw row.
    msg_id: Mapped[int | None] = mapped_column(Integer)
    platform: Mapped[str] = mapped_column(Text, default="wechat", server_default="wechat")
    account_id: Mapped[str] = mapped_column(Text, default="", server_default="")
    chat_id: Mapped[str | None] = mapped_column(Text)
    sender_id: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text, default="image", server_default="image")
    status: Mapped[str] = mapped_column(Text, default="pending", server_default="pending")

    # Clues.  Any one of them may be enough for a later attempt, which is the
    # whole point: the first attempt failing must not destroy the input.
    md5: Mapped[str | None] = mapped_column(Text, index=True)
    # md5(session) -- the directory WeChat files this chat's media under.
    session_dir: Mapped[str | None] = mapped_column(Text)
    # For a quoted/replied message: the platform id of the message it quotes,
    # whose own media row holds the picture that is actually being discussed.
    quote_ref: Mapped[str | None] = mapped_column(Text)
    source_path: Mapped[str | None] = mapped_column(Text)

    # Outcome.
    local_path: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)


class Topic(Base):
    __tablename__ = "topics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[str] = mapped_column(Text, index=True)
    chat_type: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="active")
    last_msg_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    msg_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)


class Thread(Base):
    __tablename__ = "threads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[str] = mapped_column(Text, index=True)
    parent_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("threads.id"), index=True)
    scope: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="active")
    context_budget: Mapped[int] = mapped_column(Integer, default=4096)
    temperature: Mapped[float] = mapped_column(Float, default=1.0)
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    msg_count: Mapped[int] = mapped_column(Integer, default=0)
    predictor_snapshot: Mapped[str | None] = mapped_column(Text)
    summary_quality: Mapped[str] = mapped_column(Text, default="rough")
    memory_consolidated: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)
    platform: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    account_id: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    summary: Mapped[str | None] = mapped_column(Text)


class ThreadMsgRef(Base):
    __tablename__ = "thread_msg_ref"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    thread_id: Mapped[int] = mapped_column(Integer, ForeignKey("threads.id"), index=True)
    msg_id: Mapped[int] = mapped_column(Integer, index=True)
    relevance: Mapped[float] = mapped_column(Float, default=0.5)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    status: Mapped[str] = mapped_column(Text, default="tentative")
    platform: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    account_id: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    chat_id: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")


class Analysis(Base):
    __tablename__ = "analysis"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[str | None] = mapped_column(Text)
    analysis_type: Mapped[str | None] = mapped_column(Text)
    result: Mapped[str | None] = mapped_column(Text)
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)
    msg_time: Mapped[datetime | None] = mapped_column(DateTime)


class FrictionEvent(Base):
    __tablename__ = "friction_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(Text)
    msg_id: Mapped[int] = mapped_column(Integer, index=True)
    from_thread_id: Mapped[int | None] = mapped_column(Integer)
    to_thread_id: Mapped[int | None] = mapped_column(Integer)
    diagnosis_cause: Mapped[str | None] = mapped_column(Text)
    diagnosis_detail: Mapped[str | None] = mapped_column(Text)
    lesson_applied: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class UserProfile(Base):
    __tablename__ = "user_profile"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)


class SyncState(Base):
    __tablename__ = "sync_state"

    source: Mapped[str] = mapped_column(Text, primary_key=True)
    last_sync: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str | None] = mapped_column(Text)
    error_msg: Mapped[str | None] = mapped_column(Text)
    # Per-Msg_* table high-water mark, stored as JSON so adding a new chat
    # does not require a schema migration.
    cursor_json: Mapped[str | None] = mapped_column(Text)


class Persona(Base):
    __tablename__ = "personas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, unique=True)
    system_prompt: Mapped[str | None] = mapped_column(Text)
    traits: Mapped[str | None] = mapped_column(Text)
    voice_config: Mapped[str | None] = mapped_column("voice", Text)
    avatar: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)


class PersonaMode(Base):
    __tablename__ = "persona_modes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    persona_id: Mapped[int] = mapped_column(Integer, ForeignKey("personas.id"), nullable=False)
    mode: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    system_prompt_override: Mapped[str | None] = mapped_column(Text)
    traits_override: Mapped[str | None] = mapped_column(Text)
    voice_override: Mapped[str | None] = mapped_column(Text)
    rules: Mapped[str | None] = mapped_column(Text)
    extra_rules: Mapped[str | None] = mapped_column(Text)


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    persona_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("personas.id"))
    session_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("chat_sessions.id"))
    role: Mapped[str] = mapped_column(Text)
    content: Mapped[str | None] = mapped_column(Text)
    emotion: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[str | None] = mapped_column("metadata", Text)
    # Identity context for the turn.  The shared meta database owns the row;
    # the legacy/account database model mirrors the columns so the same
    # ConversationRepository works for both after migration 23.
    platform: Mapped[str | None] = mapped_column(Text)
    account_id: Mapped[str | None] = mapped_column(Text)
    chat_id: Mapped[str | None] = mapped_column(Text)
    sender_id: Mapped[str | None] = mapped_column(Text)
    # Who spoke, as a display name.  In a group every other speaker's turn used
    # to render as a bare "用户", so the transcript could not answer "who said
    # this" or "who asked for that" -- and the rolling summary, which is built
    # from that same rendered text, made the loss permanent.  Nullable on
    # purpose: private chats do not need it and rows written before migration
    # 27 have no value to give.
    sender_name: Mapped[str | None] = mapped_column(Text)
    # The image this turn was about, if any.  Stored so the transcript can be
    # rendered with what the picture showed instead of a bare "[图片]" -- the
    # description is cached by file, so it is available on every later turn.
    media_path: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)


# A WeChat group's chat id carries this marker; a private chat's id is the
# other side's wxid.  Only a group needs every speaker named in a transcript,
# so this is the switch the history renderers use.
_GROUP_CHAT_MARKERS = ("@chatroom", "@group")
# QQ（OneBot）用 ``group:<群号>`` 前缀区分群/私聊。
_GROUP_CHAT_PREFIXES = ("group:",)

# Values that stand in for "we could not work out who this was".  Rendering one
# would produce a label like "用户#nknown", which reads as a name.
_PLACEHOLDER_SENDERS = frozenset({"", "unknown", "?", "-", "none", "null", "0"})


def is_group_chat_id(chat_id: str | None) -> bool:
    value = str(chat_id or "")
    if any(marker in value for marker in _GROUP_CHAT_MARKERS):
        return True
    return value.startswith(_GROUP_CHAT_PREFIXES)


def safe_speaker_name(value) -> str:
    """A speaker name is untrusted text on its way into a prompt.

    Nicknames can contain newlines and arbitrary length, and a name is
    rendered into the transcript one line per message, so collapse whitespace
    and cap the length rather than letting a nickname forge transcript lines.
    """
    return " ".join(str(value or "").split())[:32]


def speaker_label(row, fallback: str) -> str:
    """Label one conversation row for a transcript.

    A private chat has exactly one other participant, so "用户" is unambiguous
    there and the label stays exactly as it was.  A group does not: every
    member's turn rendered as "用户", which is how a request made by one person
    came to be answered as if another had made it.  The label keeps the "用户"
    prefix so anything keying off the old text still matches.

    Lives here rather than in each renderer because the same transcript is
    built in two places -- the reply prompt and the rolling summary -- and a
    summary built without attribution loses the speaker permanently.
    """
    if getattr(row, "role", "") != "user":
        return fallback
    if not is_group_chat_id(getattr(row, "chat_id", "")):
        return fallback
    name = safe_speaker_name(getattr(row, "sender_name", ""))
    if name:
        return f"用户:{name}"
    # Rows written before the column existed: a short id cannot name anyone,
    # but it still separates one member from another.  Placeholders such as
    # "unknown" are not identities, so they fall through to the plain label.
    sender_id = safe_speaker_name(getattr(row, "sender_id", ""))
    if sender_id and sender_id.lower() not in _PLACEHOLDER_SENDERS:
        return f"用户#{sender_id[-6:]}"
    return fallback


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    persona_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("personas.id"))
    title: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(Text, default="web")
    metadata_: Mapped[str | None] = mapped_column("metadata", Text)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)


class CoreMemoryBlock(Base):
    __tablename__ = "core_memories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    persona_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("personas.id"))
    block: Mapped[str] = mapped_column(Text)
    content: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)


class Relationship(Base):
    __tablename__ = "relationships"

    persona_id: Mapped[int] = mapped_column(Integer, ForeignKey("personas.id"), primary_key=True)
    target_wxid: Mapped[str] = mapped_column(Text, primary_key=True)
    relation_type: Mapped[str | None] = mapped_column(Text)
    intimacy: Mapped[float] = mapped_column(Float, default=0.5)
    notes: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Transient affect (see core.affect).  ``intimacy`` above is the slow,
    # near-static "what are we to each other"; these three are the per-turn
    # "how do I feel right now", which decays back to 0 and is written only by
    # AffectManager -- never by RelationshipRepository.upsert, which owns the
    # structural columns.
    warmth: Mapped[float | None] = mapped_column(Float, default=0.0)
    warmth_reason: Mapped[str | None] = mapped_column(Text)
    warmth_updated_at: Mapped[datetime | None] = mapped_column(DateTime)


class Reminder(Base):
    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    persona_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("personas.id"))
    content: Mapped[str | None] = mapped_column(Text)
    remind_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(Text, default="pending")
    repeat_rule: Mapped[str | None] = mapped_column(Text)

    # 43：提醒的送达地址与重试止损线（失败 3 次停手）
    # 送达三元组由 CreateReminderTool 从 ctx 固化；无 chat_id 直接拒绝

    source_platform: Mapped[str | None] = mapped_column(Text)
    source_account_id: Mapped[str | None] = mapped_column(Text)
    source_chat_id: Mapped[str | None] = mapped_column(Text)
    source_chat_type: Mapped[str | None] = mapped_column(Text)

    # 44：非主人委托的授权/审计字段
    requester_id: Mapped[str | None] = mapped_column(Text)
    requester_name: Mapped[str | None] = mapped_column(Text)
    requester_is_owner: Mapped[int] = mapped_column(Integer, default=0)

    # 44：群提醒的 @ 对象创建时定死；45 起 mention_names 优先
    #     （旧列 mention_name 保留只读兼容，投递端回退用）
    mention_name: Mapped[str | None] = mapped_column(Text)
    mention_names: Mapped[str | None] = mapped_column(Text)

    # 45：任务类型（remind/act；act 先留字段、执行器未接）
    purpose: Mapped[str] = mapped_column(Text, default="remind")

    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)


class Todo(Base):
    __tablename__ = "todos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    persona_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("personas.id"))
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="pending")
    priority: Mapped[int] = mapped_column(Integer, default=0)
    due_at: Mapped[datetime | None] = mapped_column(DateTime)
    source_chat_id: Mapped[str | None] = mapped_column(Text)
    source_msg_id: Mapped[int | None] = mapped_column(Integer)
    tags: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)


class KnowledgeDoc(Base):
    __tablename__ = "knowledge_docs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    persona_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("personas.id"))
    title: Mapped[str] = mapped_column(Text)
    source_file: Mapped[str | None] = mapped_column(Text)
    chunk_index: Mapped[int] = mapped_column(Integer, default=0)
    content: Mapped[str] = mapped_column(Text)
    char_count: Mapped[int] = mapped_column(Integer, default=0)
    tags: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)
    source_type: Mapped[str] = mapped_column(Text, default="doc_chunk")
    source_url: Mapped[str | None] = mapped_column(Text)
    original_query: Mapped[str | None] = mapped_column(Text)
    quality_score: Mapped[float] = mapped_column(Float, default=0.5)
    content_hash: Mapped[str | None] = mapped_column(Text)
    access_count: Mapped[int] = mapped_column(Integer, default=0)
    last_accessed_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)


class SendLog(Base):
    __tablename__ = "send_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    platform: Mapped[str] = mapped_column(Text)
    chat_id: Mapped[str] = mapped_column(Text, index=True)
    chat_type: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str | None] = mapped_column(Text)
    msg_type: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    trigger: Mapped[str] = mapped_column(Text)
    trigger_msg_id: Mapped[int | None] = mapped_column(Integer)
    account: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="pending")
    error: Mapped[str | None] = mapped_column(Text)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)
    confirmed_msg_id: Mapped[int | None] = mapped_column(Integer)
    extra: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    def get_extra(self, model_cls: type) -> object | None:
        if not self.extra:
            return None
        try:
            return model_cls.model_validate_json(self.extra)
        except Exception:
            warn("db/models.py:520 Exception", err=repr(__import__("sys").exc_info()[1]))
            return None

    def get_extra_by_platform(self, log_type: str = "send") -> object | None:
        from core.execution.platform_registry import get_extra_cls
        cls = get_extra_cls(self.platform, log_type)
        if not cls:
            return None
        return self.get_extra(cls)


class ActionLog(Base):
    __tablename__ = "action_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tool_name: Mapped[str] = mapped_column(Text)
    tool_args: Mapped[str | None] = mapped_column(Text)
    result_summary: Mapped[str | None] = mapped_column(Text)
    platform: Mapped[str | None] = mapped_column(Text)
    account: Mapped[str | None] = mapped_column(Text)
    target: Mapped[str | None] = mapped_column(Text)
    trigger: Mapped[str] = mapped_column(Text)
    trigger_msg_id: Mapped[int | None] = mapped_column(Integer)
    thread_id: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    token_usage: Mapped[int | None] = mapped_column(Integer)
    parent_action_id: Mapped[int | None] = mapped_column(Integer)
    related_send_log_id: Mapped[int | None] = mapped_column(Integer)
    extra: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    def get_extra(self, model_cls: type) -> object | None:
        if not self.extra:
            return None
        try:
            return model_cls.model_validate_json(self.extra)
        except Exception:
            warn("db/models.py:558 Exception", err=repr(__import__("sys").exc_info()[1]))
            return None

    def get_extra_by_platform(self, log_type: str = "action") -> object | None:
        from core.execution.platform_registry import get_extra_cls
        platform = self.platform or ""
        cls = get_extra_cls(platform, log_type)
        if not cls:
            return None
        return self.get_extra(cls)


class EventOutbox(Base):
    """account.db 的事件 outbox（任务模型 §7.1 ②）。

    与消息行**同事务**写入；``core/terminal/outbox.py`` 的 forwarder 把
    pending 行幂等投递到 meta.db 的 ``terminal_events``：失败退避重试、
    到止损线标 failed + 告警（迁移 47 以 ``messages`` 表存在为守卫）。
    """

    __tablename__ = "event_outbox"
    __table_args__ = (
        UniqueConstraint(
            "platform", "account_id", "chat_id", "msg_id",
            name="ux_event_outbox_msg",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    platform: Mapped[str] = mapped_column(Text, default="")
    account_id: Mapped[str] = mapped_column(Text, default="")
    chat_id: Mapped[str] = mapped_column(Text, default="")
    msg_id: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(Text, index=True, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.now)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime)
    # REC-8（迁移 51）：行级 claim / 租约。旧库由迁移补列，新库 create_all 直接带上。
    # claimed_by 用 server_default：迁移 51 的 ALTER 也是 DEFAULT ''，而
    # enqueue_message_outbox 的 INSERT 不带这一列（NOT NULL 会把登记整行打回）。
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime)
    claimed_by: Mapped[str] = mapped_column(Text, default="", server_default="")
    lease_until: Mapped[datetime | None] = mapped_column(DateTime)


def init_db(db_path: str, tables=None):
    from sqlalchemy import event

    # 库文件父目录可能还不存在：core.bootstrap.init_dirs() 只建「配置里已有账号」的
    # 目录，而运行时账号是从 meta 注册表恢复的（core/runtime.py load_registered_accounts），
    # 那些账号不在里面 ⇒ 全新隔离根上 create_all 直接
    # sqlite3.OperationalError: unable to open database file。
    # 这里只在「已被放行」之后到达（db/write_guard 已判定可写），补目录不给生产开洞。
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(
        f"sqlite:///{db_path}",
        pool_pre_ping=True,
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    if tables is None:
        Base.metadata.create_all(engine)
    else:
        Base.metadata.create_all(engine, tables=tables)
    from db.thread_scope import upgrade_thread_sources, upgrade_thread_summaries
    with engine.begin() as connection:
        upgrade_thread_sources(connection)
        upgrade_thread_summaries(connection)
    return engine
