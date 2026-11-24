"""Repository for Phase 5 Track B analysis projections and cursor."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any, Sequence

from sqlalchemy import delete, func, select

from core import temporal
from core.analysis.projectors import AnalysisProjectionDraft
from db.meta_models import AnalysisProjectionCursor, AnalysisProjectionRow
from db.repo_base import BaseRepository
from safelog import warn


def _loads(value: Any, fallback: Any) -> Any:
    if value in (None, ""):
        return fallback
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except Exception:
        warn("db/repo_analysis_projections.py:24 Exception", err=repr(__import__("sys").exc_info()[1]))
        return fallback


def _dumps(value: Any, fallback: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        warn("db/repo_analysis_projections.py:31 Exception", err=repr(__import__("sys").exc_info()[1]))
        return json.dumps(fallback, ensure_ascii=False)


def _aware(value: Any) -> datetime | None:
    """SQLite 回来的是 naive 本地墙钟；aware 的转本地再去 tzinfo（owner = ``temporal``）。"""
    if value is None:
        return None
    if getattr(value, "tzinfo", None) is None:
        return value
    return temporal.to_naive_local(value)


def _draft_from_row(row: AnalysisProjectionRow) -> AnalysisProjectionDraft:
    sources = _loads(getattr(row, "source_event_ids_json", "[]"), [])
    if isinstance(sources, str):
        sources = [sources]
    scopes = _loads(
        getattr(row, "visibility_scope_json", '["owner_only"]'),
        ["owner_only"],
    )
    if isinstance(scopes, str):
        scopes = [scopes]
    metadata = _loads(getattr(row, "metadata_json", "{}"), {})
    return AnalysisProjectionDraft(
        projection_id=str(getattr(row, "projection_id", "") or ""),
        projection_type=str(getattr(row, "projection_type", "") or ""),
        content=str(getattr(row, "content", "") or ""),
        summary=str(getattr(row, "summary", "") or ""),
        platform=str(getattr(row, "platform", "") or ""),
        bot_account_id=str(getattr(row, "bot_account_id", "") or ""),
        chat_id=str(getattr(row, "chat_id", "") or ""),
        chat_type=str(getattr(row, "chat_type", "") or ""),
        occurred_at=_aware(getattr(row, "occurred_at", None)),
        source_event_ids=tuple(str(value) for value in (sources or ())),
        visibility_scope=tuple(str(value) for value in (scopes or ())),
        sensitivity=str(getattr(row, "sensitivity", "") or "normal"),
        confidence=float(getattr(row, "confidence", 0.5) or 0.0),
        event_sequence=int(getattr(row, "event_sequence", 0) or 0),
        metadata=dict(metadata or {}),
    )


def _values(draft: AnalysisProjectionDraft) -> dict[str, Any]:
    return {
        "projection_id": str(draft.projection_id or ""),
        "projection_type": str(draft.projection_type or ""),
        "content": str(draft.content or ""),
        "summary": str(draft.summary or ""),
        "platform": str(draft.platform or ""),
        "bot_account_id": str(draft.bot_account_id or ""),
        "chat_id": str(draft.chat_id or ""),
        "chat_type": str(draft.chat_type or ""),
        "source_event_ids_json": _dumps(list(draft.source_event_ids or ()), []),
        "visibility_scope_json": _dumps(
            list(draft.visibility_scope or ("owner_only",)), ["owner_only"]
        ),
        "sensitivity": str(draft.sensitivity or "normal"),
        "event_sequence": int(draft.event_sequence or 0),
        "confidence": float(draft.confidence or 0.0),
        "occurred_at": draft.occurred_at,
        "metadata_json": _dumps(dict(draft.metadata or {}), {}),
    }


class AnalysisProjectionRepository(BaseRepository):
    """Persist and read event-sourced analysis projections."""

    def upsert(self, draft: AnalysisProjectionDraft) -> None:
        if not draft or not draft.projection_id:
            raise ValueError("analysis projection requires a projection_id")
        values = _values(draft)
        with self._session() as session:
            row = session.execute(
                select(AnalysisProjectionRow).where(
                    AnalysisProjectionRow.projection_id == values["projection_id"]
                )
            ).scalars().first()
            if row is None:
                row = AnalysisProjectionRow(projection_id=values["projection_id"])
                session.add(row)
            else:
                # A day/chat projection can be rebuilt from several bounded
                # worker batches.  Keep the union of provenance rather than
                # letting the last batch hide the earlier source events.
                try:
                    existing = _loads(row.source_event_ids_json, []) or []
                    incoming = _loads(values.get("source_event_ids_json"), []) or []
                    values["source_event_ids_json"] = _dumps(
                        list(dict.fromkeys([*existing, *incoming])), []
                    )
                    if str(values.get("projection_type") or "") in (
                        "message_analysis",
                        "group_summary",
                    ):
                        existing_content = str(getattr(row, "content", "") or "")
                        incoming_content = str(values.get("content", "") or "")
                        if (
                            existing_content
                            and incoming_content
                            and incoming_content not in existing_content
                        ):
                            values["content"] = (
                                f"{existing_content} | {incoming_content}"
                            )[-6000:]
                        existing_summary = str(getattr(row, "summary", "") or "")
                        incoming_summary = str(values.get("summary", "") or "")
                        if (
                            existing_summary
                            and incoming_summary
                            and incoming_summary not in existing_summary
                        ):
                            values["summary"] = (
                                f"{existing_summary} | {incoming_summary}"
                            )[-1200:]
                except Exception:
                    warn("db/repo_analysis_projections.py:146 Exception", err=repr(__import__("sys").exc_info()[1]))
            for key, value in values.items():
                setattr(row, key, value)
            row.updated_at = datetime.now()
            session.commit()

    def upsert_many(self, drafts: Sequence[AnalysisProjectionDraft]) -> int:
        written = 0
        for draft in drafts:
            self.upsert(draft)
            written += 1
        return written

    def get(self, projection_id: str) -> AnalysisProjectionDraft | None:
        if not projection_id:
            return None
        with self._session() as session:
            row = session.execute(
                select(AnalysisProjectionRow).where(
                    AnalysisProjectionRow.projection_id == str(projection_id)
                )
            ).scalars().first()
        return _draft_from_row(row) if row is not None else None

    def count(self) -> int:
        with self._session() as session:
            return int(
                session.execute(
                    select(func.count(AnalysisProjectionRow.id))
                ).scalar()
                or 0
            )

    def count_by_type(self) -> dict[str, int]:
        with self._session() as session:
            rows = session.execute(
                select(
                    AnalysisProjectionRow.projection_type,
                    func.count(AnalysisProjectionRow.id),
                ).group_by(AnalysisProjectionRow.projection_type)
            ).all()
        return {str(row[0] or ""): int(row[1] or 0) for row in rows}

    def all(self, limit: int | None = None) -> list[AnalysisProjectionDraft]:
        with self._session() as session:
            statement = select(AnalysisProjectionRow).order_by(
                AnalysisProjectionRow.occurred_at.asc(),
                AnalysisProjectionRow.id.asc(),
            )
            if limit is not None:
                statement = statement.limit(max(1, int(limit)))
            rows = list(session.execute(statement).scalars().all())
        return [_draft_from_row(row) for row in rows]

    def query_by_type(
        self,
        projection_type: str,
        *,
        limit: int = 500,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[AnalysisProjectionDraft]:
        with self._session() as session:
            statement = select(AnalysisProjectionRow).where(
                AnalysisProjectionRow.projection_type == str(projection_type or "")
            )
            if since is not None:
                statement = statement.where(
                    AnalysisProjectionRow.occurred_at >= since
                )
            if until is not None:
                statement = statement.where(
                    AnalysisProjectionRow.occurred_at <= until
                )
            rows = list(
                session.execute(
                    statement.order_by(
                        AnalysisProjectionRow.occurred_at.desc(),
                        AnalysisProjectionRow.id.desc(),
                    ).limit(max(1, int(limit)))
                ).scalars().all()
            )
            rows.reverse()
        return [_draft_from_row(row) for row in rows]

    def query_for_date(
        self,
        target_date: date,
        *,
        limit: int = 1000,
    ) -> list[AnalysisProjectionDraft]:
        start = datetime.combine(target_date, datetime.min.time())
        end = datetime.combine(target_date, datetime.max.time())
        candidates = self.all(limit=limit)
        return [
            draft
            for draft in candidates
            if draft.occurred_at is not None
            and start <= _aware(draft.occurred_at) <= end
        ]

    # --------------------------------------------------------------- cursor
    # ENG-23：与记忆投影同一套复合键语义 ``(last_sequence, last_id)``。
    def get_cursor_pair(self, key: str) -> tuple[int, int]:
        if not key:
            return (0, 0)
        with self._session() as session:
            row = session.get(AnalysisProjectionCursor, str(key))
            if row is None:
                return (0, 0)
            return (
                max(0, int(getattr(row, "last_sequence", 0) or 0)),
                max(0, int(getattr(row, "last_id", 0) or 0)),
            )

    def set_cursor_pair(self, key: str, sequence: int, last_id: int = 0) -> None:
        if not key:
            return
        with self._session() as session:
            row = session.get(AnalysisProjectionCursor, str(key))
            if row is None:
                row = AnalysisProjectionCursor(key=str(key))
                session.add(row)
            row.last_sequence = max(0, int(sequence or 0))
            row.last_id = max(0, int(last_id or 0))
            row.updated_at = datetime.now()
            session.commit()

    def get_cursor(self, key: str) -> int:
        return self.get_cursor_pair(key)[0]

    def set_cursor(self, key: str, sequence: int, last_id: int = 0) -> None:
        self.set_cursor_pair(key, sequence, last_id)

    def delete_all(self) -> None:
        with self._session() as session:
            session.execute(delete(AnalysisProjectionRow))
            session.execute(delete(AnalysisProjectionCursor))
            session.commit()


__all__ = ["AnalysisProjectionRepository"]
