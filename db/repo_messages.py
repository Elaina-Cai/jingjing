from __future__ import annotations

from datetime import datetime
from typing import Sequence

from sqlalchemy import select, func, and_, or_

from db.models import Message, Topic, Contact, is_group_chat_id
from db.repo_base import BaseRepository
from db.outbox_store import enqueue_message_outbox
from safelog import warn


class MessageRepository(BaseRepository):
    @staticmethod
    def _find_by_platform_id(session, msg_id: int, *,
                             chat_id: str | None = None,
                             account_id: str | None = None,
                             source: str | None = None) -> Message | None:
        q = select(Message).where(Message.msg_id == msg_id)
        if chat_id:
            q = q.where(Message.chat_id == chat_id)
        if account_id:
            q = q.where(Message.account_id == account_id)
        if source:
            q = q.where(Message.source == source)
        return session.execute(
            q.order_by(Message.id.desc()).limit(1)
        ).scalar_one_or_none()

    def _get_any(self, session, key: int) -> Message | None:
        """Look up by surrogate id first, then by platform msg_id."""
        row = session.get(Message, key)
        if row is not None:
            return row
        return self._find_by_platform_id(session, key)

    def get_by_id(self, msg_id: int) -> Message | None:
        with self._session() as s:
            return self._get_any(s, msg_id)

    def find_by_platform_id(self, msg_id: int | str) -> Message | None:
        """Look up by the platform message id, skipping surrogate ids."""
        with self._session() as s:
            return self._find_by_platform_id(s, msg_id)

    def find_by_identity(self, *, chat_id: str, msg_id: int | str,
                         source: str | None = None,
                         account_id: str | None = None) -> Message | None:
        """按 (chat, platform msg id) 精确找一行（outbox 投递用）。"""
        if msg_id in (None, ""):
            return None
        try:
            key: int | str = int(msg_id)
        except (TypeError, ValueError):
            warn("db/repo_messages.py:54 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
            key = str(msg_id)
        with self._session() as s:
            return self._find_by_platform_id(
                s, key, chat_id=chat_id or None,
                account_id=account_id or None, source=source or None,
            )

    def get_recent(
        self,
        chat_id: str | None = None,
        chat_type: str | None = None,
        source: str | None = None,
        sender_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        synced_since: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
        keyword: str | None = None,
    ) -> Sequence[Message]:
        with self._session() as s:
            q = select(Message).order_by(Message.created_at.desc(), Message.id.desc())
            if chat_id:
                q = q.where(Message.chat_id == chat_id)
            if chat_type:
                q = q.where(Message.chat_type == chat_type)
            if source:
                q = q.where(Message.source == source)
            if sender_id:
                q = q.where(Message.sender_id == sender_id)
            if since:
                q = q.where(Message.created_at >= since)
            if until:
                q = q.where(Message.created_at <= until)
            if synced_since:
                q = q.where(Message.synced_at >= synced_since)
            if keyword:
                # 2026-10-24：关键词下沉到 SQL。以前 search_messages 是先取最近 N 条
                # 再在 Python 里做子串过滤，于是「查三天前的记账表」几乎不可能命中。
                q = q.where(Message.content.like(f'%{keyword}%'))
            return s.execute(q.limit(limit).offset(offset)).scalars().all()

    def get_synced_after(
        self,
        *,
        after_synced_at: datetime | None = None,
        after_id: int = 0,
        limit: int = 500,
    ) -> Sequence[Message]:
        """Return messages after an evidence-worker cursor boundary.

        The cursor key is ``(synced_at, id)`` ordered ascending.  Messages with
        a NULL ``synced_at`` are legacy rows that the sync pipeline never
        claimed; they are handled by the historical backfill, not by the
        incremental worker.
        """
        with self._session() as s:
            q = select(Message).where(Message.synced_at.is_not(None))
            if after_synced_at is not None:
                q = q.where(or_(
                    Message.synced_at > after_synced_at,
                    and_(
                        Message.synced_at == after_synced_at,
                        Message.id > int(after_id or 0),
                    ),
                ))
            return s.execute(
                q.order_by(Message.synced_at.asc(), Message.id.asc())
                .limit(limit)
            ).scalars().all()


    def get_by_chat(self, chat_id: str, limit: int = 200,
                    sender_id: str | None = None) -> Sequence[Message]:
        with self._session() as s:
            q = (
                select(Message)
                .where(Message.chat_id == chat_id)
                .order_by(Message.created_at.desc())
            )
            if sender_id:
                q = q.where(Message.sender_id == sender_id)
            return s.execute(q.limit(limit)).scalars().all()

    def get_by_topic(self, topic_id: int, limit: int = 50) -> Sequence[Message]:
        with self._session() as s:
            return s.execute(
                select(Message)
                .where(Message.topic_id == topic_id)
                .order_by(Message.created_at.desc())
                .limit(limit)
            ).scalars().all()

    def assign_topic(self, msg_id: int, topic_id: int):
        with self._session() as s:
            m = self._get_any(s, msg_id)
            if m:
                old_tid = m.topic_id
                m.topic_id = topic_id
                s.commit()
                for tid in ({old_tid, topic_id} if old_tid else {topic_id}):
                    if not tid:
                        continue
                    t = s.get(Topic, tid)
                    if t:
                        cnt = s.execute(
                            select(func.count()).select_from(Message).where(Message.topic_id == tid)
                        ).scalar() or 0
                        t.msg_count = cnt
                s.commit()

    def assign_topic_batch(self, assignments: list[tuple[int, int]]):
        """批量分配: [(msg_id, topic_id), ...]，同时更新topic的msg_count。"""
        if not assignments:
            return
        with self._session() as s:
            affected_topics: set[int] = set()
            for msg_id, topic_id in assignments:
                m = self._get_any(s, msg_id)
                if m:
                    if m.topic_id and m.topic_id != topic_id:
                        affected_topics.add(m.topic_id)
                    m.topic_id = topic_id
                    affected_topics.add(topic_id)
            s.commit()
            for tid in affected_topics:
                t = s.get(Topic, tid)
                if t:
                    cnt = s.execute(
                        select(func.count()).select_from(Message).where(Message.topic_id == tid)
                    ).scalar() or 0
                    t.msg_count = cnt
            s.commit()

    def count_since(self, since: datetime, chat_id: str | None = None) -> int:
        with self._session() as s:
            q = select(func.count()).select_from(Message).where(Message.created_at >= since)
            if chat_id:
                q = q.where(Message.chat_id == chat_id)
            return s.execute(q).scalar() or 0

    def get_reply_chain(self, msg_id: int, max_depth: int = 5) -> list[Message]:
        """沿 quote_reply_id 向上追溯引用链，返回从根消息到当前消息的有序列表。"""
        chain: list[Message] = []
        visited: set[int] = set()
        with self._session() as s:
            current_id = msg_id
            for _ in range(max_depth):
                if current_id in visited:
                    break
                visited.add(current_id)
                m = self._get_any(s, current_id)
                if not m:
                    break
                chain.append(m)
                parent_id = m.quote_reply_id
                if not parent_id:
                    break
                try:
                    current_id = int(parent_id)
                except (ValueError, TypeError):
                    warn("db/repo_messages.py:215 (ValueError, TypeError)", err=repr(__import__("sys").exc_info()[1]))
                    break
        chain.reverse()
        return chain

    def get_replies_to(self, msg_id: int, limit: int = 20) -> Sequence[Message]:
        """查找所有引用了 msg_id 的回复消息。"""
        with self._session() as s:
            return s.execute(
                select(Message)
                .where(Message.quote_reply_id == str(msg_id))
                .order_by(Message.created_at.asc())
                .limit(limit)
            ).scalars().all()

    def get_topic_neighbors(self, msg_id: int, limit: int = 20) -> Sequence[Message]:
        """获取同 topic 下的相邻消息（上下文窗口）。"""
        with self._session() as s:
            m = self._get_any(s, msg_id)
            if not m or not m.topic_id:
                return []
            return s.execute(
                select(Message)
                .where(Message.topic_id == m.topic_id, Message.id != m.id)
                .order_by(Message.created_at.desc())
                .limit(limit)
            ).scalars().all()

    def get_chat_ids(self, chat_type: str | None = None) -> list[str]:
        with self._session() as s:
            q = select(Message.chat_id).distinct()
            if chat_type:
                q = q.where(Message.chat_type == chat_type)
            rows = s.execute(q).scalars().all()
            return list(rows)

    def list_conversations(self, limit: int = 500) -> list[dict]:
        """Newest-first per-chat summary used by the proactive target picker."""
        limit = max(1, min(int(limit or 500), 2000))
        with self._session() as s:
            grouped = s.execute(
                select(
                    Message.chat_id,
                    func.max(Message.created_at).label("last_at"),
                )
                .where(Message.chat_id.is_not(None), Message.chat_id != "")
                .group_by(Message.chat_id)
                .order_by(func.max(Message.created_at).desc())
                .limit(limit)
            ).all()
            try:
                contacts = {
                    str(row.wxid): row
                    for row in s.execute(select(Contact)).scalars().all()
                }
            except Exception:
                warn("db/repo_messages.py:270 Exception", err=repr(__import__("sys").exc_info()[1]))
                contacts = {}

            out: list[dict] = []
            for chat_id, last_at in grouped:
                chat_id = str(chat_id or "").strip()
                if not chat_id:
                    continue
                latest = s.execute(
                    select(Message)
                    .where(Message.chat_id == chat_id)
                    .order_by(Message.created_at.desc(), Message.id.desc())
                    .limit(1)
                ).scalar_one_or_none()
                if latest is None:
                    continue
                is_group = (
                    str(latest.chat_type or "") == "group"
                    or is_group_chat_id(chat_id)
                )
                contact = contacts.get(chat_id)
                display_name = ""
                if contact is not None:
                    display_name = str(
                        getattr(contact, "remark", "")
                        or getattr(contact, "nickname", "")
                        or ""
                    ).strip()
                if not display_name:
                    if is_group:
                        display_name = chat_id
                    else:
                        display_name = (
                            str(latest.sender_name or "").strip() or chat_id
                        )
                preview = str(
                    latest.content or latest.msg_type_name or ""
                ).replace("\n", " ").strip()
                out.append({
                    "chat_id": chat_id,
                    "chat_type": "group" if is_group else "private",
                    "display_name": display_name,
                    "last_message_at": (
                        last_at.isoformat(timespec="seconds")
                        if last_at is not None else ""
                    ),
                    "last_message_preview": preview[:80],
                    "is_self_last": bool(latest.is_self),
                })
            return out


    def add(self, msg: Message) -> Message:
        with self._session() as s:
            s.add(msg)
            enqueue_message_outbox(s, msg)
            s.commit()
            s.refresh(msg)
            return msg

    def add_batch(self, msgs: list[Message]) -> int:
        with self._session() as s:
            s.add_all(msgs)
            for msg in msgs:
                enqueue_message_outbox(s, msg)
            s.commit()
            return len(msgs)

    def exists(self, msg_id: int, *, chat_id: str | None = None,
               account_id: str | None = None, source: str | None = None) -> bool:
        with self._session() as s:
            if s.get(Message, msg_id) is not None:
                return True
            return self._find_by_platform_id(
                s, msg_id, chat_id=chat_id, account_id=account_id, source=source,
            ) is not None

    def to_dict(self, msg: Message) -> dict:
        sender_name = msg.sender_name
        if not sender_name and msg.sender_id:
            with self._session() as s:
                c = s.get(Contact, msg.sender_id)
                if c:
                    sender_name = c.remark or c.nickname or msg.sender_id
            if not sender_name:
                sender_name = msg.sender_id
        return {
            "id": msg.id,
            "msg_id": msg.msg_id,
            "local_id": msg.local_id,
            "sender_id": msg.sender_id,
            "sender_name": sender_name,
            "chat_id": msg.chat_id,
            "chat_type": msg.chat_type,
            "content": msg.content,
            "msg_type": msg.msg_type,
            "msg_sub_type": msg.msg_sub_type,
            "msg_type_name": msg.msg_type_name,
            "secondary_types": msg.secondary_types,
            "reply_to": msg.reply_to,
            "media_path": msg.media_path,
            "media_url": msg.media_url,
            "media_file_id": msg.media_file_id,
            "media_mime": msg.media_mime,
            "media_width": msg.media_width,
            "media_height": msg.media_height,
            "media_duration": msg.media_duration,
            "sticker_id": msg.sticker_id,
            "sticker_emoji": msg.sticker_emoji,
            "link_url": msg.link_url,
            "link_title": msg.link_title,
            "location_lat": msg.location_lat,
            "location_lon": msg.location_lon,
            "location_name": msg.location_name,
            "quote_reply_id": msg.quote_reply_id,
            "quote_reply_user": msg.quote_reply_user,
            "quote_reply_content": msg.quote_reply_content,
            "mentions": msg.mentions,
            "created_at": str(msg.created_at) if msg.created_at else None,
            "is_self": msg.is_self,
            "source": msg.source,
            "extra": msg.extra,
            "topic_id": msg.topic_id,
        }

    def bulk_insert(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        with self._session() as s:
            for d in rows:
                msg_id = d.get("msg_id")
                if msg_id is None:
                    continue
                existing = self._find_by_platform_id(
                    s, msg_id,
                    chat_id=d.get("chat_id"),
                    account_id=d.get("account_id"),
                    source=d.get("source"),
                )
                if existing:
                    continue
                m = Message()
                for k, v in d.items():
                    if hasattr(m, k):
                        setattr(m, k, v)
                s.add(m)
            s.commit()
        return len(rows)
