"""Phase 1 deterministic authority gateway.

This is the single entry point the live chain uses to turn raw platform
coordinates (``platform / bot_account_id / chat_id / sender_id``) into a
:class:`PrincipalContext`, and to answer owner/capability questions.

Design rules:

* Authority is always on: every live turn resolves a deterministic
  ``PrincipalContext`` from platform/account/chat/sender coordinates.  Callers
  may still attach an explicit principal (Web/tests) which is honoured.
* A display name NEVER grants owner or any other capability.
* When the real identity service cannot be built, the resolver fails closed:
  it returns an ``UNKNOWN`` principal with no capabilities instead of falling
  back to nickname / owner-id matching.
* The service is cached and read-only.  ``install_authority_service`` exists so
  tests and Web/bootstrap code can inject a repository-backed service without
  mutating global module state through private attributes.
"""

from __future__ import annotations

import threading
from dataclasses import replace as dataclass_replace
from datetime import datetime, timezone
from typing import Any, Mapping

from core import features
from core.terminal.identity_service import IdentityService
from core.terminal.principal import (
    Capability,
    CapabilityDenied,
    ChatType,
    PrincipalContext,
    RelationLevel,
)
from safelog import warn

_lock = threading.RLock()
_service: "AuthorityService | None" = None
_error: str = ""
_service_link_revision: int = -1
class AuthorityService:
    """Thin, cache-friendly wrapper around :class:`IdentityService`."""

    def __init__(self, identity_service: IdentityService):
        self.identity_service = identity_service

    @property
    def link_revision(self) -> int:
        return int(getattr(self.identity_service.link_graph, "revision", 0))

    def resolve_principal(
        self,
        *,
        platform: str,
        account_id: str,
        chat_id: str,
        chat_type: str = "private",
        sender_id: str = "",
        sender_name: str = "",
        occurred_at: datetime | None = None,
        group_id: str = "",
    ) -> PrincipalContext:
        return self.identity_service.resolve_principal(
            platform=platform,
            bot_account_id=account_id,
            chat_id=chat_id,
            chat_type=chat_type,
            sender_id=sender_id,
            sender_name=sender_name,
            occurred_at=occurred_at,
            group_id=group_id,
        )

    def can_query_group_commands(self, principal: PrincipalContext) -> bool:
        if principal.is_owner:
            return True
        return principal.has(Capability.QUERY_GROUP_COMMANDS) and (
            self.identity_service.group_query_policy(
                principal.platform,
                principal.bot_account_id,
                principal.group_id,
            )
        )


def install_authority_service(service: AuthorityService | None) -> None:
    """Inject a service (tests / web startup).  Passing ``None`` resets it."""
    global _service, _error, _service_link_revision
    with _lock:
        _service = service
        _error = ""
        _service_link_revision = -1


def reset_authority_service() -> None:
    global _service, _error, _service_link_revision
    with _lock:
        _service = None
        _error = ""
        _service_link_revision = -1


def authority_error() -> str:
    with _lock:
        return _error


def get_authority_service(*, force: bool = False) -> AuthorityService | None:
    """Return the cached service, building it from bootstrap when needed.

    Authority is always active.  Returns ``None`` only when the backing
    identity service cannot be built (missing DB/config); the error is retained
    for diagnostics and callers fail closed through :func:`unknown_principal`.
    """
    global _service, _error, _service_link_revision
    with _lock:
        if _service is not None:
            # Cheap live-link invalidation: a DB revision change hot-swaps the
            # graph so an unlink hides cross-account views on the next turn.
            try:
                from core.terminal import link_runtime

                revision = link_runtime.current_link_revision()
                if revision >= 0 and revision != _service_link_revision:
                    if link_runtime.refresh_identity_service_links(
                        _service.identity_service
                    ):
                        _service_link_revision = revision
            except Exception:
                warn("core/terminal/authority.py:130 Exception", err=repr(__import__("sys").exc_info()[1]))
            return _service
        if _error:
            return None
        try:
            from core.terminal.adapters import build_identity_service_from_bootstrap

            _service = AuthorityService(build_identity_service_from_bootstrap())
            _error = ""
            try:
                from core.terminal import link_runtime

                _service_link_revision = link_runtime.current_link_revision()
            except Exception:
                warn("core/terminal/authority.py:144 Exception", err=repr(__import__("sys").exc_info()[1]))
                _service_link_revision = -1
        except Exception as exc:  # noqa: BLE001 - fail closed, never crash main
            warn("core/terminal/authority.py:146 Exception", err=repr(exc))
            _error = str(exc)[:240]
            return None
        return _service


def unknown_principal(
    *,
    platform: str = "",
    account_id: str = "",
    chat_id: str = "",
    chat_type: str = "private",
    sender_id: str = "",
    sender_name: str = "",
    occurred_at: datetime | None = None,
    group_id: str = "",
) -> PrincipalContext:
    """Deterministic fail-closed principal with zero capabilities."""
    normalized_chat = (
        ChatType.GROUP
        if str(chat_type or "").lower() == "group"
        else ChatType.PRIVATE
    )
    effective_group = group_id or (chat_id if normalized_chat == ChatType.GROUP else "")
    person_id = str(sender_id or "").strip() or (
        str(chat_id or "").strip() if normalized_chat == ChatType.PRIVATE else ""
    )
    return PrincipalContext(
        platform=str(platform or ""),
        bot_account_id=str(account_id or ""),
        chat_id=str(chat_id or ""),
        chat_type=normalized_chat,
        person_id=person_id,
        display_name=str(sender_name or sender_id or chat_id or ""),
        relation_to_bot=RelationLevel.UNKNOWN,
        intimacy=0.0,
        group_id=str(effective_group or ""),
        capabilities=frozenset(),
        memory_scopes=frozenset(),
        occurred_at=occurred_at or datetime.now(),
    )


def active_principal(
    *,
    platform: str = "",
    account_id: str = "",
    chat_id: str = "",
    chat_type: str = "private",
    sender_id: str = "",
    sender_name: str = "",
    occurred_at: datetime | None = None,
    group_id: str = "",
    service: AuthorityService | None = None,
) -> PrincipalContext:
    """Resolve a principal; fail closed to an UNKNOWN principal.

    Authority is permanently active, so callers always receive a principal
    (never ``None``) and can enforce capabilities without a legacy fallback.
    """
    coords = dict(
        platform=str(platform or ""),
        account_id=str(account_id or ""),
        chat_id=str(chat_id or ""),
        chat_type=str(chat_type or "private"),
        sender_id=str(sender_id or ""),
        sender_name=str(sender_name or ""),
        occurred_at=occurred_at,
        group_id=str(group_id or ""),
    )
    explicit_service = service
    if explicit_service is None:
        explicit_service = get_authority_service()
    if explicit_service is None:
        return unknown_principal(**coords)
    try:
        return explicit_service.resolve_principal(**coords)
    except Exception:  # noqa: BLE001 - fail closed
        warn("core/terminal/authority.py:223 Exception", err=repr(__import__("sys").exc_info()[1]))
        return unknown_principal(**coords)


def active_is_owner(**kwargs: Any) -> bool:
    service = kwargs.pop("service", None)
    return active_principal(service=service, **kwargs).is_owner


# --------------------------------------------------------------------------
# Context adapters
# --------------------------------------------------------------------------
def context_extra(context: Any) -> dict:
    value = getattr(context, "extra", None)
    return value if isinstance(value, dict) else {}


def context_principal(context: Any) -> PrincipalContext | None:
    if context is None:
        return None
    direct = getattr(context, "principal", None)
    if isinstance(direct, PrincipalContext):
        return direct
    extra = context_extra(context)
    candidate = extra.get("principal_context")
    if isinstance(candidate, PrincipalContext):
        return candidate
    return None


def attach_principal(context: Any, principal: PrincipalContext | None) -> None:
    if context is None or principal is None:
        return
    try:
        context.principal = principal
    except Exception:
        warn("core/terminal/authority.py:258 Exception", err=repr(__import__("sys").exc_info()[1]))
    extra = getattr(context, "extra", None)
    if isinstance(extra, dict):
        # Scalar copies keep diagnostics/audit logs useful without forcing every
        # downstream serializer to understand PrincipalContext.
        extra.setdefault("person_id", principal.person_id)
        extra.setdefault("relation_to_bot", principal.relation_to_bot.value)
        extra.setdefault("link_component_id", principal.link_component_id)


def context_coordinates(context: Any) -> dict[str, Any]:
    """Best-effort extraction of identity coordinates from an AgentContext."""
    extra = context_extra(context)
    account_ctx = getattr(context, "account_context", None)
    platform = str(
        extra.get("platform")
        or getattr(account_ctx, "platform", "")
        or ""
    )
    account_id = str(
        extra.get("account_id")
        or extra.get("reply_account")
        or getattr(account_ctx, "account_id", "")
        or ""
    )
    chat_id = str(
        extra.get("chat_id")
        or extra.get("source_chat")
        or getattr(context, "chat_id", "")
        or ""
    )
    raw_chat_type = str(extra.get("chat_type") or "").lower()
    chat_type = "group" if (extra.get("is_group") or raw_chat_type == "group") else "private"
    sender_id = str(extra.get("sender_id") or extra.get("user_id") or "")
    sender_name = str(extra.get("sender_name") or extra.get("user_name") or "")
    group_id = str(extra.get("group_id") or (chat_id if chat_type == "group" else ""))
    occurred_at = extra.get("message_time") or extra.get("occurred_at")
    if not isinstance(occurred_at, datetime):
        occurred_at = None
    if not platform:
        event = getattr(context, "event", None)
        platform = str(getattr(event, "platform", "") or "")
    return {
        "platform": platform,
        "account_id": account_id,
        "chat_id": chat_id,
        "chat_type": chat_type,
        "sender_id": sender_id,
        "sender_name": sender_name,
        "occurred_at": occurred_at,
        "group_id": group_id,
    }


def _coordinates_differ(principal: PrincipalContext, overrides: Mapping[str, Any]) -> bool:
    mapping = {
        "platform": principal.platform,
        "account_id": principal.bot_account_id,
        "chat_id": principal.chat_id,
        "chat_type": principal.chat_type.value,
        "sender_id": principal.person_id,
        "group_id": principal.group_id,
    }
    for key, expected in overrides.items():
        if expected in (None, "") or key not in mapping:
            continue
        if str(mapping[key] or "") != str(expected):
            return True
    return False


def active_context_principal(
    context: Any,
    *,
    refresh: bool = False,
    service: AuthorityService | None = None,
    **overrides: Any,
) -> PrincipalContext:
    """Resolve/attach the principal for an AgentContext-like object.

    An attached principal is always honoured so callers that build the context
    themselves (Web, tests) can stay deterministic.  Otherwise coordinates are
    resolved through the deterministic service and the result is attached.
    Failures resolve to an attached UNKNOWN principal instead of returning
    ``None`` so every live turn has one authority answer.
    """
    attached = context_principal(context)
    if attached is not None and not refresh and not _coordinates_differ(
        attached, overrides
    ):
        return attached
    provided = {k: v for k, v in overrides.items() if v not in (None, "")}
    coords = context_coordinates(context)
    coords.update(provided)
    principal = active_principal(service=service, **coords)
    attach_principal(context, principal)
    return principal


def active_context_is_owner(
    context: Any,
    *,
    service: AuthorityService | None = None,
    refresh: bool = False,
    **overrides: Any,
) -> bool:
    principal = active_context_principal(
        context, service=service, refresh=refresh, **overrides
    )
    return principal.is_owner


def active_has_capability(
    capability: Capability,
    *,
    service: AuthorityService | None = None,
    **coords: Any,
) -> bool:
    return active_principal(service=service, **coords).has(capability)


def active_context_has_capability(
    context: Any,
    capability: Capability,
    *,
    service: AuthorityService | None = None,
    refresh: bool = False,
    **overrides: Any,
) -> bool:
    principal = active_context_principal(
        context, service=service, refresh=refresh, **overrides
    )
    return principal.has(capability)


def require_context_capability(
    context: Any,
    capability: Capability,
    *,
    service: AuthorityService | None = None,
    **overrides: Any,
) -> PrincipalContext:
    """Raise :class:`CapabilityDenied` when the deterministic principal denies."""
    principal = active_context_principal(context, service=service, **overrides)
    if not principal.has(capability):
        raise CapabilityDenied(capability, principal)
    return principal


def relation_label(principal: PrincipalContext | None, *, fallback: str = "") -> str:
    """Map a principal's relation to the legacy Chinese prompt labels."""
    if principal is None:
        return fallback
    if principal.is_group:
        return "群聊"
    if principal.relation_to_bot == RelationLevel.OWNER:
        return "亲密"
    if principal.relation_to_bot == RelationLevel.INTIMATE:
        return "亲密"
    if principal.relation_to_bot == RelationLevel.FAMILIAR:
        return "熟悉"
    if principal.relation_to_bot == RelationLevel.ORDINARY:
        return "普通"
    return fallback or "普通"


def with_principal_metadata(
    principal: PrincipalContext,
    *,
    metadata: Mapping[str, str] | None = None,
) -> PrincipalContext:
    """Small helper for callers that need a derived principal (tests/audit)."""
    merged = dict(principal.metadata)
    merged.update({str(k): str(v) for k, v in (metadata or {}).items()})
    return dataclass_replace(principal, metadata=tuple(sorted(merged.items())))
