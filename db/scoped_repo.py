from __future__ import annotations

from typing import Any
from safelog import warn

# Repository attributes that live in the per-account database.
_ACCOUNT_ATTRS = frozenset({
    "messages",
    "contacts",
    "analysis",
    "sync_state",
    "user_profile",
    "send_log",
    "action_log",
})

# Repository attributes that live in the shared meta database.
_META_ATTRS = frozenset({
    "account_registry",
    "personas",
    "conversations",
    "sessions",
    "core_memory",
    "reminders",
    "relationships",
    "topics",
    "threads",
    "thread_refs",
    "friction",
    "todos",
    "knowledge_docs",
})


class ScopedRepository:
    """A repository facade that routes attributes to account or meta storage.

    Historically the project had a single repository (the legacy database).
    The multi-platform/multi-account layout splits data in two:

    * per-account: messages, contacts, analysis, send_log, sync_state, ...
    * shared meta: personas, memories, sessions, topics, reminders, todos, ...

    Passing one object to existing agents/tools keeps their constructors
    unchanged while still giving each account its own messages and contacts.
    Explicit attribute maps are used instead of blind ``hasattr`` fallbacks so
    a future name collision does not silently pick the wrong database.
    """

    def __init__(self, account_repo: Any, meta_repo: Any,
                 platform: str = "", account_id: str = ""):
        object.__setattr__(self, "_account_repo", account_repo)
        object.__setattr__(self, "_meta_repo", meta_repo)
        object.__setattr__(self, "platform", platform)
        object.__setattr__(self, "account_id", account_id)

    @property
    def account(self) -> Any:
        return object.__getattribute__(self, "_account_repo")

    @property
    def meta(self) -> Any:
        return object.__getattribute__(self, "_meta_repo")

    @property
    def engine(self):
        return getattr(self.account, "engine", None)

    @property
    def meta_engine(self):
        return getattr(self.meta, "engine", None)

    def __getattr__(self, name: str) -> Any:
        if name in _ACCOUNT_ATTRS:
            return getattr(self.account, name)
        if name in _META_ATTRS:
            return getattr(self.meta, name)
        # Unknown/forward-compatible attributes: try account first, then meta.
        try:
            return getattr(self.account, name)
        except AttributeError:
            warn("db/scoped_repo.py:80 AttributeError", err=repr(__import__("sys").exc_info()[1]))
        try:
            return getattr(self.meta, name)
        except AttributeError:
            warn("db/scoped_repo.py:84 AttributeError", err=repr(__import__("sys").exc_info()[1]))
        raise AttributeError(
            f"{type(self).__name__!s} has no attribute {name!r} "
            f"(account={self.account_id!r}, platform={self.platform!r})"
        )

    def __repr__(self) -> str:
        return (
            f"<ScopedRepository platform={self.platform!r} "
            f"account_id={self.account_id!r}>"
        )


__all__ = ["ScopedRepository"]
