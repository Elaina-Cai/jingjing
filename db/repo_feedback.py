"""Repository for Layer-2 quality feedback samples (eval collection only)."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Sequence

from sqlalchemy import delete, func, select

from db.meta_models import FeedbackSample
from db.repo_base import BaseRepository


class FeedbackRepository(BaseRepository):
    """Store real-usage quality signals; never touches recall."""

    def add(
        self,
        *,
        source: str,
        platform: str = "",
        bot_account_id: str = "",
        chat_id: str = "",
        chat_type: str = "",
        person_id: str = "",
        sample_text: str = "",
        previous_user_text: str = "",
        reply_event_id: str = "",
        reply_preview: str = "",
        metadata: dict[str, Any] | None = None,
        status: str = "new",
        created_at: datetime | None = None,
    ) -> FeedbackSample:
        row = FeedbackSample(
            source=str(source or "unknown"),
            platform=str(platform or ""),
            bot_account_id=str(bot_account_id or ""),
            chat_id=str(chat_id or ""),
            chat_type=str(chat_type or ""),
            person_id=str(person_id or ""),
            sample_text=str(sample_text or "")[:2000],
            previous_user_text=str(previous_user_text or "")[:2000],
            reply_event_id=str(reply_event_id or ""),
            reply_preview=str(reply_preview or "")[:2000],
            status=str(status or "new"),
            metadata_json=json.dumps(metadata or {}, ensure_ascii=False, default=str),
            created_at=created_at or datetime.now(),
        )
        with self._session() as session:
            session.add(row)
            session.commit()
            session.refresh(row)
            return row

    def recent_duplicate(
        self,
        *,
        chat_id: str,
        sample_text: str,
        platform: str,
        account_id: str,
        within_seconds: int = 600,
    ) -> bool:
        """True when the same signal was already collected moments ago."""
        text = str(sample_text or "").strip()
        if not text or not str(platform).strip() or not str(account_id).strip():
            return False
        cutoff = datetime.now().timestamp() - max(1, int(within_seconds))
        with self._session() as session:
            rows = session.execute(
                select(FeedbackSample)
                .where(FeedbackSample.chat_id == str(chat_id or ""),
                       FeedbackSample.platform == str(platform), FeedbackSample.bot_account_id == str(account_id))
                .order_by(FeedbackSample.id.desc())
                .limit(20)
            ).scalars().all()
        for row in rows:
            if str(row.sample_text or "").strip() != text:
                continue
            created = row.created_at or datetime.now()
            if created.timestamp() >= cutoff:
                return True
        return False

    def list_recent(
        self,
        *,
        limit: int = 100,
        status: str | None = None,
        source: str | None = None,
    ) -> Sequence[FeedbackSample]:
        with self._session() as session:
            statement = select(FeedbackSample)
            if status:
                statement = statement.where(FeedbackSample.status == str(status))
            if source:
                statement = statement.where(FeedbackSample.source == str(source))
            return list(
                session.execute(
                    statement.order_by(FeedbackSample.created_at.desc())
                    .limit(max(1, int(limit)))
                ).scalars().all()
            )

    def list_pending(
        self,
        *,
        limit: int = 100,
        source: str | None = None,
    ) -> Sequence[FeedbackSample]:
        """按**最老优先**返回待处理样本；这是消费扫描顺序的唯一入口。

        ``list_recent`` 服务界面/导出的"最近"语义；消费不能复用它，否则积压超过
        一批上限后，源源不断的新样本会让旧样本永远排不进扫描窗口。
        ``(created_at, id)`` 升序同时给出确定性的并列次序。
        """
        with self._session() as session:
            statement = select(FeedbackSample).where(
                FeedbackSample.status == "new"
            )
            if source:
                statement = statement.where(FeedbackSample.source == str(source))
            return list(
                session.execute(
                    statement.order_by(
                        FeedbackSample.created_at.asc(), FeedbackSample.id.asc()
                    ).limit(max(1, int(limit)))
                ).scalars().all()
            )

    def count(self, *, status: str | None = None) -> int:
        with self._session() as session:
            statement = select(func.count()).select_from(FeedbackSample)
            if status:
                statement = statement.where(FeedbackSample.status == str(status))
            return int(session.execute(statement).scalar() or 0)

    def mark_status(self, sample_id: int, status: str) -> bool:
        with self._session() as session:
            row = session.get(FeedbackSample, int(sample_id))
            if row is None:
                return False
            row.status = str(status or "reviewed")
            session.commit()
            return True

    def delete(self, sample_id: int) -> bool:
        with self._session() as session:
            row = session.get(FeedbackSample, int(sample_id))
            if row is None:
                return False
            session.delete(row)
            session.commit()
            return True

    def delete_all(self) -> None:
        with self._session() as session:
            session.execute(delete(FeedbackSample))
            session.commit()


__all__ = ["FeedbackRepository"]
