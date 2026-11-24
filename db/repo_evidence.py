"""Evidence index repository (P2).

The messages themselves live in per-account databases.  This repository keeps a
small, deduplicated projection in the shared meta database so later layers can
answer "which message is this, who sent it, and is it from the bot?" without
scanning every account DB.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Mapping, Sequence

from sqlalchemy import select

from db.meta_models import MessageEvidence
from db.repo_base import BaseRepository


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def content_hash(content: str) -> str:
    return hashlib.sha1((content or "").encode("utf-8", errors="replace")).hexdigest()


class EvidenceRepository(BaseRepository):
    def get(
        self,
        platform: str,
        account_id: str,
        chat_id: str,
        message_id: str,
    ) -> MessageEvidence | None:
        with self._session() as s:
            return s.execute(
                select(MessageEvidence).where(
                    MessageEvidence.platform == platform,
                    MessageEvidence.account_id == account_id,
                    MessageEvidence.chat_id == chat_id,
                    MessageEvidence.message_id == message_id,
                ).limit(1)
            ).scalar_one_or_none()

    def upsert(
        self,
        *,
        platform: str,
        account_id: str,
        chat_id: str,
        message_id: str,
        person_id: str | None = None,
        role: str = "user",
        source: str = "message",
        kind: str = "text",
        content: str = "",
        session_id: int | None = None,
        topic_id: int | None = None,
        created_at: datetime | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> MessageEvidence:
        platform = _clean(platform) or "unknown"
        account_id = _clean(account_id)
        chat_id = _clean(chat_id)
        message_id = _clean(message_id)
        if not message_id:
            raise ValueError("message_id is required")
        digest = content_hash(content) if content else None
        now = datetime.now()

        with self._session() as s:
            row = s.execute(
                select(MessageEvidence).where(
                    MessageEvidence.platform == platform,
                    MessageEvidence.account_id == account_id,
                    MessageEvidence.chat_id == chat_id,
                    MessageEvidence.message_id == message_id,
                ).limit(1)
            ).scalar_one_or_none()
            if row is None:
                row = MessageEvidence(
                    platform=platform,
                    account_id=account_id,
                    chat_id=chat_id,
                    message_id=message_id,
                    person_id=person_id or None,
                    role=role,
                    source=source,
                    kind=kind,
                    content_hash=digest,
                    session_id=session_id,
                    topic_id=topic_id,
                    created_at=created_at,
                    indexed_at=now,
                    metadata_json=json.dumps(dict(metadata or {}), ensure_ascii=False)
                    if metadata else None,
                )
                s.add(row)
            else:
                row.person_id = person_id or row.person_id
                row.role = role or row.role
                row.source = source or row.source
                row.kind = kind or row.kind
                if digest:
                    row.content_hash = digest
                if session_id is not None:
                    row.session_id = session_id
                if topic_id is not None:
                    row.topic_id = topic_id
                if created_at is not None:
                    row.created_at = created_at
                if metadata:
                    row.metadata_json = json.dumps(dict(metadata), ensure_ascii=False)
                row.indexed_at = now
            s.commit()
            s.refresh(row)
            return row

    def list_by_person(
        self,
        person_id: str,
        *,
        platform: str = "",
        account_id: str = "",
        limit: int = 100,
    ) -> Sequence[MessageEvidence]:
        with self._session() as s:
            q = select(MessageEvidence).where(
                MessageEvidence.person_id == person_id
            )
            if platform:
                q = q.where(MessageEvidence.platform == platform)
            if account_id:
                q = q.where(MessageEvidence.account_id == account_id)
            return s.execute(
                q.order_by(MessageEvidence.created_at.desc(), MessageEvidence.id.desc())
                .limit(limit)
            ).scalars().all()

    def list_by_chat(
        self,
        chat_id: str,
        *,
        platform: str = "",
        account_id: str = "",
        limit: int = 100,
    ) -> Sequence[MessageEvidence]:
        with self._session() as s:
            q = select(MessageEvidence).where(MessageEvidence.chat_id == chat_id)
            if platform:
                q = q.where(MessageEvidence.platform == platform)
            if account_id:
                q = q.where(MessageEvidence.account_id == account_id)
            return s.execute(
                q.order_by(MessageEvidence.created_at.desc(), MessageEvidence.id.desc())
                .limit(limit)
            ).scalars().all()

    def find_bot_message(
        self,
        platform: str,
        account_id: str,
        chat_id: str,
        message_id: str,
    ) -> MessageEvidence | None:
        row = self.get(platform, account_id, chat_id, message_id)
        if row is None or row.role != "assistant":
            return None
        return row

    def count(self) -> int:
        from sqlalchemy import func
        with self._session() as s:
            return int(s.execute(select(func.count()).select_from(MessageEvidence)).scalar() or 0)
