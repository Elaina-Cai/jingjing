from __future__ import annotations

from datetime import datetime
from typing import Sequence

from sqlalchemy import select, func

from db.models import Message, Contact
from db.repo_base import BaseRepository


class ContactRepository(BaseRepository):
    def get_by_wxid(self, wxid: str) -> Contact | None:
        with self._session() as s:
            return s.get(Contact, wxid)

    def search_by_name(self, query: str, limit: int = 10) -> list[Contact]:
        with self._session() as s:
            q = select(Contact).where(
                (Contact.nickname.contains(query)) |
                (Contact.remark.contains(query)) |
                (Contact.wxid.contains(query))
            ).limit(limit)
            return list(s.execute(q).scalars().all())

    def get_display_name(self, wxid: str) -> str:
        c = self.get_by_wxid(wxid)
        if c:
            return c.remark or c.nickname or wxid
        return wxid

    def list_all(
        self,
        is_group: int | None = None,
        limit: int = 500,
        has_messages: bool = False,
    ) -> Sequence[Contact]:
        with self._session() as s:
            q = select(Contact)
            if is_group is not None:
                q = q.where(Contact.is_group == is_group)
            if has_messages:
                msg_sub = (
                    select(Message.chat_id, func.max(Message.created_at).label("last_msg"))
                    .group_by(Message.chat_id)
                    .subquery()
                )
                q = q.join(msg_sub, Contact.wxid == msg_sub.c.chat_id)
                q = q.order_by(msg_sub.c.last_msg.desc())
            else:
                q = q.order_by(Contact.updated_at.desc())
            return s.execute(q.limit(limit)).scalars().all()

    def upsert(self, contact: Contact) -> Contact:
        with self._session() as s:
            existing = s.get(Contact, contact.wxid)
            if existing:
                existing.nickname = contact.nickname
                existing.remark = contact.remark
                existing.avatar_path = contact.avatar_path
                existing.is_group = contact.is_group
                existing.updated_at = datetime.now()
                s.commit()
                s.refresh(existing)
                return existing
            else:
                s.add(contact)
                s.commit()
                s.refresh(contact)
                return contact

    def upsert_from_dict(self, d: dict, account_id: str = "") -> Contact:
        with self._session() as s:
            wxid = d.get("wxid", "")
            existing = s.get(Contact, wxid) if wxid else None
            if existing:
                for k, v in d.items():
                    if k == "wxid":
                        continue
                    if hasattr(existing, k):
                        setattr(existing, k, v)
                if account_id:
                    existing.account_id = account_id
                existing.updated_at = datetime.now()
                s.commit()
                s.refresh(existing)
                return existing
            c = Contact()
            for k, v in d.items():
                if hasattr(c, k):
                    setattr(c, k, v)
            if account_id:
                c.account_id = account_id
            s.add(c)
            s.commit()
            s.refresh(c)
            return c
