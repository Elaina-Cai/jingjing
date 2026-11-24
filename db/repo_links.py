"""DB-managed account-link edges with a monotonic revision.

The owner draws links in the Web layer (or via the Phase 2 CLI); every add or
revoke is an append-only update plus a bump of ``account_link_state``.
Identity/authority/recall caches compare the cheap revision number and reload
only when it actually changed.

Removing a link never deletes the edge row: it sets ``revoked_at`` so audit and
re-link history stay intact.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Sequence

from sqlalchemy import or_, select

from core.terminal.link_graph import AccountLinkGraph
from core.terminal.principal import AccountRef
from db.meta_models import AccountLinkEdge, AccountLinkState
from db.repo_base import BaseRepository

LINK_STATE_KEY = "terminal_links"


def _utcnow() -> datetime:
    return datetime.now()


def _edge_filter(left: AccountRef, right: AccountRef):
    forward = (
        (AccountLinkEdge.platform_a == left.platform)
        & (AccountLinkEdge.account_a == left.account_id)
        & (AccountLinkEdge.platform_b == right.platform)
        & (AccountLinkEdge.account_b == right.account_id)
    )
    reverse = (
        (AccountLinkEdge.platform_a == right.platform)
        & (AccountLinkEdge.account_a == right.account_id)
        & (AccountLinkEdge.platform_b == left.platform)
        & (AccountLinkEdge.account_b == left.account_id)
    )
    return or_(forward, reverse)


class LinkGraphRepository(BaseRepository):
    """Append-only mutations + revision for ``account_link_edges``."""

    def _state(self, session) -> AccountLinkState:
        state = session.get(AccountLinkState, LINK_STATE_KEY)
        if state is None:
            state = AccountLinkState(
                key=LINK_STATE_KEY, revision=0, updated_at=_utcnow()
            )
            session.add(state)
            session.flush()
        return state

    def current_revision(self) -> int:
        with self._session() as session:
            state = self._state(session)
            session.commit()
            return int(state.revision or 0)

    def list_active_edges(self) -> list[AccountLinkEdge]:
        with self._session() as session:
            rows = list(
                session.execute(
                    select(AccountLinkEdge)
                    .where(AccountLinkEdge.revoked_at.is_(None))
                    .order_by(AccountLinkEdge.id.asc())
                ).scalars().all()
            )
        return rows

    def list_all_edges(self, limit: int = 500) -> list[AccountLinkEdge]:
        with self._session() as session:
            rows = list(
                session.execute(
                    select(AccountLinkEdge)
                    .order_by(AccountLinkEdge.id.desc())
                    .limit(max(1, int(limit)))
                ).scalars().all()
            )
        return rows

    def load_graph(
        self,
        base_graph: AccountLinkGraph | None = None,
    ) -> AccountLinkGraph:
        graph = base_graph if base_graph is not None else AccountLinkGraph()
        for row in self.list_active_edges():
            left = AccountRef(
                str(row.platform_a or ""), str(row.account_a or "")
            )
            right = AccountRef(
                str(row.platform_b or ""), str(row.account_b or "")
            )
            if (
                left.platform
                and left.account_id
                and right.platform
                and right.account_id
                and left != right
            ):
                graph.add_link(left, right)
        return graph

    def add_link(
        self,
        left: AccountRef,
        right: AccountRef,
        *,
        created_by: str = "",
        note: str = "",
    ) -> int:
        if left == right:
            raise ValueError("cannot link an account to itself")
        with self._session() as session:
            state = self._state(session)
            existing = session.execute(
                select(AccountLinkEdge.id)
                .where(AccountLinkEdge.revoked_at.is_(None))
                .where(_edge_filter(left, right))
                .limit(1)
            ).scalar()
            if existing is not None:
                revision = int(state.revision or 0)
                session.commit()
                return revision
            revision = int(state.revision or 0) + 1
            state.revision = revision
            state.updated_at = _utcnow()
            session.add(AccountLinkEdge(
                platform_a=str(left.platform),
                account_a=str(left.account_id),
                platform_b=str(right.platform),
                account_b=str(right.account_id),
                created_by=str(created_by or ""),
                created_at=_utcnow(),
                revision=revision,
                note=str(note or ""),
            ))
            session.commit()
            return revision

    def remove_link(
        self,
        left: AccountRef,
        right: AccountRef,
        *,
        at: datetime | None = None,
    ) -> int:
        with self._session() as session:
            state = self._state(session)
            row = session.execute(
                select(AccountLinkEdge)
                .where(AccountLinkEdge.revoked_at.is_(None))
                .where(_edge_filter(left, right))
                .order_by(AccountLinkEdge.id.desc())
                .limit(1)
            ).scalars().first()
            if row is None:
                revision = int(state.revision or 0)
                session.commit()
                return revision
            row.revoked_at = at or _utcnow()
            revision = int(state.revision or 0) + 1
            state.revision = revision
            state.updated_at = _utcnow()
            session.commit()
            return revision

    @staticmethod
    def graph_edges(graph: AccountLinkGraph) -> list[tuple[AccountRef, AccountRef]]:
        return list(graph.links())

    @staticmethod
    def active_signatures(rows: Iterable[AccountLinkEdge]) -> set[tuple[str, str, str, str]]:
        return {
            (
                str(row.platform_a),
                str(row.account_a),
                str(row.platform_b),
                str(row.account_b),
            )
            for row in rows
        }
