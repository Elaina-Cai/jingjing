"""Unified automatic memory-read adapter for Phase 5 Track C.

Automatic long-term recall must go through ``MemoryGateway`` so every read is
filtered by the deterministic ``PrincipalContext``.  Explicit ``remember`` is a
write, not a recall, and intentionally stays on the legacy memory store.

The router never falls back to the old vector store when a principal is
missing; a missing identity simply returns no recall, so it can never silently
broaden memory scope.
"""

from __future__ import annotations

from typing import Any

from core.terminal.memory_gateway import (
    MemoryGateway,
    MemoryHit,
    get_memory_gateway,
)
from safelog import warn


def recall_for_context(
    context: Any,
    query: str = "",
    *,
    limit: int = 5,
    gateway: MemoryGateway | None = None,
) -> list[MemoryHit]:
    """Return explainable memory hits for one agent/tool context.

    ``gateway`` is injectable for tests and for callers that already hold a
    configured instance.  When it is omitted, the feature-flagged runtime
    gateway is used; an unavailable gateway or principal yields ``[]``.
    """
    if context is None:
        return []
    active_gateway = gateway
    if active_gateway is None:
        active_gateway = get_memory_gateway()
    if active_gateway is None:
        return []
    principal = _principal_for_context(context)
    if principal is None:
        return []
    try:
        return list(
            active_gateway.recall(
                principal,
                query=str(query or ""),
                limit=max(1, int(limit or 1)),
            )
        )
    except Exception:
        warn("core/memory/recall_router.py:54 Exception", err=repr(__import__("sys").exc_info()[1]))
        return []


def recall_items_for_context(
    context: Any,
    query: str = "",
    *,
    kinds: list[str] | tuple[str, ...] | None = None,
    limit: int = 8,
    hub: Any = None,
) -> list[Any]:
    """Hub-based recall for contexts that need multiple memory kinds."""
    if context is None:
        return []
    principal = _principal_for_context(context)
    if principal is None:
        return []
    # 2026-11-01：这里的「显式回忆预热 embedding」随 2026-10-25 去向量一起下线。
    # 需要语义召回时再上小模型，只走显式回忆，不进每拍注入。
    from core.memory.hub import get_memory_hub

    active_hub = hub or get_memory_hub()
    try:
        return list(
            active_hub.recall(
                principal,
                str(query or ""),
                kinds=kinds,
                limit=max(1, int(limit or 1)),
                context=context,
            )
        )
    except Exception:
        warn("core/memory/recall_router.py:96 Exception", err=repr(__import__("sys").exc_info()[1]))
        return []


def _hub_item_label(hits: Any) -> str:
    """Label MemoryItem hits (hub path) so they never read as live speech.

    ⑨→③ 回调拿到的是 ``MemoryItem``：它没有网关的 ``event_type``，``occurred_at``
    也可能为空。照旧路径会被渲染成“[某人说过] [今天的钟] …”
    ——读起来像群里刚说过。web 梗库与本群梗分开标注：前者是词典条目，
    后者是本群真出现过的说法。
    """
    try:
        items = list(hits)
    except TypeError:
        warn("core/memory/recall_router.py:110 TypeError", err=repr(__import__("sys").exc_info()[1]))
        return ""
    kinds = {type(hit).__name__ for hit in items}
    if kinds and kinds <= {"MemoryItem"}:
        # MemoryItem already has kind/source metadata.  Let the gateway label
        # each row independently so evidence_state is never hidden by a batch
        # label such as "[事实版本]".
        return ""
    return ""


def render_hits(
    hits: list[MemoryHit] | tuple[MemoryHit, ...],
    *,
    header: str = "[事件记忆·仅作当前对话参考]",
    max_chars: int = 1200,
    source_label: str = "",
) -> str:
    """Render gateway hits for prompt/tool output.

    ⑨→③ 按需记忆走的是 hub，拿到的是 ``MemoryItem``：它没有网关的
    ``event_type``，``occurred_at`` 也可能为空，照旧路径会被渲染成
    “[某人说过] [今天的钟] …”——读起来像群里刚说过。故这里给它们一个不会冒充
    现场发言的标签，并不传时间。
    """
    label = source_label or _hub_item_label(hits)
    try:
        return MemoryGateway.render(
            hits, header=header, max_chars=max_chars, source_label=label
        )
    except Exception:
        warn("core/memory/recall_router.py:137 Exception", err=repr(__import__("sys").exc_info()[1]))
        return ""


def _principal_for_context(context: Any):
    """Resolve the deterministic principal from any context-like object."""
    try:
        from core.memory.index_scope import context_contradicts
        from core.terminal.authority import active_context_principal

        principal = active_context_principal(context)
        # 坐标与已附身份矛盾时按「没有身份」处理：读不到，而不是用旧身份读。
        if context_contradicts(context, principal):
            return None
        return principal
    except Exception:
        warn("core/memory/recall_router.py:147 Exception", err=repr(__import__("sys").exc_info()[1]))
        return None


__all__ = ["recall_for_context", "render_hits"]
