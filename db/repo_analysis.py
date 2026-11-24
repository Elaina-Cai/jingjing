from __future__ import annotations

import json
from datetime import datetime, date, timedelta
from typing import Any, Sequence

from sqlalchemy import select, and_, func

from db.models import Analysis, SyncState, UserProfile
from db.repo_base import BaseRepository
from safelog import warn


class AnalysisRepository(BaseRepository):
    def get_recent(
        self,
        chat_id: str | None = None,
        analysis_type: str | None = None,
        since: datetime | None = None,
        msg_since: datetime | None = None,
        limit: int = 50,
    ) -> Sequence[Analysis]:
        with self._session() as s:
            q = select(Analysis).order_by(Analysis.created_at.desc())
            if chat_id:
                q = q.where(Analysis.chat_id == chat_id)
            if analysis_type:
                q = q.where(Analysis.analysis_type == analysis_type)
            if since:
                q = q.where(Analysis.created_at >= since)
            if msg_since:
                q = q.where(Analysis.msg_time >= msg_since)
            return s.execute(q.limit(limit)).scalars().all()

    def count(
        self,
        analysis_type: str | None = None,
        since: datetime | None = None,
    ) -> int:
        """Small aggregate used by the analysis-pipeline trace."""
        with self._session() as s:
            q = select(func.count(Analysis.id))
            if analysis_type:
                q = q.where(Analysis.analysis_type == analysis_type)
            if since:
                q = q.where(Analysis.created_at >= since)
            return int(s.execute(q).scalar() or 0)

    def add(self, analysis: Analysis) -> Analysis:
        with self._session() as s:
            s.add(analysis)
            s.commit()
            s.refresh(analysis)
            return analysis

    def add_from_dict(self, d: dict) -> Analysis:
        with self._session() as s:
            a = Analysis()
            for k, v in d.items():
                if hasattr(a, k):
                    setattr(a, k, v)
            s.add(a)
            s.commit()
            s.refresh(a)
            return a

    def get_daily_digest(self, target_date: date) -> Analysis | None:
        start = datetime.combine(target_date, datetime.min.time())
        end = datetime.combine(target_date, datetime.max.time())
        with self._session() as s:
            return s.execute(
                select(Analysis).where(
                    Analysis.analysis_type == "daily_digest",
                    Analysis.created_at >= start,
                    Analysis.created_at <= end,
                ).order_by(Analysis.created_at.desc()).limit(1)
            ).scalar_one_or_none()


class SyncStateRepository(BaseRepository):
    def get(self, source: str) -> SyncState | None:
        with self._session() as s:
            return s.get(SyncState, source)

    def get_last_sync_ts(self, source: str) -> int:
        with self._session() as s:
            sync = s.get(SyncState, source)
            if sync and sync.last_sync:
                try:
                    ts = int(sync.last_sync.timestamp())
                    if ts < 0:
                        return 0
                    return ts
                except (OSError, ValueError, AttributeError):
                    warn("db/repo_analysis.py:93 (OSError, ValueError, AttributeError)", err=repr(__import__("sys").exc_info()[1]))
                    return 0
            return 0

    def get_cursors(self, source: str) -> dict[str, int]:
        """Return per-Msg_* table high-water marks.

        Rows are keyed by WeChat table name, e.g. ``Msg_<md5(wxid)>``.
        Unknown/corrupt JSON is treated as "no cursor yet" so the caller
        falls back to a one-time full reconcile instead of silently
        skipping a table.
        """
        with self._session() as s:
            sync = s.get(SyncState, source)
            raw = getattr(sync, "cursor_json", None) if sync else None
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            warn("db/repo_analysis.py:112 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
            return {}
        if not isinstance(data, dict):
            return {}
        cursors: dict[str, int] = {}
        for key, value in data.items():
            try:
                int_value = int(value)
            except (TypeError, ValueError):
                warn("db/repo_analysis.py:120 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
                continue
            if int_value >= -1:
                cursors[str(key)] = int_value
        return cursors

    def update_cursors(
        self,
        source: str,
        cursors: dict[str, int],
        status: str = "ok",
        error_msg: str | None = None,
    ):
        """Persist high-water marks after a successful table scan."""
        with self._session() as s:
            sync = s.get(SyncState, source)
            now = datetime.now()
            payload = json.dumps(cursors, ensure_ascii=False, sort_keys=True)
            if sync:
                sync.last_sync = now
                sync.status = status
                sync.error_msg = error_msg
                sync.cursor_json = payload
            else:
                s.add(SyncState(
                    source=source,
                    last_sync=now,
                    status=status,
                    error_msg=error_msg,
                    cursor_json=payload,
                ))
            s.commit()


    def update(self, source: str, status: str = "ok", error_msg: str | None = None):
        with self._session() as s:
            sync = s.get(SyncState, source)
            now = datetime.now()
            if sync:
                sync.last_sync = now
                sync.status = status
                sync.error_msg = error_msg
            else:
                s.add(SyncState(source=source, last_sync=now, status=status, error_msg=error_msg))
            s.commit()


class UserProfileRepository(BaseRepository):
    def get(self, key: str) -> str | None:
        with self._session() as s:
            p = s.get(UserProfile, key)
            return p.value if p else None

    def get_json(self, key: str, fallback: Any = None) -> Any:
        val = self.get(key)
        if val:
            try:
                return json.loads(val)
            except json.JSONDecodeError:
                warn("db/repo_analysis.py:178 json.JSONDecodeError", err=repr(__import__("sys").exc_info()[1]))
        return fallback

    def set(self, key: str, value: str):
        with self._session() as s:
            p = s.get(UserProfile, key)
            if p:
                p.value = value
                p.updated_at = datetime.now()
            else:
                s.add(UserProfile(key=key, value=value, updated_at=datetime.now()))
            s.commit()

    def set_json(self, key: str, value: Any):
        self.set(key, json.dumps(value, ensure_ascii=False))
