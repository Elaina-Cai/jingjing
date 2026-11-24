from __future__ import annotations

from datetime import datetime
from typing import Optional, Sequence

from sqlalchemy import select, and_, or_, func, delete
from sqlalchemy.orm import Session

from db.models import Topic, Thread, ThreadMsgRef
from db.repo_base import BaseRepository


class TopicRepository(BaseRepository):
    def get_active(self, chat_id: str, limit: int = 10) -> Sequence[Topic]:
        with self._session() as s:
            return s.execute(
                select(Topic).where(
                    Topic.chat_id == chat_id,
                    Topic.status == "active",
                ).order_by(Topic.last_msg_at.desc()).limit(limit)
            ).scalars().all()

    def get_by_id(self, topic_id: int) -> Topic | None:
        with self._session() as s:
            return s.get(Topic, topic_id)

    def create(self, topic: Topic) -> Topic:
        with self._session() as s:
            s.add(topic)
            s.commit()
            s.refresh(topic)
            return topic

    def update(self, topic_id: int, **fields):
        with self._session() as s:
            t = s.get(Topic, topic_id)
            if t:
                for k, v in fields.items():
                    if hasattr(t, k):
                        setattr(t, k, v)
                s.commit()
                s.refresh(t)
                return t

    def touch(self, topic_id: int, msg_at: datetime | None = None):
        with self._session() as s:
            t = s.get(Topic, topic_id)
            if t:
                t.last_msg_at = msg_at or datetime.now()
                t.msg_count = (t.msg_count or 0) + 1
                t.updated_at = datetime.now()
                s.commit()

    def close_stale(self, chat_id: str, before: datetime):
        with self._session() as s:
            s.execute(
                Topic.__table__.update().where(
                    Topic.chat_id == chat_id,
                    Topic.status == "active",
                    Topic.last_msg_at < before,
                ).values(status="closed")
            )
            s.commit()

    def get_recent_closed(self, chat_id: str, limit: int = 5) -> Sequence[Topic]:
        with self._session() as s:
            return s.execute(
                select(Topic).where(
                    Topic.chat_id == chat_id,
                    Topic.status == "closed",
                ).order_by(Topic.last_msg_at.desc()).limit(limit)
            ).scalars().all()

    def reopen(self, topic_id: int):
        with self._session() as s:
            t = s.get(Topic, topic_id)
            if t and t.status == "closed":
                t.status = "active"
                s.commit()

    def delete_topic(self, topic_id: int) -> bool:
        with self._session() as s:
            t = s.get(Topic, topic_id)
            if not t:
                return False
            s.execute(delete(ThreadMsgRef).where(ThreadMsgRef.thread_id == topic_id))
            s.delete(t)
            s.commit()
            return True


class ThreadRepository(BaseRepository):
    def find_scoped(self, platform: str, account_id: str, *, chat_id: str = "", limit: int = 200):
        if not platform or not account_id:
            return []
        with self._session() as s:
            query = select(Thread).where(Thread.platform == platform, Thread.account_id == account_id, Thread.status == "active")
            if chat_id:
                query = query.where(Thread.chat_id == chat_id)
            return s.execute(query.order_by(Thread.last_active_at.desc()).limit(limit)).scalars().all()

    def get_active(self, chat_id: str, limit: int = 20) -> Sequence[Thread]:
        with self._session() as s:
            return s.execute(
                select(Thread).where(
                    Thread.chat_id == chat_id,
                    Thread.status == "active",
                ).order_by(Thread.last_active_at.desc()).limit(limit)
            ).scalars().all()

    def get_by_id(self, thread_id: int) -> Thread | None:
        with self._session() as s:
            return s.get(Thread, thread_id)

    def get_recent_active(self, chat_id: str, since: datetime, limit: int = 20) -> Sequence[Thread]:
        with self._session() as s:
            return s.execute(
                select(Thread).where(
                    Thread.chat_id == chat_id,
                    Thread.status == "active",
                    Thread.last_active_at >= since,
                ).order_by(Thread.last_active_at.desc()).limit(limit)
            ).scalars().all()

    def get_all_active(self, limit: int = 100) -> Sequence[Thread]:
        with self._session() as s:
            return s.execute(
                select(Thread).where(
                    Thread.status == "active",
                ).order_by(Thread.last_active_at.desc()).limit(limit)
            ).scalars().all()

    def create(self, thread: Thread) -> Thread:
        with self._session() as s:
            s.add(thread)
            s.commit()
            s.refresh(thread)
            return thread

    def update(self, thread_id: int, **fields):
        with self._session() as s:
            t = s.get(Thread, thread_id)
            if t:
                for k, v in fields.items():
                    if hasattr(t, k):
                        setattr(t, k, v)
                s.commit()
                s.refresh(t)
                return t

    def touch(self, thread_id: int, msg_at: datetime | None = None):
        with self._session() as s:
            t = s.get(Thread, thread_id)
            if t:
                t.last_active_at = msg_at or datetime.now()
                t.msg_count = (t.msg_count or 0) + 1
                t.updated_at = datetime.now()
                s.commit()

    def close_stale(self, chat_id: str, before: datetime):
        with self._session() as s:
            s.execute(
                Thread.__table__.update().where(
                    Thread.chat_id == chat_id,
                    Thread.status == "active",
                    Thread.last_active_at < before,
                ).values(status="closed")
            )
            s.commit()

    def get_children(self, thread_id: int) -> Sequence[Thread]:
        with self._session() as s:
            return s.execute(
                select(Thread).where(Thread.parent_id == thread_id)
                .order_by(Thread.last_active_at.desc())
            ).scalars().all()

    def find_by_msg_id(self, msg_id: int) -> Thread | None:
        with self._session() as s:
            ref = s.execute(
                select(ThreadMsgRef).where(
                    ThreadMsgRef.msg_id == msg_id,
                    ThreadMsgRef.status == "confirmed",
                ).limit(1)
            ).scalar_one_or_none()
            if ref:
                return s.get(Thread, ref.thread_id)
            return None

    def get_thread_tree(self, chat_id: str) -> list[dict]:
        with self._session() as s:
            roots = s.execute(
                select(Thread).where(
                    Thread.chat_id == chat_id,
                    Thread.parent_id.is_(None),
                ).order_by(Thread.last_active_at.desc())
            ).scalars().all()
            return [self._build_tree(s, t) for t in roots]

    def _build_tree(self, s: Session, thread: Thread) -> dict:
        children = s.execute(
            select(Thread).where(Thread.parent_id == thread.id)
            .order_by(Thread.last_active_at.desc())
        ).scalars().all()
        return {
            "id": thread.id,
            "scope": thread.scope,
            "status": thread.status,
            "msg_count": thread.msg_count,
            "last_active_at": thread.last_active_at.isoformat() if thread.last_active_at else None,
            "children": [self._build_tree(s, c) for c in children],
        }


class ThreadMsgRefRepository(BaseRepository):
    @staticmethod
    def _validate_source(session, ref):
        from db.thread_scope import source_key
        parent = session.get(Thread, ref.thread_id)
        key = source_key(ref)
        if key is None or parent is None or key != source_key(parent):
            raise ValueError("thread_ref_source_mismatch: 引用必须有与线程一致的明确来源")

    def add(self, ref: ThreadMsgRef) -> ThreadMsgRef:
        with self._session() as s:
            self._validate_source(s, ref)
            s.add(ref)
            s.commit()
            s.refresh(ref)
            return ref

    def add_batch(self, refs: list[ThreadMsgRef]) -> int:
        with self._session() as s:
            for ref in refs:
                self._validate_source(s, ref)
            s.add_all(refs)
            s.commit()
            return len(refs)

    def get_by_thread(self, thread_id: int, status: str | None = None,
                      limit: int = 100, offset: int = 0) -> Sequence[ThreadMsgRef]:
        with self._session() as s:
            q = select(ThreadMsgRef).where(ThreadMsgRef.thread_id == thread_id)
            if status:
                q = q.where(ThreadMsgRef.status == status)
            q = q.order_by(ThreadMsgRef.id.desc()).offset(offset).limit(limit)
            return s.execute(q).scalars().all()

    def get_by_msg(self, msg_id: int) -> Sequence[ThreadMsgRef]:
        with self._session() as s:
            return s.execute(
                select(ThreadMsgRef).where(ThreadMsgRef.msg_id == msg_id)
            ).scalars().all()

    def count_by_thread(self, thread_id: int, status: str | None = None) -> int:
        with self._session() as s:
            q = select(func.count()).select_from(ThreadMsgRef).where(
                ThreadMsgRef.thread_id == thread_id
            )
            if status:
                q = q.where(ThreadMsgRef.status == status)
            return s.execute(q).scalar() or 0

    def remove(self, thread_id: int, msg_id: int):
        with self._session() as s:
            s.execute(
                delete(ThreadMsgRef).where(
                    ThreadMsgRef.thread_id == thread_id,
                    ThreadMsgRef.msg_id == msg_id,
                )
            )
            s.commit()

    def update_status(self, thread_id: int, msg_id: int, status: str,
                      relevance: float | None = None, confidence: float | None = None):
        with self._session() as s:
            ref = s.execute(
                select(ThreadMsgRef).where(
                    ThreadMsgRef.thread_id == thread_id,
                    ThreadMsgRef.msg_id == msg_id,
                ).limit(1)
            ).scalar_one_or_none()
            if ref:
                ref.status = status
                if relevance is not None:
                    ref.relevance = relevance
                if confidence is not None:
                    ref.confidence = confidence
                s.commit()

    def get_tentative_by_thread(self, thread_id: int) -> Sequence[ThreadMsgRef]:
        return self.get_by_thread(thread_id, status="tentative")

    def get_msg_ids_by_thread(self, thread_id: int, status: str | None = None) -> list[int]:
        with self._session() as s:
            q = select(ThreadMsgRef.msg_id).where(ThreadMsgRef.thread_id == thread_id)
            if status:
                q = q.where(ThreadMsgRef.status == status)
            return list(s.execute(q).scalars().all())
