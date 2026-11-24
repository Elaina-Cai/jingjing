from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from db.models import Relationship, FrictionEvent
from db.repo_base import BaseRepository


class FrictionEventRepository(BaseRepository):
    def add_event(self, event_type: str, msg_id: int,
                  from_thread_id: int | None = None,
                  to_thread_id: int | None = None,
                  diagnosis_cause: str | None = None,
                  diagnosis_detail: str | None = None) -> int:
        with self._session() as s:
            evt = FrictionEvent(
                event_type=event_type,
                msg_id=msg_id,
                from_thread_id=from_thread_id,
                to_thread_id=to_thread_id,
                diagnosis_cause=diagnosis_cause,
                diagnosis_detail=diagnosis_detail,
            )
            s.add(evt)
            s.commit()
            return evt.id

    def get_recent(self, limit: int = 50) -> list:
        with self._session() as s:
            return list(s.execute(
                select(FrictionEvent).order_by(FrictionEvent.created_at.desc()).limit(limit)
            ).scalars().all())

    def get_by_cause(self, cause: str, limit: int = 50) -> list:
        with self._session() as s:
            return list(s.execute(
                select(FrictionEvent).where(FrictionEvent.diagnosis_cause == cause)
                .order_by(FrictionEvent.created_at.desc()).limit(limit)
            ).scalars().all())

    def count_by_cause(self) -> dict[str, int]:
        from sqlalchemy import func
        with self._session() as s:
            rows = s.execute(
                select(FrictionEvent.diagnosis_cause, func.count(FrictionEvent.id))
                .group_by(FrictionEvent.diagnosis_cause)
            ).all()
            return {r[0] or "unknown": r[1] for r in rows}

    def mark_lesson_applied(self, event_id: int):
        with self._session() as s:
            evt = s.get(FrictionEvent, event_id)
            if evt:
                evt.lesson_applied = 1
                s.commit()


