from __future__ import annotations

from datetime import datetime, timedelta
from typing import Sequence

from sqlalchemy import select, func, delete

from db.models import Todo, KnowledgeDoc
from db.repo_base import BaseRepository


class TodoRepository(BaseRepository):
    def list_all(self, persona_id: int | None = None, status: str | None = None,
                 limit: int = 50) -> Sequence[Todo]:
        with self._session() as s:
            q = select(Todo).order_by(Todo.priority.desc(), Todo.created_at.desc())
            if persona_id:
                q = q.where(Todo.persona_id == persona_id)
            if status:
                q = q.where(Todo.status == status)
            return s.execute(q.limit(limit)).scalars().all()

    def get_by_id(self, todo_id: int) -> Todo | None:
        with self._session() as s:
            return s.get(Todo, todo_id)

    def add(self, todo: Todo) -> Todo:
        with self._session() as s:
            s.add(todo)
            s.commit()
            s.refresh(todo)
            return todo

    def update(self, todo_id: int, **fields) -> Todo | None:
        with self._session() as s:
            t = s.get(Todo, todo_id)
            if t:
                for k, v in fields.items():
                    if hasattr(t, k):
                        setattr(t, k, v)
                s.commit()
                s.refresh(t)
                return t
            return None

    def mark_done(self, todo_id: int):
        with self._session() as s:
            t = s.get(Todo, todo_id)
            if t:
                t.status = "done"
                t.completed_at = datetime.now()
                s.commit()

    def delete(self, todo_id: int):
        with self._session() as s:
            t = s.get(Todo, todo_id)
            if t:
                s.delete(t)
                s.commit()

    def get_overdue(self, persona_id: int | None = None) -> Sequence[Todo]:
        now = datetime.now()
        with self._session() as s:
            q = select(Todo).where(
                Todo.status == "pending",
                Todo.due_at.is_not(None),
                Todo.due_at < now,
            )
            if persona_id:
                q = q.where(Todo.persona_id == persona_id)
            return s.execute(q.order_by(Todo.due_at)).scalars().all()


class KnowledgeDocRepository(BaseRepository):
    def get(self, doc_id: int) -> KnowledgeDoc | None:
        with self._session() as s:
            return s.get(KnowledgeDoc, doc_id)

    def list_all(self, persona_id: int | None = None, limit: int = 100) -> Sequence[KnowledgeDoc]:
        with self._session() as s:
            q = select(KnowledgeDoc).order_by(KnowledgeDoc.created_at.desc())
            if persona_id:
                q = q.where(KnowledgeDoc.persona_id == persona_id)
            return s.execute(q.limit(limit)).scalars().all()

    def get_by_source(self, source_file: str) -> Sequence[KnowledgeDoc]:
        with self._session() as s:
            return s.execute(
                select(KnowledgeDoc).where(KnowledgeDoc.source_file == source_file)
                .order_by(KnowledgeDoc.chunk_index)
            ).scalars().all()

    def add(self, doc: KnowledgeDoc) -> KnowledgeDoc:
        with self._session() as s:
            s.add(doc)
            s.commit()
            s.refresh(doc)
            return doc

    def add_from_dict(self, d: dict) -> KnowledgeDoc:
        with self._session() as s:
            doc = KnowledgeDoc()
            for k, v in d.items():
                if hasattr(doc, k):
                    setattr(doc, k, v)
            s.add(doc)
            s.commit()
            s.refresh(doc)
            return doc

    def add_batch(self, docs: list[KnowledgeDoc]) -> int:
        with self._session() as s:
            s.add_all(docs)
            s.commit()
            return len(docs)

    def delete_by_source(self, source_file: str):
        with self._session() as s:
            s.execute(delete(KnowledgeDoc).where(KnowledgeDoc.source_file == source_file))
            s.commit()

    def delete(self, doc_id: int):
        with self._session() as s:
            d = s.get(KnowledgeDoc, doc_id)
            if d:
                s.delete(d)
                s.commit()

    def get_by_hash(self, content_hash: str) -> KnowledgeDoc | None:
        with self._session() as s:
            return s.execute(
                select(KnowledgeDoc).where(KnowledgeDoc.content_hash == content_hash)
            ).scalar_one_or_none()

    def get_by_source_url(self, source_url: str) -> KnowledgeDoc | None:
        with self._session() as s:
            return s.execute(
                select(KnowledgeDoc).where(KnowledgeDoc.source_url == source_url)
            ).scalar_one_or_none()

    def update(self, doc_id: int, **fields) -> KnowledgeDoc | None:
        with self._session() as s:
            d = s.get(KnowledgeDoc, doc_id)
            if d:
                for k, v in fields.items():
                    if hasattr(d, k):
                        setattr(d, k, v)
                s.commit()
                s.refresh(d)
                return d
            return None

    def count(self, persona_id: int | None = None) -> int:
        with self._session() as s:
            q = select(func.count()).select_from(KnowledgeDoc)
            if persona_id:
                q = q.where(KnowledgeDoc.persona_id == persona_id)
            return s.execute(q).scalar() or 0

    def get_stale(self, days: int = 30, limit: int = 100) -> Sequence[KnowledgeDoc]:
        cutoff = datetime.now() - timedelta(days=days)
        with self._session() as s:
            return s.execute(
                select(KnowledgeDoc)
                .where(KnowledgeDoc.last_accessed_at < cutoff)
                .order_by(KnowledgeDoc.access_count.asc())
                .limit(limit)
            ).scalars().all()
