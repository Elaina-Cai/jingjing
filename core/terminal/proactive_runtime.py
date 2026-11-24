"""Cache invalidation for the owner-managed proactive whitelist.

The database is the source of truth.  The outbound gateway reads it on every
proactive send.  IdentityService still derives ``PROACTIVE_MESSAGE`` from the
same rows for prompts/permission introspection, so in-process Web mutations
must drop the cached Authority/Shadow identity services.
"""

from __future__ import annotations
from safelog import warn


def invalidate_proactive_caches() -> None:
    """Drop identity caches that embed the proactive target set."""
    try:
        from core.terminal.authority import reset_authority_service

        reset_authority_service()
    except Exception:
        warn("core/terminal/proactive_runtime.py:18 Exception", err=repr(__import__("sys").exc_info()[1]))
    try:
        from core.terminal.shadow_runtime import reset_shadow_identity_service

        reset_shadow_identity_service()
    except Exception:
        warn("core/terminal/proactive_runtime.py:24 Exception", err=repr(__import__("sys").exc_info()[1]))
