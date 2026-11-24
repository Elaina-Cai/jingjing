"""Phase 0 shadow terminal primitives.

These modules are read-only prototypes for the identity/event/command model.
They are not wired into the live reply path yet.
"""

from core.terminal.adapters import (
    build_identity_service,
    build_identity_service_from_bootstrap,
)
from core.terminal.authority import (
    AuthorityService,
    active_context_has_capability,
    active_context_is_owner,
    active_context_principal,
    active_has_capability,
    active_is_owner,
    active_principal,
    attach_principal,
    context_principal,
    get_authority_service,
    install_authority_service,
    relation_label,
    require_context_capability,
    reset_authority_service,
    unknown_principal,
)
from core.terminal.command_ledger import (
    AuthorizationRecord,
    CommandAccessDenied,
    CommandLedger,
    CommandQueryLog,
    CommandRecord,
    CommandStatus,
    IdentityMode,
    PublicRenderPolicy,
)
from core.terminal.command_runtime import (
    get_terminal_command_recorder,
    get_terminal_command_service,
    install_terminal_command_service,
    reset_terminal_command_service,
)
from core.terminal.command_service import CommandService
from core.terminal.event_model import (
    Event,
    EventType,
    Sensitivity,
    utcnow as event_utcnow,
)
from core.terminal.event_store_db import TerminalEventStore
from core.terminal.group_membership_service import (
    GroupMembershipService,
    MembershipTransition,
    parse_membership_notice,
)
from core.terminal.event_runtime import (
    get_terminal_event_store,
    install_terminal_event_store,
    reset_terminal_event_store,
)
from core.terminal.identity_service import AccountGrant, IdentityService
from core.terminal.link_graph import AccountLinkGraph
from core.terminal.link_runtime import (
    add_link,
    current_link_revision,
    invalidate_link_caches,
    load_live_link_graph,
    refresh_identity_service_links,
    remove_link,
)
from core.terminal.memory_gateway import (
    MemoryGateway,
    MemoryHit,
    attach_terminal_memory,
    get_memory_gateway,
    install_memory_gateway,
    reset_memory_gateway,
)
from core.terminal.memory_projection import (
    MemoryProjectionItem,
    MemoryProjector,
)
from core.terminal.memory_runtime import (
    get_memory_projector,
    install_memory_projector,
    project_pending_memory,
    reset_memory_projector,
)
from core.terminal.principal import (
    AccountRef,
    Capability,
    CapabilityDenied,
    ChatType,
    GroupMembershipPeriod,
    PrincipalContext,
    RelationLevel,
    RelationshipState,
    VisibilityScope,
    pick_closer_relationship,
)
from core.terminal.persona_brain import (
    build_persona_system_prompt,
)

__all__ = [
    "AccountGrant",
    "AccountLinkGraph",
    "AccountRef",
    "AuthorityService",
    "AuthorizationRecord",
    "Capability",
    "CapabilityDenied",
    "ChatType",
    "CommandAccessDenied",
    "CommandLedger",
    "CommandQueryLog",
    "CommandRecord",
    "CommandService",
    "CommandStatus",
    "Event",
    "EventType",
    "GroupMembershipPeriod",
    "GroupMembershipService",
    "IdentityMode",
    "IdentityService",
    "MemoryGateway",
    "MemoryHit",
    "MemoryProjectionItem",
    "MemoryProjector",
    "MembershipTransition",
    "PrincipalContext",
    "PublicRenderPolicy",
    "RelationLevel",
    "RelationshipState",
    "Sensitivity",
    "TerminalEventStore",
    "VisibilityScope",
    "active_context_has_capability",
    "active_context_is_owner",
    "active_context_principal",
    "active_has_capability",
    "active_is_owner",
    "active_principal",
    "add_link",
    "attach_principal",
    "attach_terminal_memory",
    "build_identity_service",
    "build_identity_service_from_bootstrap",
    "build_persona_system_prompt",
    "context_principal",
    "current_link_revision",
    "event_utcnow",
    "get_authority_service",
    "get_memory_gateway",
    "get_memory_projector",
    "get_terminal_command_recorder",
    "get_terminal_command_service",
    "get_terminal_event_store",
    "invalidate_link_caches",
    "install_authority_service",
    "install_memory_gateway",
    "install_memory_projector",
    "install_terminal_command_service",
    "install_terminal_event_store",
    "load_live_link_graph",
    "parse_membership_notice",
    "pick_closer_relationship",
    "relation_label",
    "refresh_identity_service_links",
    "remove_link",
    "require_context_capability",
    "project_pending_memory",
    "reset_authority_service",
    "reset_memory_gateway",
    "reset_memory_projector",
    "reset_terminal_command_service",
    "reset_terminal_event_store",
    "unknown_principal",
]
