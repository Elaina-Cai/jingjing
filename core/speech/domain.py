from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal


class ScopeType(StrEnum):
    GLOBAL = "global"
    PLATFORM = "platform"
    ACCOUNT = "account"
    CHAT_TYPE = "chat_type"
    CONTACT = "contact"
    GROUP = "group"
    CHAT = "chat"


class AudienceKind(StrEnum):
    OWNER_PRIVATE = "owner_private"
    NON_OWNER_PRIVATE = "non_owner_private"
    GROUP_MENTIONED = "group_mentioned"
    GROUP_OWNER = "group_owner"
    GROUP_AMBIENT = "group_ambient"
    GROUP_PAT = "group_pat"
    PROACTIVE = "proactive"
    WEB_CHAT = "web_chat"


class RuleCategory(StrEnum):
    TOPIC = "topic"
    SENSITIVE = "sensitive"
    PRIVACY = "privacy"
    ACCOUNT = "account"
    TARGET = "target"
    TOOL = "tool"
    FORMAT = "format"
    RATE = "rate"
    STYLE = "style"
    ENGAGEMENT = "engagement"


class Enforcement(StrEnum):
    PROMPT = "prompt"
    DETERMINISTIC = "deterministic"
    HYBRID = "hybrid"
    SEMANTIC = "semantic"


class RuleEffect(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE = "require"
    LIMIT = "limit"
    REWRITE = "rewrite"
    LOG_ONLY = "log_only"


class PolicyAction(StrEnum):
    ALLOW = "allow"
    BLOCK = "block"
    DROP = "drop"
    REWRITE = "rewrite"
    REPLACE = "replace"
    REGENERATE = "regenerate"
    LOG_ONLY = "log_only"


class ReplyMode(StrEnum):
    ALWAYS = "always"
    MENTION = "mention"
    URGENT = "urgent"
    DEFER = "defer"
    NEVER = "never"


class PromptPurpose(StrEnum):
    FAST_REPLY = "fast_reply"
    REACT = "react"
    SYNTHESIS = "synthesis"
    VISION = "vision"
    SYSTEM = "system"


@dataclass(frozen=True)
class ConversationTarget:
    """Canonical address of one conversation turn.

    ``account_id`` is the account that will actually send the reply, not the
    literal choice string ("self" / "owner").
    """

    platform: str
    account_id: str
    chat_id: str
    chat_type: Literal["private", "group"]
    sender_id: str = ""
    sender_name: str = ""
    display_name: str = ""

    @property
    def is_group(self) -> bool:
        return self.chat_type == "group"

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.platform, self.account_id, self.chat_type, self.chat_id)


@dataclass(frozen=True)
class TurnContext:
    request_id: str
    target: ConversationTarget
    audience: AudienceKind
    inbound_text: str = ""
    msg_type: str = "text"
    trigger_reason: str = ""
    session_id: int | None = None
    is_mentioned: bool = False
    is_owner: bool = False
    group_recent_context: str = ""
    sender_role: str = ""
    relationship_tier: str = ""
    intimacy: float | None = None

    @property
    def scene(self) -> str:
        return "group" if self.target.is_group else "private"


@dataclass(frozen=True)
class PromptSection:
    key: str
    template: str
    priority: int = 100
    enabled: bool = True


@dataclass(frozen=True)
class ResolvedPersona:
    persona_id: int
    version: int
    key: str
    name: str
    aliases: tuple[str, ...] = ()
    identity: dict[str, Any] = field(default_factory=dict)
    traits: dict[str, Any] = field(default_factory=dict)
    voice: dict[str, Any] = field(default_factory=dict)
    scene_styles: dict[str, Any] = field(default_factory=dict)
    prompt_sections: tuple[PromptSection, ...] = ()
    source: str = "database"

    def style_for(self, scene: str) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        merged.update(self.scene_styles.get("default", {}) or {})
        merged.update(self.scene_styles.get(scene, {}) or {})
        return merged


@dataclass(frozen=True)
class HardConstraint:
    """A deterministic rule compiled into an executable guard hint."""

    kind: str
    rule_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    priority: int = 500


@dataclass(frozen=True)
class AccountPolicy:
    allowed_accounts: tuple[str, ...] = ("self", "owner")
    allow_cross_chat: bool = False
    require_confirmation: bool = False


@dataclass(frozen=True)
class TargetPolicy:
    allow_cross_chat: bool = False
    denied_sender_ids: tuple[str, ...] = ()
    required_chat_id: str = ""


@dataclass(frozen=True)
class ToolPolicy:
    allowed_tools: tuple[str, ...] | None = None  # None means "no explicit allow-list"
    denied_tools: tuple[str, ...] = ()
    max_calls: int = 0
    sticker_scopes: tuple[str, ...] = ()
    require_confirmation: tuple[str, ...] = ()


@dataclass(frozen=True)
class OutputPolicy:
    max_chars: int = 0
    max_lines: int = 0
    forbidden_terms: tuple[str, ...] = ()
    required_phrases: tuple[str, ...] = ()
    replacement_text: str = ""
    style_examples: str = ""


@dataclass(frozen=True)
class SpeakingPolicy:
    revision: str
    target: ConversationTarget
    audience: AudienceKind
    persona: ResolvedPersona
    prompt_brief: str = ""
    hard_constraints: tuple[HardConstraint, ...] = ()
    account_policy: AccountPolicy = field(default_factory=AccountPolicy)
    target_policy: TargetPolicy = field(default_factory=TargetPolicy)
    tool_policy: ToolPolicy = field(default_factory=ToolPolicy)
    output_policy: OutputPolicy = field(default_factory=OutputPolicy)
    source_rule_ids: tuple[str, ...] = ()
    engagement: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SpeechSnapshot:
    turn: TurnContext
    audience: AudienceKind
    persona: ResolvedPersona
    policy: SpeakingPolicy

    @property
    def scene(self) -> str:
        return self.turn.scene


@dataclass(frozen=True)
class EngagementDecision:
    reply_mode: ReplyMode = ReplyMode.ALWAYS
    defer_seconds: float = 0.0
    reply_reason: str = ""
    rule_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class OutboundResult:
    ok: bool
    output: str = ""
    error: str = ""
    msg_id: str = ""
    decision: PolicyDecision | None = None
    audit_id: int | None = None


@dataclass(frozen=True)
class OutboundDraft:
    request_id: str
    kind: Literal["text", "sticker", "file", "image", "notify"]
    target: ConversationTarget
    account_choice: str = "self"
    account_id: str = ""
    content: str = ""
    filepath: str = ""
    mood_id: str = ""
    emotion: str = ""
    who: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Violation:
    rule_id: str = ""
    category: str = ""
    detail: str = ""
    span: tuple[int, int] | None = None
    replacement: str = ""


@dataclass(frozen=True)
class PolicyDecision:
    action: PolicyAction = PolicyAction.ALLOW
    violations: tuple[Violation, ...] = ()
    rewritten_content: str = ""
    reason: str = ""
    rule_ids: tuple[str, ...] = ()

    @property
    def rule(self) -> str:
        """命中的第一条规则（老字段名 ``rule`` 的兼容读法）。

        真机踩过：``core/speech/outbound.py`` 有多处 ``gate.rule`` / ``gate.rules``，
        而本类字段在重构里改成了 ``rule_ids``，于是每一次发送都在
        ``'PolicyDecision' object has no attribute 'rule'`` 上炸掉——**一个字节都发不出去**。
        """
        return self.rule_ids[0] if self.rule_ids else ""

    @property
    def rules(self) -> tuple[str, ...]:
        """全部命中的规则（老字段名 ``rules`` 的兼容读法）。"""
        return self.rule_ids

    @property
    def allowed(self) -> bool:
        # LOG_ONLY（看板模式）语义 = 记录但不拦：必须计入 allowed，
        # 否则 router 的降级分支会把回复清空（历史上从未有规则用过它）。
        return self.action in (
            PolicyAction.ALLOW,
            PolicyAction.REWRITE,
            PolicyAction.REPLACE,
            PolicyAction.LOG_ONLY,
        )
