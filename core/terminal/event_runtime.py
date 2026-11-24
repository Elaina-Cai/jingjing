"""Runtime accessors for the Phase 2 terminal event stream.

The store is built lazily from the bootstrap meta database and cached.  All
callers treat it as best-effort: if the DB is unavailable, recording must never
break the main chain.
"""

from __future__ import annotations

import threading
from typing import Any

from core import features
from safelog import warn

_lock = threading.RLock()
_store: Any = None
_error: str = ""
def install_terminal_event_store(store: Any | None) -> None:
    global _store, _error
    with _lock:
        _store = store
        _error = ""


def reset_terminal_event_store() -> None:
    global _store, _error
    with _lock:
        _store = None
        _error = ""


def event_store_error() -> str:
    with _lock:
        return _error


def get_terminal_event_store(*, force: bool = False) -> Any | None:
    """Return the cached SQLite event store, or ``None`` when unavailable."""
    global _store, _error
    with _lock:
        if _store is not None:
            return _store
        if _error:
            return None
        try:
            from core.terminal.store_port import get_meta_repo
            from core.terminal.event_store_db import TerminalEventStore

            _store = TerminalEventStore(get_meta_repo().engine)
            _error = ""
        except Exception as exc:  # noqa: BLE001 - best-effort recording
            warn("core/terminal/event_runtime.py:51 Exception", err=repr(exc))
            _error = str(exc)[:240]
            return None
        return _store


def default_event_store() -> Any | None:
    """Return the formal SQLite event store.

    The Phase 0 JSONL fallback was removed after the single-account gray run:
    production recording now has one source of truth.
    """
    return get_terminal_event_store(force=True)


def get_event_identity_service() -> Any | None:
    """Read-only identity service used to stamp principal metadata on events.

    Reuses the cached shadow runtime service so one DB read serves both the
    event stream and any Phase 0 compatibility hooks.
    """
    try:
        from core.terminal.shadow_runtime import get_shadow_identity_service

        return get_shadow_identity_service()
    except Exception:
        warn("core/terminal/event_runtime.py:76 Exception", err=repr(__import__("sys").exc_info()[1]))
        return None
