"""Shared event visibility ACL (Phase 5 D cleanup).

One deterministic implementation for every event store: the production SQLite
store, the deleted debug JSONL store and tests all applied the same rules.
"""

from __future__ import annotations

from core.terminal.event_model import Event, Sensitivity
from core.terminal.principal import Capability, PrincipalContext, VisibilityScope


def _is_actor_or_recipient(event: Event, principal: PrincipalContext) -> bool:
    """The principal is either the event's actor or one of its recipients.

    Bot-emitted events carry the bot's account id in ``actor_person_id`` and the
    counterpart in ``recipient_person_ids``, so a private-chat recipient must be
    matched through the recipient list (the actor slot holds the bot, not the
    person who should see the reply).
    """
    person_id = str(principal.person_id or "")
    if not person_id:
        return False
    if str(event.actor_person_id or "") == person_id:
        return True
    return person_id in {str(item) for item in (event.recipient_person_ids or ())}


def _same_platform_account(event: Event, principal: PrincipalContext) -> bool:
    """Chat-scoped visibility never crosses platform or bot account."""
    event_platform = str(event.platform or "")
    principal_platform = str(principal.platform or "")
    if event_platform and principal_platform and event_platform != principal_platform:
        return False
    event_account = str(event.bot_account_id or "")
    principal_account = str(principal.bot_account_id or "")
    if event_account and principal_account and event_account != principal_account:
        return False
    return True


def event_visible(event: Event, principal: PrincipalContext) -> bool:
    """Apply visibility and raw-thought ACLs without DB lookups."""
    if event.sensitivity == Sensitivity.OWNER_CHAIN_OF_THOUGHT:
        return principal.is_owner and principal.has(Capability.VIEW_RAW_THOUGHT)
    scopes = set(event.visibility_scope)
    if VisibilityScope.PUBLIC in scopes:
        return True
    group_context = principal.chat_type.value == "group"
    if VisibilityScope.OWNER_ONLY in scopes:
        return principal.is_owner and not group_context
    if VisibilityScope.SELF in scopes:
        return (
            (principal.is_owner and not group_context)
            or _is_actor_or_recipient(event, principal)
        )
    if VisibilityScope.ISSUER_ONLY in scopes:
        return (
            (principal.is_owner and not group_context)
            or _is_actor_or_recipient(event, principal)
        )
    if VisibilityScope.GROUP_MEMBERS in scopes:
        if not _same_platform_account(event, principal):
            return False
        if principal.is_owner:
            return True
        event_group = str(event.metadata.get("group_id") or event.chat_id or "")
        return (
            principal.chat_type.value == "group"
            and bool(principal.group_id)
            and principal.group_id == event_group
        )
    if VisibilityScope.COMPONENT in scopes:
        if principal.is_owner:
            return True
        event_account = (
            str(event.platform or ""),
            str(event.bot_account_id or ""),
        )
        if event_account[0] and event_account[1]:
            for part in str(principal.link_component_id or "").split("|"):
                part = part.strip()
                if ":" not in part:
                    continue
                platform, account = part.split(":", 1)
                if (platform, account) == event_account:
                    return True
        event_component = str(event.metadata.get("link_component_id") or "")
        return bool(principal.link_component_id) and (
            principal.link_component_id == event_component
        )
    return False


__all__ = ["event_visible"]
