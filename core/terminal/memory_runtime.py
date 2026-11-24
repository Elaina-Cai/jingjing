"""Runtime accessors for the Phase 2 memory projection worker."""

from __future__ import annotations

import threading
from typing import Any

from core.terminal.memory_projection import MemoryProjector
from safelog import warn

_lock = threading.RLock()
_projector: MemoryProjector | None = None
_error: str = ""


def install_memory_projector(projector: MemoryProjector | None) -> None:
    global _projector, _error
    with _lock:
        _projector = projector
        _error = ""


def reset_memory_projector() -> None:
    global _projector, _error
    with _lock:
        _projector = None
        _error = ""


def memory_projector_error() -> str:
    with _lock:
        return _error


def get_memory_projector(*, force: bool = False) -> MemoryProjector | None:
    global _projector, _error
    with _lock:
        if _projector is not None:
            return _projector
        if _error:
            return None
        try:
            from core.terminal.store_port import get_meta_repo
            from core.terminal.event_runtime import get_terminal_event_store
            from db.repo_memory_projections import MemoryProjectionRepository

            engine = get_meta_repo().engine
            event_store = get_terminal_event_store(force=True)
            if event_store is None:
                _error = "terminal event store unavailable"
                return None
            _projector = MemoryProjector(
                event_store,
                MemoryProjectionRepository(engine),
            )
            _error = ""
        except Exception as exc:  # noqa: BLE001 - projection is background work
            warn("core/terminal/memory_runtime.py:56 Exception", err=repr(exc))
            _error = str(exc)[:240]
            return None
        return _projector


def project_pending_memory(*, limit: int = 200) -> dict[str, Any]:
    """Project a bounded batch of unprocessed events; best-effort."""
    projector = get_memory_projector()
    if projector is None:
        return {}
    try:
        return projector.project_pending(limit=limit)
    except Exception as exc:  # noqa: BLE001
        warn("core/terminal/memory_runtime.py:69 Exception", err=repr(exc))
        return {"error": str(exc)[:200], "scanned": 0, "projected": 0}
