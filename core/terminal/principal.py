"""Shadow identity / visibility primitives for the Phase 0 terminal model.

This module is deliberately dependency-free and deterministic.  It encodes the
product rules that were agreed for the super-terminal: account-scoped relations,
explicit account links, query-time sharing, and capability-based privacy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Iterable, Mapping


class RelationLevel(StrEnum):
    OWNER = "owner"
    INTIMATE = "intimate"
    FAMILIAR = "familiar"
    ORDINARY = "ordinary"
    STRANGER = "stranger"
    UNKNOWN = "unknown"


_RELATION_RANK: dict[RelationLevel, int] = {
    RelationLevel.OWNER: 0,
    RelationLevel.INTIMATE: 1,
    RelationLevel.FAMILIAR: 2,
    RelationLevel.ORDINARY: 3,
    RelationLevel.STRANGER: 4,
    RelationLevel.UNKNOWN: 5,
}


class ChatType(StrEnum):
    PRIVATE = "private"
    GROUP = "group"
    UNKNOWN = "unknown"


class VisibilityScope(StrEnum):
    """Where an event/command/memory item may be read."""

    OWNER_ONLY = "owner_only"
    ISSUER_ONLY = "issuer_only"
    GROUP_MEMBERS = "group_members"
    COMPONENT = "component"
    PUBLIC = "public"
    SELF = "self"


class Capability(StrEnum):
    """Hard capabilities checked by data access, tools and outbound."""

    VIEW_OWNER_PRIVATE_MEMORY = "view_owner_private_memory"
    VIEW_COMPONENT_MEMORY = "view_component_memory"
    VIEW_RAW_THOUGHT = "view_raw_thought"
    VIEW_ALL_COMMANDS = "view_all_commands"
    SEND_AS_OWNER = "send_as_owner"
    CONTACT_THIRD_PARTY = "contact_third_party"
    QUERY_GROUP_COMMANDS = "query_group_commands"
    PROACTIVE_MESSAGE = "proactive_message"
    EXECUTE_OWNER_COMMAND = "execute_owner_command"


class CapabilityDenied(PermissionError):
    """Raised when a caller asks for a capability the principal lacks."""

    def __init__(
        self,
        capability: Capability,
        principal: "PrincipalContext | None" = None,
    ):
        self.capability = capability
        self.principal = principal
        person = getattr(principal, "person_id", "") if principal is not None else ""
        super().__init__(
            f"capability denied: {getattr(capability, 'value', capability)}"
            + (f" for {person}" if person else "")
        )


@dataclass(frozen=True, order=True)
class AccountRef:
    platform: str
    account_id: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.platform}:{self.account_id}"


@dataclass(frozen=True)
class RelationshipState:
    platform: str
    bot_account_id: str
    person_id: str
    level: RelationLevel = RelationLevel.UNKNOWN
    intimacy: float = 0.0
    source: str = "shadow"
    manual_override: bool = False


@dataclass(frozen=True)
class GroupMembershipPeriod:
    start_at: datetime
    end_at: datetime | None = None
    epoch: int = 1

    def covers(self, when: datetime) -> bool:
        when = _naive(when)
        start = _naive(self.start_at)
        end = _naive(self.end_at)
        if when < start:
            return False
        if end is not None and when >= end:
            return False
        return True

    def overlaps(self, start_at: datetime, end_at: datetime | None = None) -> bool:
        start_at = _naive(start_at)
        end_at = _naive(end_at)
        my_start = _naive(self.start_at)
        my_end = _naive(self.end_at)
        if end_at is not None and end_at <= my_start:
            return False
        if my_end is not None and start_at >= my_end:
            return False
        return True


def _naive(value: datetime | None) -> datetime | None:
    """归一为本地墙钟 naive，保证 naive/aware 混合输入也能安全比较。

    换算口径唯一 owner = ``core.temporal.to_naive_local``。本模块 docstring 承诺
    「导入期零依赖」（叶子原语，可单独推理），所以只在函数内 import owner，
    不给它加模块级依赖。
    """
    if value is None:
        return None
    from core import temporal

    return temporal.to_naive_local(value)


def _is_closer(candidate: RelationshipState, current: RelationshipState) -> bool:
    if candidate.manual_override != current.manual_override:
        return candidate.manual_override
    c_rank = _RELATION_RANK.get(candidate.level, 99)
    r_rank = _RELATION_RANK.get(current.level, 99)
    if c_rank != r_rank:
        return c_rank < r_rank
    if float(candidate.intimacy) != float(current.intimacy):
        return float(candidate.intimacy) > float(current.intimacy)
    # Deterministic tie-break; account id does not imply closeness.
    return str(candidate.bot_account_id) < str(current.bot_account_id)


def pick_closer_relationship(
    states: Iterable[RelationshipState],
    *,
    manual_source_account: str = "",
) -> RelationshipState | None:
    """Pick the effective relationship for a linked account component.

    Manual override wins.  Otherwise: higher relation level, then intimacy,
    then deterministic account-id tie-break.
    """
    candidates = list(states)
    if not candidates:
        return None
    if manual_source_account:
        for state in candidates:
            if state.bot_account_id == manual_source_account:
                return state
    best = candidates[0]
    for state in candidates[1:]:
        if _is_closer(state, best):
            best = state
    return best


@dataclass(frozen=True)
class PrincipalContext:
    """The deterministic identity context resolved before the model sees a turn."""

    platform: str
    bot_account_id: str
    chat_id: str
    chat_type: ChatType
    person_id: str
    display_name: str
    relation_to_bot: RelationLevel
    intimacy: float = 0.0
    group_id: str = ""
    link_component_id: str = ""
    capabilities: frozenset[Capability] = field(default_factory=frozenset)
    memory_scopes: frozenset[VisibilityScope] = field(default_factory=frozenset)
    occurred_at: datetime | None = None
    metadata: tuple[tuple[str, str], ...] = ()

    @property
    def is_owner(self) -> bool:
        return self.relation_to_bot == RelationLevel.OWNER

    @property
    def is_group(self) -> bool:
        return self.chat_type == ChatType.GROUP

    def has(self, capability: Capability) -> bool:
        return capability in self.capabilities

    def can(self, capability: Capability) -> bool:
        """Alias used by capability gates in tools/outbound/memory."""
        return self.has(capability)

    def require(self, capability: Capability) -> None:
        if not self.has(capability):
            raise CapabilityDenied(capability, self)

    def as_dict(self) -> dict:
        return {
            "platform": self.platform,
            "bot_account_id": self.bot_account_id,
            "chat_id": self.chat_id,
            "chat_type": self.chat_type.value,
            "person_id": self.person_id,
            "display_name": self.display_name,
            "relation_to_bot": self.relation_to_bot.value,
            "intimacy": self.intimacy,
            "group_id": self.group_id,
            "link_component_id": self.link_component_id,
            "capabilities": sorted(c.value for c in self.capabilities),
            "memory_scopes": sorted(s.value for s in self.memory_scopes),
            "occurred_at": self.occurred_at.isoformat() if self.occurred_at else "",
        }
