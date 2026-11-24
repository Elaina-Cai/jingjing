"""Deterministic account-link graph for Phase 0.

Links are explicit, undirected and transitive.  The graph only computes
visibility; it never merges underlying data.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Iterable, Mapping

from core.terminal.principal import (
    AccountRef,
    RelationshipState,
    pick_closer_relationship,
)
from safelog import warn


class AccountLinkGraph:
    """Undirected graph of linked accounts.

    A connected component means "these accounts are the same person for query
    visibility".  Removing a link immediately hides cross-account views.
    """

    def __init__(self, links: Iterable[tuple[AccountRef, AccountRef]] | None = None):
        self._edges: set[frozenset[AccountRef]] = set()
        self._revision = 0
        self._manual_sources: dict[tuple[str, str], str] = {}
        if links:
            for left, right in links:
                self.add_link(left, right)

    @property
    def revision(self) -> int:
        return self._revision

    @staticmethod
    def _edge(left: AccountRef, right: AccountRef) -> frozenset[AccountRef]:
        if left == right:
            raise ValueError("cannot link an account to itself")
        return frozenset((left, right))

    def add_link(self, left: AccountRef, right: AccountRef) -> int:
        edge = self._edge(left, right)
        if edge not in self._edges:
            self._edges.add(edge)
            self._revision += 1
        return self._revision

    def remove_link(self, left: AccountRef, right: AccountRef) -> int:
        edge = self._edge(left, right)
        if edge in self._edges:
            self._edges.discard(edge)
            self._revision += 1
        return self._revision

    def has_link(self, left: AccountRef, right: AccountRef) -> bool:
        try:
            return self._edge(left, right) in self._edges
        except ValueError:
            warn("core/terminal/link_graph.py:62 ValueError", err=repr(__import__("sys").exc_info()[1]))
            return left == right

    def links(self) -> list[tuple[AccountRef, AccountRef]]:
        out: list[tuple[AccountRef, AccountRef]] = []
        for edge in sorted(self._edges, key=lambda e: tuple(sorted(map(str, e)))):
            parts = sorted(edge, key=str)
            if len(parts) == 2:
                out.append((parts[0], parts[1]))
        return out

    def set_manual_source(self, component_id: str, person_id: str, account_id: str) -> None:
        self._manual_sources[(component_id, person_id)] = account_id

    def manual_source(self, component_id: str, person_id: str) -> str:
        return self._manual_sources.get((component_id, person_id), "")

    def _adjacency(self) -> dict[AccountRef, set[AccountRef]]:
        adj: dict[AccountRef, set[AccountRef]] = defaultdict(set)
        for edge in self._edges:
            parts = tuple(edge)
            if len(parts) != 2:
                continue
            left, right = parts
            adj[left].add(right)
            adj[right].add(left)
        return adj

    def component_of(self, account: AccountRef) -> frozenset[AccountRef]:
        adj = self._adjacency()
        seen: set[AccountRef] = set()
        stack = [account]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            stack.extend(adj.get(current, ()))
        return frozenset(seen)

    def connected_components(self) -> list[frozenset[AccountRef]]:
        seen: set[AccountRef] = set()
        out: list[frozenset[AccountRef]] = []
        adjacency = self._adjacency()
        nodes = set(adjacency)
        for edge in self._edges:
            nodes.update(edge)
        for node in sorted(nodes, key=str):
            if node in seen:
                continue
            component = self.component_of(node)
            seen.update(component)
            out.append(component)
        return out

    @staticmethod
    def component_id(component: frozenset[AccountRef] | Iterable[AccountRef]) -> str:
        return "|".join(sorted(str(account) for account in component))

    def component_id_of(self, account: AccountRef) -> str:
        return self.component_id(self.component_of(account))

    def connected(self, left: AccountRef, right: AccountRef) -> bool:
        if left == right:
            return True
        return right in self.component_of(left)

    def effective_relationship(
        self,
        bot_account: AccountRef,
        person_id: str,
        relationships: Mapping[tuple[str, str, str], RelationshipState],
    ) -> RelationshipState | None:
        """Pick the closest relationship inside the bot account's component."""
        states: list[RelationshipState] = []
        component = self.component_of(bot_account)
        for account in component:
            state = relationships.get((account.platform, account.account_id, person_id))
            if state is not None:
                states.append(state)
        if not states:
            return None
        component_id = self.component_id(component)
        manual = self.manual_source(component_id, person_id)
        return pick_closer_relationship(states, manual_source_account=manual)

    def component_account_keys(self, bot_account: AccountRef) -> frozenset[tuple[str, str]]:
        return frozenset((a.platform, a.account_id) for a in self.component_of(bot_account))


def utcnow() -> datetime:
    return datetime.now()
