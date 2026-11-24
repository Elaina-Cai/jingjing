"""Lazily-built shadow identity service for main-chain shadow recording.

Building the service touches the real repositories, so it is only imported and
called when the Phase 0 shadow flag is enabled.  The service is cached and
read-only.
"""

from __future__ import annotations

import threading
from datetime import datetime

from core.terminal.adapters import build_identity_service_from_bootstrap
from core.terminal.identity_service import IdentityService
from core.terminal.principal import PrincipalContext
from safelog import warn

_lock = threading.RLock()
_service: IdentityService | None = None
_error: str = ""
_link_revision: int = -1


def get_shadow_identity_service() -> IdentityService | None:
    global _service, _error, _link_revision
    with _lock:
        if _service is not None:
            try:
                from core.terminal import link_runtime

                revision = link_runtime.current_link_revision()
                if revision >= 0 and revision != _link_revision:
                    if link_runtime.refresh_identity_service_links(_service):
                        _link_revision = revision
            except Exception:
                warn("core/terminal/shadow_runtime.py:34 Exception", err=repr(__import__("sys").exc_info()[1]))
            return _service
        if _error:
            return None
        try:
            _service = build_identity_service_from_bootstrap()
            _error = ""
            try:
                from core.terminal import link_runtime

                _link_revision = link_runtime.current_link_revision()
            except Exception:
                warn("core/terminal/shadow_runtime.py:46 Exception", err=repr(__import__("sys").exc_info()[1]))
                _link_revision = -1
        except Exception as exc:  # noqa: BLE001 - shadow must never break main
            warn("core/terminal/shadow_runtime.py:48 Exception", err=repr(exc))
            _error = str(exc)[:240]
            return None
        return _service


def reset_shadow_identity_service() -> None:
    global _service, _error, _link_revision
    with _lock:
        _service = None
        _error = ""
        _link_revision = -1


def shadow_identity_error() -> str:
    with _lock:
        return _error


def resolve_shadow_principal(
    *,
    platform: str,
    account_id: str,
    chat_id: str,
    chat_type: str = "private",
    sender_id: str = "",
    sender_name: str = "",
    occurred_at: datetime | None = None,
    group_id: str = "",
) -> PrincipalContext | None:
    """Best-effort principal resolution for shadow event hooks."""
    service = get_shadow_identity_service()
    if service is None:
        return None
    try:
        return service.resolve_principal(
            platform=platform,
            bot_account_id=account_id,
            chat_id=chat_id,
            chat_type=chat_type,
            sender_id=sender_id,
            sender_name=sender_name,
            occurred_at=occurred_at,
            group_id=group_id,
        )
    except Exception:
        warn("core/terminal/shadow_runtime.py:93 Exception", err=repr(__import__("sys").exc_info()[1]))
        return None
