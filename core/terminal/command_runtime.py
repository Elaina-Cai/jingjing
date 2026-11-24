"""Runtime accessor for the Phase 2 DB-backed CommandService."""

from __future__ import annotations

import threading
from typing import Any

from core.terminal.command_service import CommandService
from safelog import warn

_lock = threading.RLock()
_service: CommandService | None = None
_error: str = ""


def install_terminal_command_service(service: CommandService | None) -> None:
    global _service, _error
    with _lock:
        _service = service
        _error = ""


def reset_terminal_command_service() -> None:
    global _service, _error
    with _lock:
        _service = None
        _error = ""


def command_service_error() -> str:
    with _lock:
        return _error


def get_terminal_command_service(*, force: bool = False) -> CommandService | None:
    """Build the DB-backed command service from bootstrap and cache it."""
    global _service, _error
    with _lock:
        if _service is not None:
            return _service
        if _error:
            return None
        try:
            from core.terminal.store_port import get_meta_repo
            from core.terminal.event_runtime import get_event_identity_service
            from db.repo_commands import CommandRepository

            repository = CommandRepository(get_meta_repo().engine)
            _service = CommandService(
                get_event_identity_service(),
                repository=repository,
            )
            _error = ""
        except Exception as exc:  # noqa: BLE001 - command recording is best effort
            warn("core/terminal/command_runtime.py:53 Exception", err=repr(exc))
            _error = str(exc)[:240]
            return None
        return _service


def get_terminal_command_recorder(identity_service: Any = None):
    """Recorder suitable for ``record_message_events(command_recorder=...)``."""
    service = get_terminal_command_service()
    if service is None:
        return None
    from core.terminal.command_service import command_recorder_for_service

    return command_recorder_for_service(
        service,
        identity_service=identity_service,
        path=None,
    )
