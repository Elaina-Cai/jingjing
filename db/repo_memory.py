from __future__ import annotations

from datetime import datetime
from typing import Sequence

from sqlalchemy import select, func, delete, and_, or_

from db.models import Conversation, ChatSession, CoreMemoryBlock
from db.repo_base import BaseRepository


class ConversationRepository(BaseRepository):
    def get_by_persona(
        self,
        persona_id: int,
        limit: int = 50,
    ) -> Sequence[Conversation]:
        with self._session() as s:
            return s.execute(
                select(Conversation)
                .where(Conversation.persona_id == persona_id)
                .order_by(Conversation.created_at.desc())
                .limit(limit)
            ).scalars().all()

    def get_by_session(
        self,
        session_id: int,
        limit: int = 100,
    ) -> Sequence[Conversation]:
        with self._session() as s:
            return s.execute(
                select(Conversation)
                .where(Conversation.session_id == session_id)
                .order_by(Conversation.created_at.desc())
                .limit(limit)
            ).scalars().all()

    # ----------------------------------------------------- identity backfill
    @staticmethod
    def _missing_identity_filter():
        return or_(
            Conversation.platform.is_(None),
            Conversation.account_id.is_(None),
            Conversation.chat_id.is_(None),
            and_(
                Conversation.role == "user",
                Conversation.sender_id.is_(None),
            ),
        )

    def list_missing_identity(
        self,
        *,
        after_id: int = 0,
        limit: int = 200,
    ) -> Sequence[Conversation]:
        """Rows that still lack the platform/chat/sender identity context."""
        with self._session() as s:
            return s.execute(
                select(Conversation)
                .where(
                    Conversation.id > int(after_id or 0),
                    self._missing_identity_filter(),
                )
                .order_by(Conversation.id.asc())
                .limit(max(1, int(limit or 1)))
            ).scalars().all()

    def count_missing_identity(self) -> int:
        with self._session() as s:
            return int(s.execute(
                select(func.count()).select_from(Conversation)
                .where(self._missing_identity_filter())
            ).scalar() or 0)

    def apply_identity(
        self,
        conversation_id: int,
        *,
        platform: str | None = None,
        account_id: str | None = None,
        chat_id: str | None = None,
        sender_id: str | None = None,
    ) -> int:
        """Fill only the supplied identity fields; returns fields changed."""
        updates = {
            key: value
            for key, value in (
                ("platform", platform),
                ("account_id", account_id),
                ("chat_id", chat_id),
                ("sender_id", sender_id),
            )
            if value is not None and str(value) != ""
        }
        if not updates:
            return 0
        with self._session() as s:
            row = s.get(Conversation, int(conversation_id))
            if row is None:
                return 0
            changed = 0
            for key, value in updates.items():
                if getattr(row, key, None) in (None, ""):
                    setattr(row, key, value)
                    changed += 1
            if changed:
                s.commit()
            return changed


    def add(self, conv: Conversation) -> Conversation:
        with self._session() as s:
            s.add(conv)
            s.commit()
            s.refresh(conv)
            return conv

    def add_from_dict(self, d: dict) -> Conversation:
        with self._session() as s:
            c = Conversation()
            for k, v in d.items():
                if hasattr(c, k):
                    setattr(c, k, v)
            s.add(c)
            s.commit()
            s.refresh(c)
            return c


class SessionRepository(BaseRepository):
    def get(self, session_id: int) -> ChatSession | None:
        with self._session() as s:
            return s.get(ChatSession, session_id)

    def list_by_persona(self, persona_id: int, limit: int = 50) -> Sequence[ChatSession]:
        with self._session() as s:
            return s.execute(
                select(ChatSession)
                .where(ChatSession.persona_id == persona_id)
                .order_by(ChatSession.updated_at.desc())
                .limit(limit)
            ).scalars().all()

    def add(self, session: ChatSession) -> ChatSession:
        with self._session() as s:
            s.add(session)
            s.commit()
            s.refresh(session)
            return session

    def touch(self, session_id: int):
        with self._session() as s:
            sess = s.get(ChatSession, session_id)
            if sess:
                sess.updated_at = datetime.now()
                s.commit()

    def rename(self, session_id: int, title: str):
        with self._session() as s:
            sess = s.get(ChatSession, session_id)
            if sess:
                sess.title = title
                sess.updated_at = datetime.now()
                s.commit()

    def delete(self, session_id: int):
        with self._session() as s:
            sess = s.get(ChatSession, session_id)
            if sess:
                s.execute(
                    delete(Conversation)
                    .where(Conversation.session_id == session_id)
                )
                s.delete(sess)
                s.commit()


class CoreMemoryRepository(BaseRepository):
    def list_blocks(self, persona_id: int) -> Sequence[CoreMemoryBlock]:
        with self._session() as s:
            return s.execute(
                select(CoreMemoryBlock)
                .where(CoreMemoryBlock.persona_id == persona_id)
            ).scalars().all()

    def upsert_block(self, persona_id: int, block: str, content: str):
        with self._session() as s:
            existing = s.execute(
                select(CoreMemoryBlock).where(
                    CoreMemoryBlock.persona_id == persona_id,
                    CoreMemoryBlock.block == block,
                )
            ).scalar_one_or_none()
            if existing:
                existing.content = content
                existing.updated_at = datetime.now()
            else:
                s.add(CoreMemoryBlock(
                    persona_id=persona_id, block=block, content=content,
                    updated_at=datetime.now(),
                ))
            s.commit()
