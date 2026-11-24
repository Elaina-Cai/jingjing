"""Resumable cursor storage for the evidence indexing worker (P2).

The evidence index itself is a projection; this table only remembers where the
last worker pass stopped.  Keeping it separate from ``message_evidence`` makes
it obvious that the cursor can be reset/rebuilt without touching indexed
evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from sqlalchemy import select

from db.meta_models import EvidenceIndexCursor
from db.repo_base import BaseRepository


@dataclass(frozen=True)
class EvidenceCursorState:
    platform: str
    account_id: str
    last_synced_at: datetime | None
    last_message_id: int
    indexed_total: int
    updated_at: datetime | None = None

    def to_dict(self) -> dict:
        return {
            "platform": self.platform,
            "account_id": self.account_id,
            "last_synced_at": self.last_synced_at.isoformat()
            if self.last_synced_at else "",
            "last_message_id": self.last_message_id,
            "indexed_total": self.indexed_total,
            "updated_at": self.updated_at.isoformat() if self.updated_at else "",
        }


def _state(row: EvidenceIndexCursor) -> EvidenceCursorState:
    return EvidenceCursorState(
        platform=row.platform or "",
        account_id=row.account_id or "",
        last_synced_at=row.last_synced_at,
        last_message_id=int(row.last_message_id or 0),
        indexed_total=int(row.indexed_total or 0),
        updated_at=row.updated_at,
    )


class EvidenceCursorRepository(BaseRepository):
    def get(self, platform: str, account_id: str) -> EvidenceCursorState | None:
        with self._session() as s:
            row = s.execute(
                select(EvidenceIndexCursor).where(
                    EvidenceIndexCursor.platform == platform,
                    EvidenceIndexCursor.account_id == account_id,
                ).limit(1)
            ).scalar_one_or_none()
            return _state(row) if row is not None else None

    def save(
        self,
        *,
        platform: str,
        account_id: str,
        last_synced_at: datetime | None,
        last_message_id: int,
        indexed_total: int = 0,
    ) -> EvidenceCursorState:
        now = datetime.now()
        with self._session() as s:
            row = s.execute(
                select(EvidenceIndexCursor).where(
                    EvidenceIndexCursor.platform == platform,
                    EvidenceIndexCursor.account_id == account_id,
                ).limit(1)
            ).scalar_one_or_none()
            if row is None:
                row = EvidenceIndexCursor(
                    platform=platform,
                    account_id=account_id,
                    last_synced_at=last_synced_at,
                    last_message_id=int(last_message_id or 0),
                    indexed_total=int(indexed_total or 0),
                    updated_at=now,
                )
                s.add(row)
            else:
                row.last_synced_at = last_synced_at
                row.last_message_id = int(last_message_id or 0)
                row.indexed_total = int(indexed_total or 0)
                row.updated_at = now
            s.commit()
            s.refresh(row)
            return _state(row)

    def reset(self, platform: str, account_id: str) -> None:
        """Forget the cursor so the next worker pass rescans from the start."""
        with self._session() as s:
            row = s.execute(
                select(EvidenceIndexCursor).where(
                    EvidenceIndexCursor.platform == platform,
                    EvidenceIndexCursor.account_id == account_id,
                ).limit(1)
            ).scalar_one_or_none()
            if row is not None:
                s.delete(row)
                s.commit()

    def list_all(self) -> Sequence[EvidenceCursorState]:
        with self._session() as s:
            rows = s.execute(
                select(EvidenceIndexCursor).order_by(
                    EvidenceIndexCursor.platform,
                    EvidenceIndexCursor.account_id,
                )
            ).scalars().all()
            return [_state(row) for row in rows]
