"""Identity service for the refactor.

The repository (``db.repo_people``) owns storage.  Callers that only need a
stable person reference should depend on this thin service and the
``PersonRef`` contract instead of the repository's SQLAlchemy models.
"""

from __future__ import annotations

from core.contracts import PersonRef
from core.identity.backfill import BackfillReport, ConversationIdentityBackfiller
from core.identity.platforms import (
    PlatformIdentityAdapter,
    adapter_for,
    chat_type_for,
    identity_type_for,
    is_group_chat,
    register_platform_adapter,
)

__all__ = [
    "BackfillReport",
    "ConversationIdentityBackfiller",
    "PersonRef",
    "PlatformIdentityAdapter",
    "adapter_for",
    "chat_type_for",
    "identity_type_for",
    "is_group_chat",
    "register_platform_adapter",
]
