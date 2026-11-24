from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Column,
    Integer,
    Float,
    Text,
    DateTime,
    ForeignKey,
    Index,
    UniqueConstraint,
    create_engine,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class MetaBase(DeclarativeBase):
    pass  # noqa: silent（有意忽略，见 CONTRACT 第八条）


class AccountRegistry(MetaBase):
    __tablename__ = "account_registry"

    account_id: Mapped[str] = mapped_column(Text, primary_key=True)
    platform: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text)
    data_dir: Mapped[str] = mapped_column(Text)
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    config_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.now)


class SchemaVersion(MetaBase):
    __tablename__ = "schema_version"

    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime)
    description: Mapped[str | None] = mapped_column(Text)


class Persona(MetaBase):
    __tablename__ = "personas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, unique=True)
    system_prompt: Mapped[str | None] = mapped_column(Text)
    traits: Mapped[str | None] = mapped_column(Text)
    voice_config: Mapped[str | None] = mapped_column("voice", Text)
    avatar: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)


class PersonaMode(MetaBase):
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


class PersonaVersion(MetaBase):
    """Immutable published snapshot of one persona."""

    __tablename__ = "persona_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    persona_id: Mapped[int] = mapped_column(Integer, ForeignKey("personas.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    aliases_json: Mapped[str | None] = mapped_column(Text)
    identity_json: Mapped[str | None] = mapped_column(Text)
    traits_json: Mapped[str | None] = mapped_column(Text)
    voice_json: Mapped[str | None] = mapped_column(Text)
    scene_styles_json: Mapped[str | None] = mapped_column(Text)
    prompt_sections_json: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.now)


class SpeechBinding(MetaBase):
    """Maps a target scope to a persona and a scene."""

    __tablename__ = "speech_bindings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope_type: Mapped[str] = mapped_column(Text, nullable=False)
    scope_id: Mapped[str] = mapped_column(Text, default="")
    platform: Mapped[str] = mapped_column(Text, default="")
    account_id: Mapped[str] = mapped_column(Text, default="")
    chat_type: Mapped[str] = mapped_column(Text, default="")
    scene: Mapped[str] = mapped_column(Text, default="")
    persona_id: Mapped[int] = mapped_column(Integer, ForeignKey("personas.id"), nullable=False)
    persona_version: Mapped[int | None] = mapped_column(Integer)
    overrides_json: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.now)


class PolicyRule(MetaBase):
    """A single conduct rule."""

    __tablename__ = "policy_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    name: Mapped[str | None] = mapped_column(Text, default="")
    scope_type: Mapped[str] = mapped_column(Text, default="global")
    scope_id: Mapped[str] = mapped_column(Text, default="")
    platform: Mapped[str] = mapped_column(Text, default="")
    account_id: Mapped[str] = mapped_column(Text, default="")
    chat_type: Mapped[str] = mapped_column(Text, default="")
    scene: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(Text, default="style")
    enforcement: Mapped[str] = mapped_column(Text, default="prompt")
    effect: Mapped[str] = mapped_column(Text, default="allow")
    match_type: Mapped[str] = mapped_column(Text, default="always")
    pattern: Mapped[str | None] = mapped_column(Text, default="")
    payload_json: Mapped[str | None] = mapped_column(Text)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    immutable: Mapped[int] = mapped_column(Integer, default=0)
    conditions_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.now)


class PolicyProfile(MetaBase):
    """Reusable rule bundle that can be inherited by scope bindings."""

    __tablename__ = "policy_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    name: Mapped[str | None] = mapped_column(Text, default="")
    description: Mapped[str | None] = mapped_column(Text, default="")
    parent_key: Mapped[str | None] = mapped_column(Text, default="")
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.now)


class PolicyProfileBinding(MetaBase):
    """Binds a profile bundle to a scope (global/group/contact/chat...)."""

    __tablename__ = "policy_profile_bindings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    profile_key: Mapped[str] = mapped_column(Text, nullable=False)
    scope_type: Mapped[str] = mapped_column(Text, default="global")
    scope_id: Mapped[str] = mapped_column(Text, default="")
    platform: Mapped[str] = mapped_column(Text, default="")
    account_id: Mapped[str] = mapped_column(Text, default="")
    chat_type: Mapped[str] = mapped_column(Text, default="")
    scene: Mapped[str] = mapped_column(Text, default="")
    priority: Mapped[int] = mapped_column(Integer, default=100)
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.now)


class PolicyProfileRule(MetaBase):
    """Maps a rule key into a reusable profile."""

    __tablename__ = "policy_profile_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    profile_key: Mapped[str] = mapped_column(Text, nullable=False)
    rule_key: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.now)


class PolicyVersion(MetaBase):
    """Snapshot of the conduct layer for diff/rollback."""

    __tablename__ = "policy_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    name: Mapped[str | None] = mapped_column(Text, default="")
    snapshot_json: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(Text, default="system")
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.now)


class PolicyLexicon(MetaBase):
    __tablename__ = "policy_lexicons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    name: Mapped[str | None] = mapped_column(Text, default="")
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.now)


class PolicyTerm(MetaBase):
    __tablename__ = "policy_terms"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lexicon_id: Mapped[int] = mapped_column(Integer, ForeignKey("policy_lexicons.id"), nullable=False)
    term: Mapped[str] = mapped_column(Text, nullable=False)
    match_type: Mapped[str] = mapped_column(Text, default="contains")
    severity: Mapped[str] = mapped_column(Text, default="block")
    action: Mapped[str] = mapped_column(Text, default="regenerate_then_replace")
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.now)


class SpeechConfigRevision(MetaBase):
    __tablename__ = "speech_config_revisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    revision: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    changed_by: Mapped[str] = mapped_column(Text, default="system")
    change_type: Mapped[str] = mapped_column(Text, default="manual")
    detail_json: Mapped[str | None] = mapped_column(Text)
    changed_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.now)


class OutboundAudit(MetaBase):
    __tablename__ = "outbound_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(Text, default="")
    platform: Mapped[str] = mapped_column(Text, default="")
    account_id: Mapped[str] = mapped_column(Text, default="")
    chat_id: Mapped[str] = mapped_column(Text, default="")
    chat_type: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[str] = mapped_column(Text, default="text")
    persona_id: Mapped[int | None] = mapped_column(Integer)
    persona_version: Mapped[int | None] = mapped_column(Integer)
    policy_revision: Mapped[str] = mapped_column(Text, default="")
    decision: Mapped[str] = mapped_column(Text, default="allow")
    rule_ids_json: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(Text)
    content_preview: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="pending")
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.now)


class Conversation(MetaBase):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    persona_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("personas.id"))
    session_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("chat_sessions.id"))
    role: Mapped[str] = mapped_column(Text)
    content: Mapped[str | None] = mapped_column(Text)
    emotion: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[str | None] = mapped_column("metadata", Text)
    # Identity context for the turn.  Populated for new rows; legacy rows may
    # leave these NULL until a later backfill.  These fields bridge
    # conversation memory to the person/group scoped memory layer.
    platform: Mapped[str | None] = mapped_column(Text)
    account_id: Mapped[str | None] = mapped_column(Text)
    chat_id: Mapped[str | None] = mapped_column(Text)
    sender_id: Mapped[str | None] = mapped_column(Text)
    # Display name of the speaker.  Kept next to sender_id because a raw wxid
    # is not something the model can attribute a request to, and because the
    # name has to survive into the rolling summary.  See migration 27.
    sender_name: Mapped[str | None] = mapped_column(Text)
    # The image this turn was about, so the transcript can say what the picture
    # showed rather than "[图片]" once it has been described.  See migration 29.
    media_path: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)


class ChatSession(MetaBase):
    __tablename__ = "chat_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    persona_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("personas.id"))
    title: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(Text, default="web")
    metadata_: Mapped[str | None] = mapped_column("metadata", Text)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)


class CoreMemoryBlock(MetaBase):
    __tablename__ = "core_memories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    persona_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("personas.id"))
    block: Mapped[str] = mapped_column(Text)
    content: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)


class Relationship(MetaBase):
    __tablename__ = "relationships"

    persona_id: Mapped[int] = mapped_column(Integer, ForeignKey("personas.id"), primary_key=True)
    target_wxid: Mapped[str] = mapped_column(Text, primary_key=True)
    relation_type: Mapped[str | None] = mapped_column(Text)
    intimacy: Mapped[float] = mapped_column(Float, default=0.5)
    notes: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Transient affect, shared with the account-side Relationship model.  The
    # meta database is the source of truth for relationships in multi-account
    # mode, so these columns must exist here as well.
    warmth: Mapped[float | None] = mapped_column(Float, default=0.0)
    warmth_reason: Mapped[str | None] = mapped_column(Text)
    warmth_updated_at: Mapped[datetime | None] = mapped_column(DateTime)


class Reminder(MetaBase):
    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    persona_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("personas.id"))
    content: Mapped[str | None] = mapped_column(Text)
    remind_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(Text, default="pending")
    repeat_rule: Mapped[str | None] = mapped_column(Text)
    # 见 db/models.py Reminder：送达地址 + 投递重试止损线必须两边同步，
    # 否则 create_all 建出来的 meta 库会缺列。
    source_platform: Mapped[str | None] = mapped_column(Text)
    source_account_id: Mapped[str | None] = mapped_column(Text)
    source_chat_id: Mapped[str | None] = mapped_column(Text)
    source_chat_type: Mapped[str | None] = mapped_column(Text)
    requester_id: Mapped[str | None] = mapped_column(Text)
    requester_name: Mapped[str | None] = mapped_column(Text)
    requester_is_owner: Mapped[int] = mapped_column(Integer, default=0)
    mention_name: Mapped[str | None] = mapped_column(Text)
    mention_names: Mapped[str | None] = mapped_column(Text)
    purpose: Mapped[str | None] = mapped_column(Text, default="remind")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)


class Todo(MetaBase):
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


class KnowledgeDoc(MetaBase):
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


class PersonEntity(MetaBase):
    """One canonical person inside one platform account.

    Weak identities (nicknames) never merge people automatically; strong
    identities (wxid / external_id) are the only auto-link source.  A person
    can later be linked to people in other accounts via an explicit merge.
    """

    __tablename__ = "person_entities"

    person_id: Mapped[str] = mapped_column(Text, primary_key=True)
    platform: Mapped[str] = mapped_column(Text, index=True)
    account_id: Mapped[str] = mapped_column(Text, index=True)
    display_name: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(Text, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class PersonIdentity(MetaBase):
    """One observed identifier for a person, scoped by platform/account/chat.

    ``chat_id`` scopes group nicknames.  A global remark/nickname keeps it
    empty; the unique constraint then prevents that exact scoped value from
    attaching to a second person by accident.
    """

    __tablename__ = "person_identities"
    __table_args__ = (
        UniqueConstraint(
            "platform", "account_id", "identity_type", "identity_value", "chat_id",
            name="ux_person_identity_scope",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    person_id: Mapped[str] = mapped_column(
        Text, ForeignKey("person_entities.person_id"), index=True,
    )
    platform: Mapped[str] = mapped_column(Text, index=True)
    account_id: Mapped[str] = mapped_column(Text, index=True)
    identity_type: Mapped[str] = mapped_column(Text)
    identity_value: Mapped[str] = mapped_column(Text)
    chat_id: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    source: Mapped[str] = mapped_column(Text, default="")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class GroupMembership(MetaBase):
    """A person's membership/participation inside one group chat."""

    __tablename__ = "group_memberships"
    __table_args__ = (
        UniqueConstraint(
            "person_id", "platform", "account_id", "chat_id",
            name="ux_group_membership_scope",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    person_id: Mapped[str] = mapped_column(
        Text, ForeignKey("person_entities.person_id"), index=True,
    )
    platform: Mapped[str] = mapped_column(Text, index=True)
    account_id: Mapped[str] = mapped_column(Text, index=True)
    chat_id: Mapped[str] = mapped_column(Text, index=True)
    group_nickname: Mapped[str] = mapped_column(Text, default="")
    role: Mapped[str] = mapped_column(Text, default="member")
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class GroupMembershipPeriod(MetaBase):
    """One contiguous ``[joined_at, left_at)`` group-membership epoch.

    A leave/re-join is recorded as a new row with a higher ``epoch``.  Command
    visibility queries bind to one epoch instead of treating every observed
    member as open-ended, which is what ``group_memberships`` alone cannot
    express.
    """

    __tablename__ = "group_membership_periods"
    __table_args__ = (
        UniqueConstraint(
            "person_id", "platform", "account_id", "chat_id", "epoch",
            name="ux_group_membership_period_epoch",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    person_id: Mapped[str] = mapped_column(Text, index=True)
    platform: Mapped[str] = mapped_column(Text, index=True)
    account_id: Mapped[str] = mapped_column(Text, index=True, default="")
    chat_id: Mapped[str] = mapped_column(Text, index=True)
    joined_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    left_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    epoch: Mapped[int] = mapped_column(Integer, default=1)
    source: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.now)


class AccountLinkEdge(MetaBase):
    """Explicit account-link edge managed by the owner (Web UI later).

    An absent/revoked row means the accounts stay isolated.  The table stores
    the raw edge only; connected components are derived deterministically by
    ``core.terminal.link_graph.AccountLinkGraph``.
    """

    __tablename__ = "account_link_edges"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    platform_a: Mapped[str] = mapped_column(Text, index=True)
    account_a: Mapped[str] = mapped_column(Text, index=True)
    platform_b: Mapped[str] = mapped_column(Text, index=True)
    account_b: Mapped[str] = mapped_column(Text, index=True)
    created_by: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.now)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    note: Mapped[str | None] = mapped_column(Text, default="", nullable=True)


class AccountLinkState(MetaBase):
    """Singleton revision counter for DB-managed account links.

    Two processes can cache an ``AccountLinkGraph``.  Every add/revoke bumps
    this row, so a cheap ``SELECT revision`` is enough to invalidate cached
    identity/recall state without re-reading every edge.
    """

    __tablename__ = "account_link_state"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.now)


class FeedbackSample(MetaBase):
    """Layer-2 quality signal collected from real usage (eval collection only)."""

    __tablename__ = "feedback_samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(Text, index=True)
    platform: Mapped[str] = mapped_column(Text, default="")
    bot_account_id: Mapped[str] = mapped_column(Text, default="")
    chat_id: Mapped[str] = mapped_column(Text, index=True, default="")
    chat_type: Mapped[str] = mapped_column(Text, default="")
    person_id: Mapped[str] = mapped_column(Text, default="")
    sample_text: Mapped[str] = mapped_column(Text, default="")
    previous_user_text: Mapped[str] = mapped_column(Text, default="")
    reply_event_id: Mapped[str] = mapped_column(Text, default="")
    reply_preview: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(Text, index=True, default="new")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.now)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "source": self.source,
            "platform": self.platform or "",
            "bot_account_id": self.bot_account_id or "",
            "chat_id": self.chat_id or "",
            "chat_type": self.chat_type or "",
            "person_id": self.person_id or "",
            "sample_text": self.sample_text or "",
            "previous_user_text": self.previous_user_text or "",
            "reply_event_id": self.reply_event_id or "",
            "reply_preview": self.reply_preview or "",
            "status": self.status or "new",
            "created_at": self.created_at.isoformat() if self.created_at else "",
        }


class ProactiveTarget(MetaBase):
    """Owner whitelist + rate policy for one bot account / conversation."""

    __tablename__ = "proactive_targets"
    __table_args__ = (
        UniqueConstraint(
            "platform", "bot_account_id", "chat_id",
            name="ux_proactive_target_scope",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    platform: Mapped[str] = mapped_column(Text, index=True)
    bot_account_id: Mapped[str] = mapped_column(Text, index=True, default="")
    chat_id: Mapped[str] = mapped_column(Text, index=True, default="")
    chat_type: Mapped[str] = mapped_column(Text, default="private")
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    allow_sticker: Mapped[int] = mapped_column(Integer, default=1)
    max_per_hour: Mapped[int] = mapped_column(Integer, default=6)
    max_per_day: Mapped[int] = mapped_column(Integer, default=20)
    cooldown_minutes: Mapped[int] = mapped_column(Integer, default=60)
    # JSON [start_hour, end_hour), supports wrapping midnight, e.g. [23, 7].
    quiet_hours_json: Mapped[str] = mapped_column(Text, default="[]")
    note: Mapped[str | None] = mapped_column(Text, default="", nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.now)


class ProactiveSendLog(MetaBase):
    """Append-only record of one proactive send attempt/result."""

    __tablename__ = "proactive_send_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    platform: Mapped[str] = mapped_column(Text, index=True, default="")
    bot_account_id: Mapped[str] = mapped_column(Text, index=True, default="")
    chat_id: Mapped[str] = mapped_column(Text, index=True, default="")
    sent_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    kind: Mapped[str] = mapped_column(Text, default="text")
    trigger: Mapped[str] = mapped_column(Text, default="proactive")
    content_preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    ok: Mapped[int] = mapped_column(Integer, default=1)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class TerminalEvent(MetaBase):
    """Append-only unified terminal event stream (Phase 2).

    Every message, command, reply, action, tool call and owner-visible thought
    lands here exactly once.  JSON payload columns keep the draft schema
    flexible while the hot query dimensions (time, actor, chat, type, command)
    are real indexed columns.
    """

    __tablename__ = "terminal_events"
    __table_args__ = (
        UniqueConstraint("event_id", name="ux_terminal_event_id"),
        Index(
            "ux_terminal_events_external_msg",
            "platform",
            "bot_account_id",
            "chat_id",
            "external_msg_id",
            unique=True,
            sqlite_where=text(
                "external_msg_id IS NOT NULL AND external_msg_id != ''"
            ),
        ),
        # 与 _m050 同名同列：新库 create_all 直接建好，老库由迁移补（避免重复索引）
        Index("ix_terminal_events_turn", "turn_id", "sequence"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(Text, unique=True, index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    sequence: Mapped[int] = mapped_column(Integer, index=True, default=0)
    platform: Mapped[str] = mapped_column(Text, index=True, default="")
    bot_account_id: Mapped[str] = mapped_column(Text, index=True, default="")
    chat_id: Mapped[str] = mapped_column(Text, index=True, default="")
    chat_type: Mapped[str] = mapped_column(Text, default="")
    actor_person_id: Mapped[str] = mapped_column(Text, index=True, default="")
    actor_account_id: Mapped[str] = mapped_column(Text, default="")
    recipient_person_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    event_type: Mapped[str] = mapped_column(Text, index=True, default="message")
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    action_payload_json: Mapped[str] = mapped_column(Text, default="{}")
    command_id: Mapped[str] = mapped_column(Text, index=True, default="")
    authorization_id: Mapped[str] = mapped_column(Text, default="")
    parent_event_id: Mapped[str] = mapped_column(Text, default="")
    turn_id: Mapped[str] = mapped_column(Text, default="")
    external_msg_id: Mapped[str] = mapped_column(Text, default="")
    link_revision: Mapped[int] = mapped_column(Integer, default=0)
    visibility_scope_json: Mapped[str] = mapped_column(Text, default='["public"]')
    sensitivity: Mapped[str] = mapped_column(Text, index=True, default="normal")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.now)
    # span 三列（m061）：原文地址，不是对内容的解释。-1 = 未知（与 EvidenceRef 同约定）。
    content_start: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("-1")
    )
    content_end: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("-1")
    )
    media_span_json: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'[]'")
    )


class WorkItemRow(MetaBase):
    """P1 任务台账：``terminal_events`` 的同步双写读模型（docs/任务模型.md §4.1 / §5）。

    **单向投影**：P1 期间 ``reminders`` 仍是唯一执行权威，本表只由
    ``core.tasks.store`` 从 reminders 侧写入（reminders → work_items，绝不反向），
    因此不存在第二条投递路径。

    状态集**以 §5.5 为准**（``open`` / ``running`` / ``waiting_user`` / ``done`` /
    ``failed`` / ``cancelled`` / ``superseded``）：§4.1 与 §4.4 只列了 5 个，少了
    ``waiting_user`` / ``superseded``，两处枚举不一致处按 §5.5 执行。

    列范围：只建「P1 会写 + 验收要查」的列；§5.2 / §5.6 的成本与快照类长尾
    （tokens / cost / tool_calls / constraints_json / run_summary …）**不预建**，
    先落事件 payload（§5.8 规则 2：新字段先落 payload，稳定后再提列）。
    所有非空列都带 ``server_default``：迁移脚本只传
    ``core/tasks/mapping.py::as_work_item_fields()`` 的字段也能插入。时间列一律
    **naive 本地墙钟**（§5.4），默认 NULL。

    Migration 52 的建表 DDL 必须与本模型**同形**（列名 / 类型 / NOT NULL / DEFAULT /
    索引全对齐），由 ``tests/test_work_item_p1.py`` 的 parity 用例锁住——ENG-17 就是
    「迁移 51 nullable vs ``create_all`` NOT NULL」的建表漂移。
    """

    __tablename__ = "work_items"
    __table_args__ = (
        UniqueConstraint("work_item_id", name="ux_work_item_id"),
        UniqueConstraint("idempotency_key", name="ux_work_items_idempotency_key"),
        Index("ix_work_items_status_due", "status", "due_at"),
        Index("ix_work_items_due_at", "due_at"),
        Index("ix_work_items_requester_person", "requester_person_id"),
        # 送达四元组 dedup（platform + bot_account_id + chat_id + external_msg_id）：
        # **刻意非唯一**——同一条消息可以建两条提醒，唯一键是 idempotency_key
        # （主人拍板 2026-09-30）。
        Index(
            "ix_work_items_target_dedup",
            "target_platform",
            "target_bot_account_id",
            "target_chat_id",
            "external_msg_id",
        ),
        Index("ix_work_items_command", "command_id"),
        Index("ix_work_items_source_event", "source_event_id"),
        Index("ix_work_items_legacy_reminder", "legacy_reminder_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 身份：work_item_id 是事实身份，idempotency_key 是幂等键（P1 两者同值）。
    work_item_id: Mapped[str] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(Text)
    # 类型与状态（§4.1 / §5.5）。
    kind: Mapped[str] = mapped_column(Text, server_default=text("'notify'"))
    trigger: Mapped[str] = mapped_column(Text, server_default=text("'at'"))
    status: Mapped[str] = mapped_column(Text, server_default=text("'open'"))
    # 意图与验收（§5.2 的热查询子集）。
    goal: Mapped[str] = mapped_column(Text, server_default=text("''"))
    spec_json: Mapped[str] = mapped_column(Text, server_default=text("'{}'"))
    spec_schema_version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    priority: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    repeat_spec_json: Mapped[str] = mapped_column(Text, server_default=text("''"))
    occurrences_done: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    due_at: Mapped[datetime | None] = mapped_column(DateTime)
    # 请求人（§5.1，对齐 CommandRecord 的 issuer_*）。
    requester_person_id: Mapped[str] = mapped_column(Text, server_default=text("''"))
    requester_account_id: Mapped[str] = mapped_column(Text, server_default=text("''"))
    requester_role: Mapped[str] = mapped_column(Text, server_default=text("'ordinary'"))
    requester_is_owner: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    # 送达（§5.3）：target_* 是送到哪，origin_* 是从哪来。
    target_platform: Mapped[str] = mapped_column(Text, server_default=text("''"))
    target_bot_account_id: Mapped[str] = mapped_column(Text, server_default=text("''"))
    target_chat_id: Mapped[str] = mapped_column(Text, server_default=text("''"))
    target_chat_type: Mapped[str] = mapped_column(Text, server_default=text("''"))
    origin_platform: Mapped[str] = mapped_column(Text, server_default=text("''"))
    origin_bot_account_id: Mapped[str] = mapped_column(Text, server_default=text("''"))
    origin_chat_id: Mapped[str] = mapped_column(Text, server_default=text("''"))
    origin_chat_type: Mapped[str] = mapped_column(Text, server_default=text("''"))
    mention_names_json: Mapped[str] = mapped_column(Text, server_default=text("'[]'"))
    # 上下文锚点（§5.1）：external_msg_id 是**触发消息**的 id（事件行的实列留空，
    # 见 core.tasks.store 的说明），source_event_id 是本行投影的来源事件
    # （work_item_created 的 event_id）。
    external_msg_id: Mapped[str] = mapped_column(Text, server_default=text("''"))
    our_message_id: Mapped[str] = mapped_column(Text, server_default=text("''"))
    session_id: Mapped[str] = mapped_column(Text, server_default=text("''"))
    thread_id: Mapped[str] = mapped_column(Text, server_default=text("''"))
    command_id: Mapped[str] = mapped_column(Text, server_default=text("''"))
    authorization_id: Mapped[str] = mapped_column(Text, server_default=text("''"))
    source_event_id: Mapped[str] = mapped_column(Text, server_default=text("''"))
    # 重试与取消（§5.5）。
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, server_default=text("3"))
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str] = mapped_column(Text, server_default=text("''"))
    last_error_class: Mapped[str] = mapped_column(Text, server_default=text("''"))
    last_error_code: Mapped[str] = mapped_column(Text, server_default=text("''"))
    claim_owner: Mapped[str] = mapped_column(Text, server_default=text("''"))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime)
    cancel_reason: Mapped[str] = mapped_column(Text, server_default=text("''"))
    cancelled_by: Mapped[str] = mapped_column(Text, server_default=text("''"))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime)
    # 兜底、格式、可见性与时间语义（§5.2 / §5.3 / §5.4）。
    fallback_text: Mapped[str] = mapped_column(Text, server_default=text("''"))
    acceptance_criteria: Mapped[str] = mapped_column(Text, server_default=text("''"))
    delivery_kind: Mapped[str] = mapped_column(Text, server_default=text("'text'"))
    format_json: Mapped[str] = mapped_column(Text, server_default=text("'{}'"))
    visibility_scope: Mapped[str] = mapped_column(Text, server_default=text("''"))
    owner_only: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    redaction_policy: Mapped[str] = mapped_column(Text, server_default=text("''"))
    user_tz: Mapped[str] = mapped_column(Text, server_default=text("''"))
    intended_local_time: Mapped[datetime | None] = mapped_column(DateTime)
    punctuality: Mapped[str] = mapped_column(Text, server_default=text("'normal'"))
    lease_deadline: Mapped[datetime | None] = mapped_column(DateTime)
    # 版本与审计（§5.1）。legacy_reminder_id 是 P1 的过渡锚点：迁移脚本与双写都靠
    # 它把一行 work_items 挂回 reminders（去掉 reminders 后本列即可废弃）。
    schema_version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    record_version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    created_by: Mapped[str] = mapped_column(Text, server_default=text("''"))
    created_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    legacy_reminder_id: Mapped[int | None] = mapped_column(Integer)


class WorkItemRunRow(MetaBase):
    """P1 每次执行明细（§4.2：**一次 run = 一次投递**；字段组见 §5.6）。

    §5.6 的执行快照（model / persona_version / prompt_hash / context_hash /
    feature_flags_snapshot）、工具明细（``tool_calls[]``）与成本（tokens / cost）
    **不建列**：P1 的投递观测拿不到这些事实，先落事件 payload 或不落（§5.8 规则 2）。

    ``trace_ref`` 写 **``terminal_event:<event_id>``**（§4.2 / §5.9）：**不用
    ``sequence``**——存量有重号与空缺，它不是身份（§5.10）。
    """

    __tablename__ = "work_item_runs"
    __table_args__ = (
        UniqueConstraint("run_id", name="ux_work_item_run_id"),
        # 左前缀即 work_item_id（按任务查执行明细），run_seq 给出第几次。
        Index("ix_work_item_runs_item_seq", "work_item_id", "run_seq"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(Text)
    work_item_id: Mapped[str] = mapped_column(Text)
    run_seq: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    trigger_source: Mapped[str] = mapped_column(Text, server_default=text("'timer'"))
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    duration_ms: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    outcome: Mapped[str] = mapped_column(Text, server_default=text("''"))
    trace_ref: Mapped[str] = mapped_column(Text, server_default=text("''"))
    send_ids_json: Mapped[str] = mapped_column(Text, server_default=text("'[]'"))
    lease_wait_ms: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    delay_reason: Mapped[str] = mapped_column(Text, server_default=text("''"))
    reply_text: Mapped[str] = mapped_column(Text, server_default=text("''"))
    delivered_external_msg_id: Mapped[str] = mapped_column(Text, server_default=text("''"))
    error_class: Mapped[str] = mapped_column(Text, server_default=text("''"))
    error_message: Mapped[str] = mapped_column(Text, server_default=text("''"))


class CommandRecordRow(MetaBase):
    """DB form of ``core.terminal.command_ledger.CommandRecord`` (Phase 2).

    The append-only event stream is the fact layer; this table is the audit /
    query projection for commands (questions, requests and explicit
    instructions).  Status may change over time, but history is never deleted.
    """

    __tablename__ = "command_records"
    __table_args__ = (
        UniqueConstraint("command_id", name="ux_command_record_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    command_id: Mapped[str] = mapped_column(Text, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    issuer_person_id: Mapped[str] = mapped_column(Text, index=True, default="")
    issuer_account_id: Mapped[str] = mapped_column(Text, default="")
    issuer_role: Mapped[str] = mapped_column(Text, index=True, default="ordinary")
    origin_platform: Mapped[str] = mapped_column(Text, index=True, default="")
    origin_bot_account_id: Mapped[str] = mapped_column(Text, index=True, default="")
    origin_chat_id: Mapped[str] = mapped_column(Text, index=True, default="")
    origin_chat_type: Mapped[str] = mapped_column(Text, default="private")
    raw_request: Mapped[str | None] = mapped_column(Text, nullable=True)
    clarified_spec_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(Text, index=True, default="received")
    identity_mode: Mapped[str] = mapped_column(Text, default="as_yinyue")
    nominal_name: Mapped[str] = mapped_column(Text, default="")
    target_person_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    action_type: Mapped[str] = mapped_column(Text, default="")
    execution_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    group_queryable_after: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    public_render_policy: Mapped[str] = mapped_column(Text, default="as_yinyue")
    authorization_id: Mapped[str] = mapped_column(Text, index=True, default="")
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.now)


class AuthorizationRecordRow(MetaBase):
    """Owner-only authorization evidence for one command."""

    __tablename__ = "authorization_records"
    __table_args__ = (
        UniqueConstraint("authorization_id", name="ux_authorization_record_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    authorization_id: Mapped[str] = mapped_column(Text, unique=True, index=True)
    command_id: Mapped[str] = mapped_column(Text, index=True)
    raw_owner_conversation_json: Mapped[str] = mapped_column(Text, default="[]")
    clarification_events_json: Mapped[str] = mapped_column(Text, default="[]")
    final_confirmation: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner_only: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class CommandQueryLogRow(MetaBase):
    """Append-only log of command-query actions (the query itself is an event)."""

    __tablename__ = "command_query_logs"
    __table_args__ = (
        UniqueConstraint("query_id", name="ux_command_query_log_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    query_id: Mapped[str] = mapped_column(Text, unique=True, index=True)
    requester_person_id: Mapped[str] = mapped_column(Text, index=True, default="")
    requester_account_id: Mapped[str] = mapped_column(Text, default="")
    queried_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    command_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    context_json: Mapped[str] = mapped_column(Text, default="{}")


class MemoryProjectionRow(MetaBase):
    """One extracted memory projection over ``terminal_events`` (Phase 2).

    Event rows are the fact layer.  This table stores the deterministic
    projection (fact / episode / relationship / self) that recall should read
    first; ``source_event_ids_json`` keeps every item explainable.
    """

    __tablename__ = "memory_projections"
    __table_args__ = (
        UniqueConstraint("memory_id", name="ux_memory_projection_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    memory_id: Mapped[str] = mapped_column(Text, unique=True, index=True)
    memory_type: Mapped[str] = mapped_column(Text, index=True, default="episode")
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    platform: Mapped[str] = mapped_column(Text, index=True, default="")
    bot_account_id: Mapped[str] = mapped_column(Text, index=True, default="")
    subject_person_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    source_event_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    event_sequence: Mapped[int] = mapped_column(Integer, index=True, default=0)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    visibility_scope_json: Mapped[str] = mapped_column(Text, default='["public"]')
    sensitivity: Mapped[str] = mapped_column(Text, index=True, default="normal")
    linked_component_id: Mapped[str] = mapped_column(Text, index=True, default="")
    link_revision: Mapped[int] = mapped_column(Integer, default=0)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.now)


class MemoryProjectionCursor(MetaBase):
    """Incremental projection cursor: last ``(sequence, id)`` consumed (ENG-23).

    ``sequence`` alone is not a total order on ``terminal_events``（B6 前有重号），
    只存它就会漏读同号的后续行。``last_id`` 是 ``terminal_events.id`` 的配套位；
    老行 ``last_id = 0``（Migration 53 默认值）等于旧语义。
    """

    __tablename__ = "memory_projection_cursors"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    last_sequence: Mapped[int] = mapped_column(Integer, default=0)
    last_id: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.now)


class AnalysisProjectionRow(MetaBase):
    """One Track B analysis projection over ``terminal_events``.

    The legacy account-level ``analysis`` table stays available while the
    migration is in shadow mode.  This table is the event-sourced replacement:
    every row records the source event ids that produced the conclusion.
    """

    __tablename__ = "analysis_projections"
    __table_args__ = (
        UniqueConstraint("projection_id", name="ux_analysis_projection_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    projection_id: Mapped[str] = mapped_column(Text, unique=True, index=True)
    projection_type: Mapped[str] = mapped_column(Text, index=True, default="")
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    platform: Mapped[str] = mapped_column(Text, index=True, default="")
    bot_account_id: Mapped[str] = mapped_column(Text, index=True, default="")
    chat_id: Mapped[str] = mapped_column(Text, index=True, default="")
    chat_type: Mapped[str] = mapped_column(Text, default="")
    source_event_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    visibility_scope_json: Mapped[str] = mapped_column(Text, default='["owner_only"]')
    sensitivity: Mapped[str] = mapped_column(Text, index=True, default="normal")
    event_sequence: Mapped[int] = mapped_column(Integer, index=True, default=0)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.now)


class AnalysisProjectionCursor(MetaBase):
    """Incremental cursor for the Track B analysis worker (ENG-23 pair key)."""

    __tablename__ = "analysis_projection_cursors"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    last_sequence: Mapped[int] = mapped_column(Integer, default=0)
    last_id: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.now)


class PersonObservation(MetaBase):
    """Evidence-backed observation about one canonical person.

    This table is platform-neutral: ``platform``/``account_id`` are always
    present, and identity values live on ``person_entities`` rather than here.
    """

    __tablename__ = "person_observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    observation_id: Mapped[str] = mapped_column(Text, unique=True, index=True)
    person_id: Mapped[str] = mapped_column(Text, index=True)
    platform: Mapped[str] = mapped_column(Text, index=True, default="")
    account_id: Mapped[str] = mapped_column(Text, index=True, default="")
    occurred_scope_type: Mapped[str] = mapped_column(Text, default="unknown")
    occurred_scope_id: Mapped[str] = mapped_column(Text, index=True, default="")
    speaker_person_id: Mapped[str] = mapped_column(Text, index=True, default="")
    mentioned_person_ids_json: Mapped[str | None] = mapped_column(Text)
    source_evidence_id: Mapped[str] = mapped_column(Text, default="")
    source_message_id: Mapped[str] = mapped_column(Text, default="")
    observed_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    kind: Mapped[str] = mapped_column(Text, default="unknown")
    content: Mapped[str | None] = mapped_column(Text)
    raw_quote: Mapped[str | None] = mapped_column(Text)
    normalized_key: Mapped[str] = mapped_column(Text, index=True, default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    importance: Mapped[float] = mapped_column(Float, default=0.5)
    visibility: Mapped[str] = mapped_column(Text, default="group_shared")
    sensitivity: Mapped[str] = mapped_column(Text, default="normal")
    status: Mapped[str] = mapped_column(Text, default="active", index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    model_version: Mapped[str] = mapped_column(Text, default="")
    evidence_ids_json: Mapped[str | None] = mapped_column(Text)
    extra_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.now)


class PersonObservationLink(MetaBase):
    """Normalized many-to-many link between an observation and every person it
    involves.  Keeping this separate avoids JSON lookups for the common query
    ``show me observations involving person P``.
    """

    __tablename__ = "person_observation_links"
    __table_args__ = (
        UniqueConstraint(
            "observation_id", "person_id", "relation",
            name="ux_person_observation_link",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    observation_id: Mapped[str] = mapped_column(Text, index=True)
    person_id: Mapped[str] = mapped_column(Text, index=True)
    relation: Mapped[str] = mapped_column(Text)  # subject / speaker / mentioned


class PersonProfileSnapshot(MetaBase):
    """Latest aggregated profile for one person in one scope."""

    __tablename__ = "person_profile_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "person_id", "platform", "account_id", "scope_type", "scope_id",
            name="ux_person_profile_scope",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    profile_id: Mapped[str] = mapped_column(Text, unique=True, index=True)
    person_id: Mapped[str] = mapped_column(Text, index=True)
    platform: Mapped[str] = mapped_column(Text, index=True, default="")
    account_id: Mapped[str] = mapped_column(Text, index=True, default="")
    scope_type: Mapped[str] = mapped_column(Text, default="global")
    scope_id: Mapped[str] = mapped_column(Text, index=True, default="")
    summary: Mapped[str | None] = mapped_column(Text)
    style: Mapped[str | None] = mapped_column(Text)
    topics_json: Mapped[str | None] = mapped_column(Text)
    active_hours_json: Mapped[str | None] = mapped_column(Text)
    emotion_tendency: Mapped[str | None] = mapped_column(Text)
    relationship_tone: Mapped[str | None] = mapped_column(Text)
    group_map_json: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    observation_count: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.now)


class MessageEvidence(MetaBase):
    """Cross-account index of messages that can back a claim or decision.

    ``messages`` remain the source of truth in account databases.  This table
    is a projection used by the evidence/person/conversation layers so they can
    resolve "which message is this?" without opening every account database.
    """

    __tablename__ = "message_evidence"
    __table_args__ = (
        UniqueConstraint(
            "platform", "account_id", "chat_id", "message_id",
            name="ux_message_evidence_identity",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    platform: Mapped[str] = mapped_column(Text, index=True)
    account_id: Mapped[str] = mapped_column(Text, index=True)
    chat_id: Mapped[str] = mapped_column(Text, index=True)
    message_id: Mapped[str] = mapped_column(Text)
    person_id: Mapped[str | None] = mapped_column(Text, index=True)
    role: Mapped[str] = mapped_column(Text, default="user", index=True)
    source: Mapped[str] = mapped_column(Text, default="message")
    kind: Mapped[str] = mapped_column(Text, default="text")
    content_hash: Mapped[str | None] = mapped_column(Text)
    session_id: Mapped[int | None] = mapped_column(Integer, index=True)
    topic_id: Mapped[int | None] = mapped_column(Integer, index=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    indexed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    metadata_json: Mapped[str | None] = mapped_column(Text)



class ConversationWindowRecord(MetaBase):
    """Durable multi-turn conversation window for one person in one chat."""

    __tablename__ = "conversation_windows"
    __table_args__ = (
        UniqueConstraint(
            "platform", "account_id", "chat_id", "person_id",
            name="ux_conversation_window_scope",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    platform: Mapped[str] = mapped_column(Text, index=True)
    account_id: Mapped[str] = mapped_column(Text, index=True)
    chat_id: Mapped[str] = mapped_column(Text, index=True)
    person_id: Mapped[str] = mapped_column(Text, index=True)
    state: Mapped[str] = mapped_column(Text, default="idle")
    topic: Mapped[str] = mapped_column(Text, default="")
    last_bot_message_id: Mapped[str] = mapped_column(Text, default="")
    last_send_log_id: Mapped[str] = mapped_column(Text, default="")
    last_bot_asked_question: Mapped[int] = mapped_column(Integer, default=0)
    last_person_message_id: Mapped[str] = mapped_column(Text, default="")
    opened_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    turn_count: Mapped[int] = mapped_column(Integer, default=0)
    bot_turn_count: Mapped[int] = mapped_column(Integer, default=0)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class EvidenceIndexCursor(MetaBase):
    """Resumable cursor for the evidence indexing worker.

    Messages are synced per account and ordered by ``(synced_at, id)``.  The
    worker stores the last fully indexed boundary so a restart, a process
    crash, or a later sync only scans what is new instead of re-reading the
    whole sync window.  Re-indexing the same page is safe because
    ``message_evidence`` upserts by message identity.
    """

    __tablename__ = "evidence_index_cursors"
    __table_args__ = (
        UniqueConstraint(
            "platform", "account_id",
            name="ux_evidence_index_cursor_scope",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    platform: Mapped[str] = mapped_column(Text, index=True)
    account_id: Mapped[str] = mapped_column(Text, index=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_message_id: Mapped[int] = mapped_column(Integer, default=0)
    indexed_total: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)



class FrictionEvent(MetaBase):
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


class Topic(MetaBase):
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


class Thread(MetaBase):
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


class ThreadMsgRef(MetaBase):
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


class SessionFocusState(MetaBase):
    """N3 焦点状态：**每会话一行**的最小跨轮状态（docs/全链路架构审计与目标架构.md §2.2 L4）。

    这是全系统**唯一允许落库的判定状态**。``docs/话题簇与锚点规格.md`` §十二
    【已定 1】经 R-T5 修订为「**除焦点状态外仍不落库**」：话题簇与锚点判定照旧
    读时派生（``core/context/threads.py``，P1 落地），这里只留"这一轮在聊哪件事、
    主角是谁"的一句话快照，供下一轮取用。

    **丢行只损失"一句确认"**：本表缺失 / 读不到时，调用方必须退回"当轮判定"，
    **不得**因此拒绝服务（``db/repo_focus.py::FocusStateRepository.get`` 的降级契约）。

    列范围只建「N3 读回一句话」所需的最小子集；``suspended_json`` 存挂起话题数组
    （JSON 文本，读侧解析失败按空数组处理）。时间列一律 **naive 本地墙钟**
    （与工作台账 ``work_items`` 同口径，见 docs/任务模型.md §5.4）。

    所有非空列都带 ``server_default``：迁移 54 的建表 DDL 与本模型必须**同形**
    （列名 / 类型 / NOT NULL / DEFAULT / 索引全对齐），由 ``tests/test_focus_state.py``
    的 parity 用例锁住——ENG-17 就是「迁移 nullable vs ``create_all`` NOT NULL」
    那类建表漂移。
    """

    __tablename__ = "session_focus"
    __table_args__ = (
        # 复合主键已给出唯一性；两个复查索引是**读法驱动的**：
        # ① 按人回查"最近在说谁"；② 按更新时间清扫陈旧行（N3 的维护任务）。
        Index("ix_session_focus_anchor", "current_anchor_person_id"),
        Index("ix_session_focus_updated", "updated_at"),
    )

    # ---- 身份（复合主键 = 一个会话一行；列顺序与模型声明顺序一致，parity 会比对）
    platform: Mapped[str] = mapped_column(Text, primary_key=True, server_default=text("''"))
    account_id: Mapped[str] = mapped_column(Text, primary_key=True, server_default=text("''"))
    chat_id: Mapped[str] = mapped_column(Text, primary_key=True, server_default=text("''"))
    # ---- 焦点（当前一句话 / 当前主角；主角可以是群里的任何人，故不加外键）
    current_topic: Mapped[str] = mapped_column(Text, server_default=text("''"))
    current_anchor_person_id: Mapped[str] = mapped_column(Text, server_default=text("''"))
    # ---- 上一焦点（换话题时把旧焦点压到这里）与挂起话题数组（JSON 文本）
    previous_topic: Mapped[str] = mapped_column(Text, server_default=text("''"))
    suspended_json: Mapped[str] = mapped_column(Text, server_default=text("'[]'"))
    # ---- 时间与版本
    topic_since: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)
    #: 单调递增计数：供读侧缓存失效（对齐 core/speech/store.py 的 revision 模式）。
    #: ``bump_revision`` 只动这一列、不动 ``updated_at``；``upsert`` 顺带 +1。
    revision: Mapped[int] = mapped_column(Integer, server_default=text("1"))


class RuleUseLedger(MetaBase):
    """N4 规则引擎的 ``quota`` 维：**一次动作 = 一行**的追加式用量台账。

    为什么必须落库而不是只放内存：``quota`` 是「次数」维（``max_per_hour`` /
    ``max_per_day`` / ``cooldown_seconds``），跨进程、跨重启都要算数——提醒配额
    （``NON_OWNER_PENDING_CAP``）与主动开口限频（``ProactiveSendLog``）本来就是
    "重启不重置"的语义，规则引擎只是把这两处的常数上移到数据行。

    **只追加、不更新、不删除**：任何一行就是一次真的发生过的动作。计数一律按
    时间窗 ``used_at >= :since`` 现算（滚动 1 小时 / 24 小时），所以没有"窗口边界
    要不要重置"的第二套语义。

    **读不到就按 0**（表不存在 / 查询失败都不许因此拒绝动作，同 ``output_gate``
    兜底口径）：``core/rules/engine.py::use_count`` 在同进程回退计数里记录。

    时间列一律 **naive 本地墙钟**（与 ``work_items`` / ``session_focus`` 同口径）。
    所有非空列都带 ``server_default``：迁移 55 的建表 DDL 与本模型必须**同形**
    （列名 / 类型 / NOT NULL / DEFAULT / 索引全对齐），由
    ``tests/test_rules_engine.py`` 的 parity 用例锁住——ENG-17 就是
    「迁移 nullable vs ``create_all`` NOT NULL」那类建表漂移。
    """

    __tablename__ = "rule_use_ledger"
    __table_args__ = (
        # 读法驱动：① 按 quota_key 在时间窗内计数（quota 的每一条判据都走它）；
        # ② 按 rule_key 出"这条规则用了几次"的读数（Web 生效提示/审计用）。
        Index("ix_rule_use_quota_used", "quota_key", "used_at"),
        Index("ix_rule_use_rule_key", "rule_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rule_key: Mapped[str] = mapped_column(Text, server_default=text("''"))
    #: 计数口径 = 动作 × 平台 × 账号 × 会话 × 发起人（见 ``ActionContext.quota_key``）。
    quota_key: Mapped[str] = mapped_column(Text, server_default=text("''"))
    platform: Mapped[str] = mapped_column(Text, server_default=text("''"))
    account_id: Mapped[str] = mapped_column(Text, server_default=text("''"))
    chat_id: Mapped[str] = mapped_column(Text, server_default=text("''"))
    actor_id: Mapped[str] = mapped_column(Text, server_default=text("''"))
    action_id: Mapped[str] = mapped_column(Text, server_default=text("''"))
    used_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.now)


class ClaimLedgerRow(MetaBase):
    """M2-4 claim 台账：**一行 = 一次有来源的声明**（事实权威序的载体）。

    `docs/archive/任务清单_2026-10-06.md:71` 要的三件事各占一组列：

    * **三级权威**（``authority``：``self_report`` > ``relay`` > ``speculation``）：
      同一 ``(platform, account_id, chat_id, person_key, field_key)`` 上读侧只取
      **仍有效**的行（``superseded_by = 0 and withdrawn_at = ''``），写侧另有一道
      "低权威不得覆盖高权威"的拒绝——两侧合起来就是"权威序"；
    * **更正历史**：``superseded_by`` / ``supersede_reason`` / ``superseded_at``
      （被更新）+ ``withdrawn_at`` / ``withdrawn_reason`` / ``withdrawn_evidence``
      （被撤回）——**旧行的 ``value`` 一旦写入就不再被 UPDATE**，"不得静默改写"
      在 schema 层面就是这个形状；
    * **内容归并**：``content_key`` = ``core.context.message_facts.content_key``
      （字面归一化：去标点/符号后 casefold）。原"近 N 条里第几次复述"的读数已随
      复读指纹退役（2026-10-24，A6），本列只留归并语义。

    溯源列（``source_kind`` / ``source_actor_id`` / ``source_chat_id`` /
    ``source_message_id`` / ``source_claim_id``）指向**原始证据**：本表**不是**
    第四份事实仓库，不复制 ``person_notes`` / ``person_observations`` /
    ``memory_projections`` 的正文（见 ``core/context/message_facts.py`` 文件头）。

    时间列一律 **naive 本地墙钟**（与 ``work_items`` / ``session_focus`` /
    ``rule_use_ledger`` 同口径）。所有非空列都带 ``server_default``：迁移 56 的
    建表 DDL 与本模型必须**同形**（列名 / 类型 / NOT NULL / DEFAULT / 索引全对齐），
    由 ``tests/test_claim_ledger.py`` 的 parity 用例锁住——ENG-17 就是
    「迁移 nullable vs ``create_all`` NOT NULL」那类建表漂移。
    """

    __tablename__ = "claim_ledger"
    __table_args__ = (
        # 读法驱动：① 权威序查询（会话 + 人 + 字段）；② 复述抑制
        # （同一 (chat, content_key) 出现了几次）；③ 更正链回查。
        Index("ix_claim_ledger_scope", "platform", "account_id", "chat_id",
              "person_key", "field_key"),
        Index("ix_claim_ledger_content", "chat_id", "content_key"),
        Index("ix_claim_ledger_superseded", "superseded_by"),
        Index("ix_claim_ledger_source", "source_claim_id"),
        Index("ix_claim_ledger_created", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # ---- 作用域（一个会话一份权威序）
    platform: Mapped[str] = mapped_column(Text, server_default=text("''"))
    account_id: Mapped[str] = mapped_column(Text, server_default=text("''"))
    chat_id: Mapped[str] = mapped_column(Text, server_default=text("''"))
    # ---- 主题与取值
    person_key: Mapped[str] = mapped_column(Text, server_default=text("''"))
    field_key: Mapped[str] = mapped_column(Text, server_default=text("''"))
    value: Mapped[str] = mapped_column(Text, server_default=text("''"))
    content_key: Mapped[str] = mapped_column(Text, server_default=text("''"))
    # ---- 权威级 + 溯源
    authority: Mapped[str] = mapped_column(
        Text, server_default=text("'speculation'"))
    source_kind: Mapped[str] = mapped_column(Text, server_default=text("''"))
    source_actor_id: Mapped[str] = mapped_column(Text, server_default=text("''"))
    source_chat_id: Mapped[str] = mapped_column(Text, server_default=text("''"))
    source_message_id: Mapped[str] = mapped_column(Text, server_default=text("''"))
    source_claim_id: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    observed_at: Mapped[datetime | None] = mapped_column(DateTime)
    # ---- 更正链（更新 / 撤回；旧正文永不改写）
    superseded_by: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    supersede_reason: Mapped[str] = mapped_column(Text, server_default=text("''"))
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime)
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime)
    withdrawn_reason: Mapped[str] = mapped_column(Text, server_default=text("''"))
    withdrawn_evidence: Mapped[str] = mapped_column(Text, server_default=text("''"))
    created_at: Mapped[datetime | None] = mapped_column(DateTime)

    # ---- 原话与 span（**写时不承诺**收口，迁移 59）
    # 字段抽取（field_key/value）是**可重建的派生缓存**；原话与位置才是基质：
    # `source_text` 是原文的有上限副本，真相仍在消息表里 ——
    # `source_message_id` + `[evidence_start:evidence_end]` 可回到现场。
    source_text: Mapped[str] = mapped_column(Text, server_default=text("''"))
    evidence_text: Mapped[str] = mapped_column(Text, server_default=text("''"))
    evidence_start: Mapped[int] = mapped_column(Integer, server_default=text("-1"))
    evidence_end: Mapped[int] = mapped_column(Integer, server_default=text("-1"))


class ClaimLedgerAuditRow(MetaBase):
    """claim 台账的**拒绝/重复**审计：被拒的那次写入也要留痕（不得静默丢弃）。

    「低权威不得覆盖高权威」有两种落地方式：静默忽略，或者拒绝 + 记账。规格要的是
    后者（`docs/archive/任务清单_2026-10-06.md:71` 的"**不得静默改写**"）。这张表就是那本账：一次被拒的
    写入 = 一行，读侧可以回答"今天有几次转述想盖掉自报"。
    """

    __tablename__ = "claim_ledger_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: ``inserted`` / ``duplicate`` / ``superseded`` / ``rejected``（见
    #: ``core.context.message_facts.RecordOutcome``）。
    action: Mapped[str] = mapped_column(Text, server_default=text("''"))
    chat_id: Mapped[str] = mapped_column(Text, server_default=text("''"))
    person_key: Mapped[str] = mapped_column(Text, server_default=text("''"))
    field_key: Mapped[str] = mapped_column(Text, server_default=text("''"))
    value: Mapped[str] = mapped_column(Text, server_default=text("''"))
    authority: Mapped[str] = mapped_column(Text, server_default=text("''"))
    existing_claim_id: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    existing_authority: Mapped[str] = mapped_column(Text, server_default=text("''"))
    reason: Mapped[str] = mapped_column(Text, server_default=text("''"))
    created_at: Mapped[datetime | None] = mapped_column(DateTime)


class SelfMoveRow(MetaBase):
    """WP-STATE：她发出的每一条回复（追加式台账；一行 = 一次发送）。

    口径：只记"她自己的行为"，不记用户正文；来源是发送成功后的
    ``core.routing.turn_pipeline._send_reply_impl``。时间列一律本地 naive 墙钟
    （与 session_focus / work_items 同口径）。表结构必须与迁移 57 同形，
    由 tests/test_state_store.py 的 PRAGMA parity 锁住。
    """

    __tablename__ = "self_moves"
    __table_args__ = (
        Index("ux_self_moves_dedup", "platform", "account_id", "chat_id",
              "content_hash", "sent_at_sec", unique=True),
        Index("ix_self_moves_chat_time", "platform", "account_id", "chat_id", "sent_at"),
        Index("ix_self_moves_kind_time", "chat_id", "move_kind", "sent_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    platform: Mapped[str] = mapped_column(Text, server_default=text("''"))
    account_id: Mapped[str] = mapped_column(Text, server_default=text("''"))
    chat_id: Mapped[str] = mapped_column(Text, server_default=text("''"))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    sent_at_sec: Mapped[datetime | None] = mapped_column(DateTime)
    move_kind: Mapped[str] = mapped_column(Text, server_default=text("'other'"))
    bit_keys_json: Mapped[str] = mapped_column(Text, server_default=text("'[]'"))
    content_hash: Mapped[str] = mapped_column(Text, server_default=text("''"))
    chars: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    expects_reply: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    engagement: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    engaged_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.now)


class ChatStateRow(MetaBase):
    """WP-STATE：每会话一行（最后一次用户消息 / 她最后一次回复 / 回应状态）。

    只按时间戳判定"用户有没有回应上一条"，不解析文本；读失败必须降级为 None
    （见 db/repo_state.py 的降级契约），不得因此拒绝服务。
    """

    __tablename__ = "chat_state"

    platform: Mapped[str] = mapped_column(Text, primary_key=True, server_default=text("''"))
    account_id: Mapped[str] = mapped_column(Text, primary_key=True, server_default=text("''"))
    chat_id: Mapped[str] = mapped_column(Text, primary_key=True, server_default=text("''"))
    last_user_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_bot_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_bot_move_kind: Mapped[str] = mapped_column(Text, server_default=text("''"))
    last_bot_bit_keys_json: Mapped[str] = mapped_column(Text, server_default=text("'[]'"))
    last_bot_content_hash: Mapped[str] = mapped_column(Text, server_default=text("''"))
    last_bot_expects_reply: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    last_bot_engagement: Mapped[str] = mapped_column(Text, server_default=text("'none'"))
    last_bot_engaged_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)
    revision: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    #: 「翻篇边界」：晚于它的动作才进"最近用过/冷却"；边界本身由主人明确指令触发。
    reset_at: Mapped[datetime | None] = mapped_column(DateTime)




class UtteranceEdge(MetaBase):
    """一条入站消息的「对谁说」事实（task-43，方案 B：单独表，不进 terminal_events）。

    为什么单独一张表
    ----------------
    ``terminal_events`` 是 append-only 的**事实流**，它的列必须是"每条消息都有"
    的维度；而"这条消息在回谁"天然稀疏（生产实测只有 11.3% 的入站消息带引用），
    用三态 NULL 去表达"没有边"会把审计表拖进语义泥潭。这里用「没有边 = 没有行」
    表达同一件事：回滚就是 ``DROP TABLE utterance_edges`` 一行，删掉它不动任何
    既有取值。

    为什么 ``speaker`` 从来不是收件人
    ------------------------------
    这张表最容易犯的错是"谁发的就当成发给谁的"。``recipient_kind`` 只允许由
    ``evidence`` 列写明的证据推出；群里既没有 @ 也没有引用时必须是
    ``unknown`` + ``none``——宁可不知道，也不许猜（见
    ``core/terminal/utterance_edges.py`` 的 ``build_edges``）。

    已知的坑（task-42 生产实测，写代码时不要"图省事"绕开）
    ---------------------------------------------------
    * 微信 ``messages.quote_reply_user`` **不可用**：主群 572 条引用里 558 条填的
      是 chat_id 自身。作者一律走 ``quote_reply_id → messages.msg_id →
      sender_name``（实测 93.4% 可解）。
    * 微信 ``messages.mentions`` 恒为空，@ 在**正文**里（``@鲸鲸 信息轰炸…``）：
      靠显示名精确匹配从正文解析（``evidence='at_body'``）。

    Migration 62 的建表 DDL 必须与本模型**同形**（列名 / 类型 / NOT NULL /
    DEFAULT / 索引全对齐），由 ``tests/test_utterance_edges.py`` 的 parity 用例
    锁住——ENG-17 就是"迁移 nullable vs create_all NOT NULL"的建表漂移。
    """

    __tablename__ = "utterance_edges"
    __table_args__ = (
        # 会话维度必须带账号：同一群里两个账号看到的是同一条 msg_id，缺 bot_account_id
        # 会撞键、静默丢一行（2026-10-21 由 scripts/check_identity_keys.py 规则 D 抓到）。
        UniqueConstraint(
            "platform", "bot_account_id", "chat_id", "msg_id",
            name="ux_utterance_edge_msg",
        ),
        Index("ix_utterance_edges_quoted", "quoted_msg_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    platform: Mapped[str] = mapped_column(
        Text, index=True, default="", server_default=text("''")
    )
    bot_account_id: Mapped[str] = mapped_column(
        Text, default="", server_default=text("''")
    )
    chat_id: Mapped[str] = mapped_column(
        Text, index=True, default="", server_default=text("''")
    )
    chat_type: Mapped[str] = mapped_column(Text, default="", server_default=text("''"))
    msg_id: Mapped[str] = mapped_column(Text, default="", server_default=text("''"))
    event_id: Mapped[str] = mapped_column(Text, default="", server_default=text("''"))
    turn_id: Mapped[str] = mapped_column(Text, default="", server_default=text("''"))
    speaker_id: Mapped[str] = mapped_column(Text, default="", server_default=text("''"))
    speaker_name: Mapped[str] = mapped_column(Text, default="", server_default=text("''"))
    recipient_kind: Mapped[str] = mapped_column(
        Text, default="unknown", server_default=text("'unknown'")
    )
    recipient_id: Mapped[str] = mapped_column(Text, default="", server_default=text("''"))
    recipient_name: Mapped[str] = mapped_column(Text, default="", server_default=text("''"))
    quoted_msg_id: Mapped[str] = mapped_column(Text, default="", server_default=text("''"))
    quoted_speaker_id: Mapped[str] = mapped_column(
        Text, default="", server_default=text("''")
    )
    quoted_speaker_name: Mapped[str] = mapped_column(
        Text, default="", server_default=text("''")
    )
    evidence: Mapped[str] = mapped_column(Text, default="none", server_default=text("'none'"))
    evidence_detail_json: Mapped[str] = mapped_column(
        Text, default="{}", server_default=text("'{}'")
    )
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.now)


def init_meta_db(db_path: str):
    from sqlalchemy import event

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

    MetaBase.metadata.create_all(engine)
    from db.migrations import run_meta_migrations
    run_meta_migrations(db_path)

    return engine


class TurnTrace(MetaBase):
    """E0 判决行留痕（U-01）：每回合一行「decision + basis」。

    旁注，不是账本正行——判决是她自己的决定，不是外部世界发生的事实。
    迁移 63 的 DDL 必须与本模型同形（列名 / 类型 / NOT NULL / 默认值）。
    ``turn_id`` 是主键：每个回合一行，重复写是 no-op（不覆盖、不追加）。
    """

    __tablename__ = "turn_trace"

    turn_id: Mapped[str] = mapped_column(Text, primary_key=True)
    chat_id: Mapped[str] = mapped_column(Text, default="", server_default=text("''"))
    decision: Mapped[str] = mapped_column(
        Text, default="silent", server_default=text("'silent'")
    )
    basis: Mapped[str] = mapped_column(Text, default="[]", server_default=text("'[]'"))
    mode: Mapped[str] = mapped_column(Text, default="off", server_default=text("'off'"))
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)
