"""Live account-link graph and revision-aware cache invalidation.

The DB is the source of truth for owner-drawn links (``account_link_edges``
plus ``account_link_state.revision``).  Config-file manual tie-break sources
are still merged while the Web UI is pending.

Every cache that holds an ``IdentityService`` (authority, shadow identity,
memory gateway) compares the cheap DB revision before serving a turn.  When
the owner adds or removes a link, the next resolution rebuilds the component
map; an unlink therefore hides the cross-account view on the next turn.
"""

from __future__ import annotations

import threading
from typing import Any

from core.terminal.link_graph import AccountLinkGraph
from safelog import warn

_lock = threading.RLock()
_repository: Any = None
_error: str = ""


def install_link_repository(repository: Any | None) -> None:
    global _repository, _error
    with _lock:
        _repository = repository
        _error = ""


def reset_link_repository() -> None:
    global _repository, _error
    with _lock:
        _repository = None
        _error = ""


def link_repository_error() -> str:
    with _lock:
        return _error


def get_link_repository(*, force: bool = False) -> Any | None:
    global _repository, _error
    with _lock:
        if _repository is not None:
            return _repository
        if _error:
            return None
        try:
            from core.terminal.store_port import get_meta_repo
            from db.repo_links import LinkGraphRepository

            _repository = LinkGraphRepository(get_meta_repo().engine)
            _error = ""
        except Exception as exc:  # noqa: BLE001 - links are best-effort live state
            warn("core/terminal/link_runtime.py:57 Exception", err=repr(exc))
            _error = str(exc)[:240]
            return None
        return _repository


def current_link_revision() -> int:
    repository = get_link_repository()
    if repository is None:
        return -1
    try:
        return int(repository.current_revision())
    except Exception:
        warn("core/terminal/link_runtime.py:69 Exception", err=repr(__import__("sys").exc_info()[1]))
        return -1


def load_live_link_graph() -> AccountLinkGraph:
    """Build the effective graph: config manual sources + DB active edges."""
    graph: AccountLinkGraph
    try:
        from core.terminal.adapters import load_account_link_edges, load_link_graph

        graph = load_link_graph()
        try:
            from core.terminal.store_port import get_meta_repo

            graph = load_account_link_edges(get_meta_repo(), graph)
        except Exception:
            warn("core/terminal/link_runtime.py:84 Exception", err=repr(__import__("sys").exc_info()[1]))
    except Exception:
        warn("core/terminal/link_runtime.py:86 Exception", err=repr(__import__("sys").exc_info()[1]))
        graph = AccountLinkGraph()
    return graph


def refresh_identity_service_links(service: Any) -> bool:
    """Hot-swap the graph on a cached IdentityService; True when refreshed."""
    if service is None or not hasattr(service, "replace_link_graph"):
        return False
    try:
        service.replace_link_graph(load_live_link_graph())
        return True
    except Exception:
        warn("core/terminal/link_runtime.py:98 Exception", err=repr(__import__("sys").exc_info()[1]))
        return False


def refresh_authority_links_if_stale(force: bool = False) -> bool:
    """Refresh the Authority's cached IdentityService when revision changed."""
    from core.terminal import authority

    service = authority.get_authority_service()
    if service is None:
        return False
    revision = current_link_revision()
    if revision < 0:
        return False
    cached = getattr(authority, "_service_link_revision", -1)
    if not force and revision == cached:
        return False
    refreshed = refresh_identity_service_links(service.identity_service)
    if refreshed:
        with authority._lock:
            authority._service_link_revision = revision
    return refreshed


def invalidate_link_caches() -> None:
    """Drop local in-process caches after a link mutation in this process."""
    try:
        from core.terminal import authority

        authority.reset_authority_service()
    except Exception:
        warn("core/terminal/link_runtime.py:128 Exception", err=repr(__import__("sys").exc_info()[1]))
    try:
        from core.terminal.shadow_runtime import reset_shadow_identity_service

        reset_shadow_identity_service()
    except Exception:
        warn("core/terminal/link_runtime.py:134 Exception", err=repr(__import__("sys").exc_info()[1]))
    try:
        from core.terminal.memory_gateway import reset_memory_gateway

        reset_memory_gateway()
    except Exception:
        warn("core/terminal/link_runtime.py:140 Exception", err=repr(__import__("sys").exc_info()[1]))


def add_link(
    left_platform: str,
    left_account: str,
    right_platform: str,
    right_account: str,
    *,
    created_by: str = "",
    note: str = "",
) -> int:
    from core.terminal.principal import AccountRef

    repository = get_link_repository()
    if repository is None:
        raise RuntimeError("link repository unavailable")
    revision = repository.add_link(
        AccountRef(str(left_platform), str(left_account)),
        AccountRef(str(right_platform), str(right_account)),
        created_by=created_by,
        note=note,
    )
    invalidate_link_caches()
    return revision


def remove_link(
    left_platform: str,
    left_account: str,
    right_platform: str,
    right_account: str,
) -> int:
    from core.terminal.principal import AccountRef

    repository = get_link_repository()
    if repository is None:
        raise RuntimeError("link repository unavailable")
    revision = repository.remove_link(
        AccountRef(str(left_platform), str(left_account)),
        AccountRef(str(right_platform), str(right_account)),
    )
    invalidate_link_caches()
    return revision
