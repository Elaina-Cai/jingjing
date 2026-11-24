"""Persistence for durable conversation windows (P3)."""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

from sqlalchemy import select

from db.meta_models import ConversationWindowRecord
from db.repo_base import BaseRepository


class ConversationWindowRepository(BaseRepository):
    def get(
        self,
        *,
        platform: str,
        account_id: str,
        chat_id: str,
        person_id: str,
    ) -> ConversationWindowRecord | None:
        with self._session() as s:
            return s.execute(
                select(ConversationWindowRecord).where(
                    ConversationWindowRecord.platform == platform,
                    ConversationWindowRecord.account_id == account_id,
                    ConversationWindowRecord.chat_id == chat_id,
                    ConversationWindowRecord.person_id == person_id,
                ).limit(1)
            ).scalar_one_or_none()

    def upsert(
        self,
        *,
        platform: str,
        account_id: str,
        chat_id: str,
        person_id: str,
        state: str = "idle",
        topic: str = "",
        last_bot_message_id: str = "",
        last_send_log_id: str = "",
        last_bot_asked_question: bool = False,
        last_person_message_id: str = "",
        opened_at: datetime | None = None,
        last_activity_at: datetime | None = None,
        turn_count: int = 0,
        bot_turn_count: int = 0,
        confidence: float = 0.0,
    ) -> ConversationWindowRecord:
        now = datetime.now()
        with self._session() as s:
            row = s.execute(
                select(ConversationWindowRecord).where(
                    ConversationWindowRecord.platform == platform,
                    ConversationWindowRecord.account_id == account_id,
                    ConversationWindowRecord.chat_id == chat_id,
                    ConversationWindowRecord.person_id == person_id,
                ).limit(1)
            ).scalar_one_or_none()
            if row is None:
                row = ConversationWindowRecord(
                    platform=platform,
                    account_id=account_id,
                    chat_id=chat_id,
                    person_id=person_id,
                    state=state,
                    topic=topic,
                    last_bot_message_id=last_bot_message_id,
                    last_send_log_id=last_send_log_id,
                    last_bot_asked_question=1 if last_bot_asked_question else 0,
                    last_person_message_id=last_person_message_id,
                    opened_at=opened_at,
                    last_activity_at=last_activity_at,
                    turn_count=turn_count,
                    bot_turn_count=bot_turn_count,
                    confidence=confidence,
                    updated_at=now,
                )
                s.add(row)
            else:
                row.state = state
                row.topic = topic
                row.last_bot_message_id = last_bot_message_id
                row.last_send_log_id = last_send_log_id
                row.last_bot_asked_question = 1 if last_bot_asked_question else 0
                row.last_person_message_id = last_person_message_id
                row.opened_at = opened_at
                row.last_activity_at = last_activity_at
                row.turn_count = turn_count
                row.bot_turn_count = bot_turn_count
                row.confidence = confidence
                row.updated_at = now
            s.commit()
            s.refresh(row)
            return row

    def delete(
        self,
        *,
        platform: str,
        account_id: str,
        chat_id: str,
        person_id: str,
    ) -> None:
        with self._session() as s:
            row = s.execute(
                select(ConversationWindowRecord).where(
                    ConversationWindowRecord.platform == platform,
                    ConversationWindowRecord.account_id == account_id,
                    ConversationWindowRecord.chat_id == chat_id,
                    ConversationWindowRecord.person_id == person_id,
                ).limit(1)
            ).scalar_one_or_none()
            if row is not None:
                s.delete(row)
                s.commit()

    def list_open(
        self,
        *,
        platform: str = "",
        account_id: str = "",
        states: Sequence[str] = ("open", "waiting"),
        limit: int = 200,
    ) -> list[ConversationWindowRecord]:
        with self._session() as s:
            q = select(ConversationWindowRecord).where(
                ConversationWindowRecord.state.in_(list(states))
            )
            if platform:
                q = q.where(ConversationWindowRecord.platform == platform)
            if account_id:
                q = q.where(ConversationWindowRecord.account_id == account_id)
            return list(s.execute(
                q.order_by(ConversationWindowRecord.last_activity_at.desc())
                .limit(limit)
            ).scalars().all())

    def list_pending_bot_replies(
        self,
        *,
        platform: str = "",
        account_id: str = "",
        limit: int = 200,
    ) -> list[ConversationWindowRecord]:
        """Windows waiting for a confirmed platform id from the outbox/send log."""
        with self._session() as s:
            q = select(ConversationWindowRecord).where(
                ConversationWindowRecord.last_send_log_id != "",
                ConversationWindowRecord.last_bot_message_id == "",
            )
            if platform:
                q = q.where(ConversationWindowRecord.platform == platform)
            if account_id:
                q = q.where(ConversationWindowRecord.account_id == account_id)
            return list(s.execute(
                q.order_by(ConversationWindowRecord.last_activity_at.desc())
                .limit(limit)
            ).scalars().all())


    def clear(self) -> None:
        with self._session() as s:
            for row in s.execute(select(ConversationWindowRecord)).scalars().all():
                s.delete(row)
            s.commit()
