"""Person identity and group-membership storage.

This is the P2 identity layer: it answers *who* a message is from, without yet
claiming to understand *what kind of person* they are.  Observations, claims
and profiles will be layered on top later.

Design rules:

* Weak identities (nickname/remark/group nickname) never merge people
  automatically; they only reuse the exact same scoped identity row.
* Strong identities (wxid/external_id/phone/email) are the future auto-link
  source.
* All lookups are scoped by ``platform + account_id``; group nicknames also
  carry ``chat_id``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from core.contracts import ChatType
from core.identity.platforms import (
    adapter_for,
    chat_type_for,
    identity_type_for,
)
from db.meta_models import (
    GroupMembership,
    GroupMembershipPeriod,
    PersonEntity,
    PersonIdentity,
)
from db.repo_base import BaseRepository
from safelog import warn

STRONG_IDENTITY_TYPES = frozenset({
    "wxid",
    "qq_uin",
    "qq_uid",
    "telegram_user_id",
    "discord_user_id",
    "dingtalk_user_id",
    "feishu_user_id",
    "external_id",
    "phone",
    "email",
})
WEAK_IDENTITY_TYPES = frozenset({
    "nickname", "remark", "group_nickname", "alias", "display_name",
})
# Placeholders are *not* account ids even when a platform adapter stores them
# with ``identity_type="wxid"``: "unknown" from four groups can be four
# different strangers, and "self" is the bot's own alias.  Treating them as
# strong identifiers would merge unrelated people.
PLACEHOLDER_IDENTITY_VALUES = frozenset({"unknown", "self", "none", "null"})


def is_strong_identity(identity_type: Any, identity_value: Any) -> bool:
    """True when the identifier is account-wide (wxid/qq_uin/phone/...).

    Strong identifiers are unique inside one ``platform + account_id``; the
    ``chat_id`` where they were first seen must not split them into one person
    per group.  Placeholder values (``unknown``/``self``) are deliberately
    excluded: they carry no identity even when an adapter labels them "wxid".
    """
    kind = str(identity_type or "").strip().lower()
    value = str(identity_value or "").strip().lower()
    if kind not in STRONG_IDENTITY_TYPES:
        return False
    return value not in PLACEHOLDER_IDENTITY_VALUES


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


#: 会把「会话自己」写成发送人的 id 后缀（群系统消息 / 平台特殊账号）。
_SESSION_SENDER_SUFFIXES = ("@chatroom", "@im.chatroom")


def is_person_sender(value: Any, *, account_id: Any = "") -> bool:
    """这个发送人值能不能当「某个人」来建号（只认机械事实，不做语义推断）。

    拒：空值、占位值（``self`` / ``unknown`` / ``none`` / ``null``）、本账号自己、
    会话自身（``<群id>@chatroom`` 一类）。

    为什么需要它：本地库把「自己发的」写成 ``self``、把「取不到 wxid」写成
    ``unknown``、把群系统消息的发送人写成群 id；这些值被当成 wxid 建号之后，
    一个群会凭空多出两三个「成员」，而 ``unknown`` 名下会聚起几百条本该属于
    不同人的消息（2026-10-23 实测：互刷群 9 人 = 7 真人 + self + unknown）。
    """
    sender = _clean(value).lower()
    if not sender:
        return False
    if sender in PLACEHOLDER_IDENTITY_VALUES:
        return False
    if account_id and sender == _clean(account_id).lower():
        return False
    return not sender.endswith(_SESSION_SENDER_SUFFIXES)


class PeopleRepository(BaseRepository):
    # ------------------------------------------------------------------ people
    def get_person(self, person_id: str) -> PersonEntity | None:
        if not person_id:
            return None
        with self._session() as s:
            return s.get(PersonEntity, person_id)

    def find_persons(
        self,
        query: str,
        *,
        platform: str = "",
        account_id: str = "",
        limit: int = 20,
    ) -> list[PersonEntity]:
        query = _clean(query)
        if not query:
            return []
        with self._session() as s:
            q = select(PersonEntity).where(PersonEntity.status == "active")
            if platform:
                q = q.where(PersonEntity.platform == platform)
            if account_id:
                q = q.where(PersonEntity.account_id == account_id)
            q = q.where(PersonEntity.display_name.contains(query))
            direct = list(s.execute(q.limit(limit)).scalars().all())

            seen = {p.person_id for p in direct}
            iq = select(PersonIdentity).where(
                PersonIdentity.identity_value.contains(query)
            )
            if platform:
                iq = iq.where(PersonIdentity.platform == platform)
            if account_id:
                iq = iq.where(PersonIdentity.account_id == account_id)
            for identity in s.execute(iq.limit(limit * 3)).scalars().all():
                if identity.person_id in seen:
                    continue
                person = s.get(PersonEntity, identity.person_id)
                if person is not None and person.status == "active":
                    direct.append(person)
                    seen.add(person.person_id)
                if len(direct) >= limit:
                    break
        return direct

    def list_persons(
        self,
        *,
        platform: str = "",
        account_id: str = "",
        limit: int = 500,
    ) -> list[PersonEntity]:
        """List active canonical persons for a platform/account."""
        with self._session() as s:
            q = select(PersonEntity).where(PersonEntity.status == "active")
            if platform:
                q = q.where(PersonEntity.platform == platform)
            if account_id:
                q = q.where(PersonEntity.account_id == account_id)
            return list(
                s.execute(q.limit(max(1, int(limit or 500)))).scalars().all()
            )

    # --------------------------------------------------------------- identities
    def get_identity(
        self,
        platform: str,
        account_id: str,
        identity_type: str,
        identity_value: str,
        chat_id: str = "",
    ) -> PersonIdentity | None:
        with self._session() as s:
            return s.execute(
                select(PersonIdentity).where(
                    PersonIdentity.platform == platform,
                    PersonIdentity.account_id == account_id,
                    PersonIdentity.identity_type == identity_type,
                    PersonIdentity.identity_value == identity_value,
                    PersonIdentity.chat_id == chat_id,
                ).limit(1)
            ).scalar_one_or_none()

    def link_identity(
        self,
        person_id: str,
        *,
        platform: str,
        account_id: str,
        identity_type: str,
        identity_value: str,
        chat_id: str = "",
        confidence: float = 1.0,
        source: str = "",
    ) -> PersonIdentity:
        """Attach an identifier to an existing person.

        Raises ``ValueError`` when the identifier already belongs to another
        person; an explicit merge should be used in that case.
        """
        identity_value = _clean(identity_value)
        if not person_id or not identity_value:
            raise ValueError("person_id and identity_value are required")

        existing = self.get_identity(
            platform, account_id, identity_type, identity_value, chat_id,
        )
        if existing is not None:
            if existing.person_id != person_id:
                raise ValueError(
                    f"identity already linked to {existing.person_id!r}"
                )
            return existing

        with self._session() as s:
            person = s.get(PersonEntity, person_id)
            if person is None:
                raise ValueError(f"person not found: {person_id}")
            row = PersonIdentity(
                person_id=person_id,
                platform=platform,
                account_id=account_id,
                identity_type=identity_type,
                identity_value=identity_value,
                chat_id=chat_id,
                confidence=confidence,
                source=source,
                first_seen_at=datetime.now(),
                last_seen_at=datetime.now(),
            )
            s.add(row)
            s.commit()
            s.refresh(row)
            return row

    def resolve_identity(
        self,
        *,
        platform: str,
        account_id: str,
        identity_type: str,
        identity_value: str,
        chat_id: str = "",
        display_name: str = "",
        confidence: float = 1.0,
        source: str = "",
    ) -> PersonEntity | None:
        """Return the person for an identifier, creating one if needed.

        Strong identities (``wxid``/``qq_uin``/``phone``/``email``/...) are
        keyed by ``platform + account_id + identity_type + identity_value``:
        the same account id seen in three groups resolves to one person, and
        ``chat_id`` is only recorded as "seen here" on an extra identity row.

        Weak identities (nickname/group nickname/remark) create a provisional
        person on first sight and stay ``chat_id``-scoped; different scopes
        therefore never merge by nickname alone.
        """
        identity_type = _clean(identity_type) or "unknown"
        identity_value = _clean(identity_value)
        if not identity_value:
            return None
        display_name = _clean(display_name) or identity_value
        now = datetime.now()
        strong = is_strong_identity(identity_type, identity_value)

        with self._session() as s:
            conditions = [
                PersonIdentity.platform == platform,
                PersonIdentity.account_id == account_id,
                PersonIdentity.identity_type == identity_type,
                PersonIdentity.identity_value == identity_value,
            ]
            # 强标识在账号内全局唯一：chat_id 只是"在哪见过"，不进查重键。
            # 弱标识（昵称/群名片）仍按 chat 作用域，绝不凭昵称跨群合并。
            if not strong:
                conditions.append(PersonIdentity.chat_id == chat_id)
            existing = s.execute(
                select(PersonIdentity)
                .where(*conditions)
                .order_by(
                    PersonIdentity.first_seen_at.asc(),
                    PersonIdentity.id.asc(),
                )
                .limit(1)
            ).scalar_one_or_none()

            if existing is not None:
                existing.last_seen_at = now
                if confidence > (existing.confidence or 0.0):
                    existing.confidence = confidence
                person = s.get(PersonEntity, existing.person_id)
                if person is not None and display_name and not person.display_name:
                    person.display_name = display_name
                    person.updated_at = now
                if strong and chat_id and existing.chat_id != chat_id:
                    self._record_seen_scope(
                        s,
                        person_id=existing.person_id,
                        platform=platform,
                        account_id=account_id,
                        identity_type=identity_type,
                        identity_value=identity_value,
                        chat_id=chat_id,
                        confidence=confidence,
                        source=source,
                        now=now,
                    )
                s.commit()
                if person is not None:
                    s.refresh(person)
                return person

            person = PersonEntity(
                person_id=uuid.uuid4().hex,
                platform=platform,
                account_id=account_id,
                display_name=display_name,
                status="active",
                created_at=now,
                updated_at=now,
            )
            s.add(person)
            s.flush()
            identity = PersonIdentity(
                person_id=person.person_id,
                platform=platform,
                account_id=account_id,
                identity_type=identity_type,
                identity_value=identity_value,
                chat_id=chat_id,
                confidence=confidence,
                source=source,
                first_seen_at=now,
                last_seen_at=now,
            )
            s.add(identity)
            s.commit()
            s.refresh(person)
            return person

    @staticmethod
    def _record_seen_scope(
        session: Any,
        *,
        person_id: str,
        platform: str,
        account_id: str,
        identity_type: str,
        identity_value: str,
        chat_id: str,
        confidence: float,
        source: str,
        now: datetime,
    ) -> None:
        """Record "this account-wide id was also seen in ``chat_id``".

        The person is already resolved; this only keeps the per-chat provenance
        row for tools that ask "where have we seen this person".  A legacy row
        for the same scope may still point at another person (pre-merge data),
        in which case nothing is inserted — the merge tool owns that cleanup.
        """
        seen = session.execute(
            select(PersonIdentity.id).where(
                PersonIdentity.platform == platform,
                PersonIdentity.account_id == account_id,
                PersonIdentity.identity_type == identity_type,
                PersonIdentity.identity_value == identity_value,
                PersonIdentity.chat_id == chat_id,
            ).limit(1)
        ).scalar_one_or_none()
        if seen is not None:
            return
        try:
            with session.begin_nested():
                session.add(PersonIdentity(
                    person_id=person_id,
                    platform=platform,
                    account_id=account_id,
                    identity_type=identity_type,
                    identity_value=identity_value,
                    chat_id=chat_id,
                    confidence=confidence,
                    source=source,
                    first_seen_at=now,
                    last_seen_at=now,
                ))
        except IntegrityError:
            # Concurrent writer won the race; its row is equally valid.
            warn("db/repo_people.py:397 IntegrityError", err=repr(__import__("sys").exc_info()[1]))
            return

    def list_identities(self, person_id: str) -> Sequence[PersonIdentity]:
        with self._session() as s:
            return s.execute(
                select(PersonIdentity)
                .where(PersonIdentity.person_id == person_id)
                .order_by(PersonIdentity.first_seen_at)
            ).scalars().all()

    # ------------------------------------------------------------- memberships
    def upsert_membership(
        self,
        person_id: str,
        *,
        platform: str,
        account_id: str,
        chat_id: str,
        group_nickname: str = "",
        role: str = "member",
    ) -> GroupMembership:
        now = datetime.now()
        with self._session() as s:
            row = s.execute(
                select(GroupMembership).where(
                    GroupMembership.person_id == person_id,
                    GroupMembership.platform == platform,
                    GroupMembership.account_id == account_id,
                    GroupMembership.chat_id == chat_id,
                ).limit(1)
            ).scalar_one_or_none()
            if row is None:
                row = GroupMembership(
                    person_id=person_id,
                    platform=platform,
                    account_id=account_id,
                    chat_id=chat_id,
                    group_nickname=group_nickname,
                    role=role,
                    message_count=0,
                    first_seen_at=now,
                    last_seen_at=now,
                )
                s.add(row)
            else:
                if group_nickname:
                    row.group_nickname = group_nickname
                if role:
                    row.role = role
                row.last_seen_at = now
            s.commit()
            s.refresh(row)
            return row

    def record_membership_activity(
        self,
        person_id: str,
        *,
        platform: str,
        account_id: str,
        chat_id: str,
        group_nickname: str = "",
        role: str = "member",
    ) -> GroupMembership:
        row = self.upsert_membership(
            person_id,
            platform=platform,
            account_id=account_id,
            chat_id=chat_id,
            group_nickname=group_nickname,
            role=role,
        )
        with self._session() as s:
            current = s.get(GroupMembership, row.id)
            if current is not None:
                current.message_count = (current.message_count or 0) + 1
                current.last_seen_at = datetime.now()
                s.commit()
                s.refresh(current)
                return current
        return row

    def list_memberships(
        self,
        person_id: str,
        *,
        chat_id: str = "",
    ) -> Sequence[GroupMembership]:
        with self._session() as s:
            q = select(GroupMembership).where(
                GroupMembership.person_id == person_id
            )
            if chat_id:
                q = q.where(GroupMembership.chat_id == chat_id)
            return s.execute(q.order_by(GroupMembership.last_seen_at.desc())).scalars().all()

    # ----------------------------------------------------- membership periods
    def open_membership_period(
        self,
        person_id: str,
        *,
        platform: str,
        account_id: str,
        chat_id: str,
        joined_at: datetime | None = None,
        source: str = "membership_notice",
    ) -> int:
        """Open a new epoch or return the current open one."""
        now = joined_at or datetime.now()
        with self._session() as s:
            latest = s.execute(
                select(GroupMembershipPeriod)
                .where(
                    GroupMembershipPeriod.person_id == person_id,
                    GroupMembershipPeriod.platform == platform,
                    GroupMembershipPeriod.account_id == account_id,
                    GroupMembershipPeriod.chat_id == chat_id,
                )
                .order_by(GroupMembershipPeriod.epoch.desc())
                .limit(1)
            ).scalar_one_or_none()
            if latest is not None and latest.left_at is None:
                latest.updated_at = datetime.now()
                if source:
                    latest.source = source
                s.commit()
                return int(latest.epoch or 1)
            epoch = int(latest.epoch or 0) + 1 if latest is not None else 1
            row = GroupMembershipPeriod(
                person_id=person_id,
                platform=platform,
                account_id=account_id,
                chat_id=chat_id,
                joined_at=now,
                left_at=None,
                epoch=epoch,
                source=source,
                created_at=datetime.now(),
                updated_at=datetime.now(),
            )
            s.add(row)
            s.commit()
            s.refresh(row)
            return int(row.epoch or epoch)

    def close_membership_period(
        self,
        person_id: str,
        *,
        platform: str,
        account_id: str,
        chat_id: str,
        left_at: datetime | None = None,
        source: str = "membership_notice",
    ) -> bool:
        """Close the latest open epoch; False when none is open."""
        now = left_at or datetime.now()
        with self._session() as s:
            latest = s.execute(
                select(GroupMembershipPeriod)
                .where(
                    GroupMembershipPeriod.person_id == person_id,
                    GroupMembershipPeriod.platform == platform,
                    GroupMembershipPeriod.account_id == account_id,
                    GroupMembershipPeriod.chat_id == chat_id,
                    GroupMembershipPeriod.left_at.is_(None),
                )
                .order_by(GroupMembershipPeriod.epoch.desc())
                .limit(1)
            ).scalar_one_or_none()
            if latest is None:
                return False
            latest.left_at = now
            if source:
                latest.source = source
            latest.updated_at = datetime.now()
            s.commit()
            return True

    def list_membership_periods(
        self,
        person_id: str,
        *,
        platform: str = "",
        account_id: str = "",
        chat_id: str = "",
    ) -> Sequence[GroupMembershipPeriod]:
        with self._session() as s:
            q = select(GroupMembershipPeriod).where(
                GroupMembershipPeriod.person_id == person_id
            )
            if platform:
                q = q.where(GroupMembershipPeriod.platform == platform)
            if account_id:
                q = q.where(GroupMembershipPeriod.account_id == account_id)
            if chat_id:
                q = q.where(GroupMembershipPeriod.chat_id == chat_id)
            return s.execute(
                q.order_by(
                    GroupMembershipPeriod.epoch.asc(),
                    GroupMembershipPeriod.joined_at.asc(),
                )
            ).scalars().all()

    # ------------------------------------------------------------ message path
    def resolve_message(
        self,
        message: Mapping[str, Any],
        *,
        platform: str = "",
        account_id: str = "",
    ) -> PersonEntity | None:
        """Resolve the sender of a legacy message dict and touch membership."""
        platform = _clean(platform or message.get("platform") or message.get("source"))
        account_id = _clean(account_id or message.get("account_id"))
        sender_id = _clean(
            message.get("sender_id") or message.get("sender_wxid")
        )
        sender_name = _clean(message.get("sender_name") or "")
        chat_id = _clean(message.get("chat_id") or message.get("talker"))
        raw_chat_type = _clean(message.get("chat_type") or "")
        resolved_chat_type = chat_type_for(platform, raw_chat_type, chat_id)
        is_group = resolved_chat_type in (ChatType.GROUP, ChatType.CHANNEL)

        if sender_id:
            identity_type = identity_type_for(platform, message.get("identity_type"))
            identity_value = sender_id
            confidence = 0.95
        elif sender_name:
            adapter = adapter_for(platform)
            identity_type = (
                adapter.group_nickname_type if is_group else adapter.nickname_type
            )
            identity_value = sender_name
            confidence = 0.5
        else:
            return None

        person = self.resolve_identity(
            platform=platform,
            account_id=account_id,
            identity_type=identity_type,
            identity_value=identity_value,
            chat_id=chat_id if is_group else "",
            display_name=sender_name or identity_value,
            confidence=confidence,
            source="message",
        )
        if person is None:
            return None
        # 「群成员」是机械事实：只给真的是一笔人号的发送人记账。
        # self / unknown / <群id>@chatroom 建出来的成员关系会把名单和人数弄脏，
        # 但它们的消息仍然照旧走 identity/evidence（不在这里丢数据）。
        if is_group and chat_id and is_person_sender(sender_id or sender_name,
                                                    account_id=account_id):
            self.record_membership_activity(
                person.person_id,
                platform=platform,
                account_id=account_id,
                chat_id=chat_id,
                group_nickname=sender_name,
            )
        return person

    # -------------------------------------------------------------------- merge
    def merge_persons(self, source_person_id: str, target_person_id: str) -> bool:
        """Move identities/memberships from source to target.

        This is an explicit operation only; weak identities never trigger it.
        Duplicate scoped identities on the target are dropped from the source
        instead of violating the unique constraint.
        """
        source_person_id = _clean(source_person_id)
        target_person_id = _clean(target_person_id)
        if not source_person_id or not target_person_id:
            return False
        if source_person_id == target_person_id:
            return True

        with self._session() as s:
            source = s.get(PersonEntity, source_person_id)
            target = s.get(PersonEntity, target_person_id)
            if source is None or target is None:
                return False

            target_identities = {
                (i.platform, i.account_id, i.identity_type, i.identity_value, i.chat_id)
                for i in s.execute(
                    select(PersonIdentity).where(
                        PersonIdentity.person_id == target_person_id
                    )
                ).scalars().all()
            }
            for identity in s.execute(
                select(PersonIdentity).where(
                    PersonIdentity.person_id == source_person_id
                )
            ).scalars().all():
                key = (identity.platform, identity.account_id, identity.identity_type,
                       identity.identity_value, identity.chat_id)
                if key in target_identities:
                    s.delete(identity)
                else:
                    identity.person_id = target_person_id

            for membership in s.execute(
                select(GroupMembership).where(
                    GroupMembership.person_id == source_person_id
                )
            ).scalars().all():
                existing = s.execute(
                    select(GroupMembership).where(
                        GroupMembership.person_id == target_person_id,
                        GroupMembership.platform == membership.platform,
                        GroupMembership.account_id == membership.account_id,
                        GroupMembership.chat_id == membership.chat_id,
                    ).limit(1)
                ).scalar_one_or_none()
                if existing is None:
                    membership.person_id = target_person_id
                else:
                    existing.message_count = (
                        (existing.message_count or 0) + (membership.message_count or 0)
                    )
                    existing.last_seen_at = max(
                        existing.last_seen_at or datetime.min,
                        membership.last_seen_at or datetime.min,
                    )
                    s.delete(membership)

            source.status = "merged"
            source.updated_at = datetime.now()
            target.updated_at = datetime.now()
            s.commit()
            return True
