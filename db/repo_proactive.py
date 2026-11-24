"""Repository for the Phase 4 proactive whitelist and send log."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Sequence

from sqlalchemy import func, select

from db.meta_models import ProactiveSendLog, ProactiveTarget
from db.repo_base import BaseRepository
from safelog import warn


def _loads_quiet(value: str | None) -> list[int]:
    try:
        data = json.loads(value or "[]")
        if isinstance(data, list):
            return [int(item) for item in data[:2]]
    except Exception:
        warn("db/repo_proactive.py:20 Exception", err=repr(__import__("sys").exc_info()[1]))
    return []


class ProactiveTargetRepository(BaseRepository):
    def upsert_target(
        self,
        *,
        platform: str,
        bot_account_id: str,
        chat_id: str,
        chat_type: str = "private",
        enabled: bool = True,
        allow_sticker: bool = True,
        max_per_hour: int = 6,
        max_per_day: int = 20,
        cooldown_minutes: int = 60,
        quiet_hours: Sequence[int] | None = None,
        note: str = "",
    ) -> ProactiveTarget:
        now = datetime.now()
        with self._session() as session:
            row = session.execute(
                select(ProactiveTarget).where(
                    ProactiveTarget.platform == str(platform),
                    ProactiveTarget.bot_account_id == str(bot_account_id or ""),
                    ProactiveTarget.chat_id == str(chat_id or ""),
                ).limit(1)
            ).scalar_one_or_none()
            if row is None:
                row = ProactiveTarget(
                    platform=str(platform),
                    bot_account_id=str(bot_account_id or ""),
                    chat_id=str(chat_id or ""),
                )
                session.add(row)
            row.chat_type = str(chat_type or "private")
            row.enabled = 1 if enabled else 0
            row.allow_sticker = 1 if allow_sticker else 0
            row.max_per_hour = max(0, int(max_per_hour or 0))
            row.max_per_day = max(0, int(max_per_day or 0))
            row.cooldown_minutes = max(0, int(cooldown_minutes or 0))
            row.quiet_hours_json = json.dumps(
                [int(x) for x in (quiet_hours or ())][:2], ensure_ascii=False
            )
            row.note = str(note or "")
            row.updated_at = now
            if row.created_at is None:
                row.created_at = now
            session.commit()
            session.refresh(row)
            return row

    def get_target(
        self, platform: str, bot_account_id: str, chat_id: str
    ) -> ProactiveTarget | None:
        """Return a target, treating an empty stored bot id as a wildcard.

        The owner often whitelists a conversation without knowing which bot
        account will answer it.  An explicit blank ``bot_account_id`` in the
        UI/CLI therefore matches every account on that platform for that chat;
        per-account limits still use the concrete sending account in the log.
        """
        account = str(bot_account_id or "")
        with self._session() as session:
            row = session.execute(
                select(ProactiveTarget).where(
                    ProactiveTarget.platform == str(platform),
                    ProactiveTarget.bot_account_id == account,
                    ProactiveTarget.chat_id == str(chat_id or ""),
                ).limit(1)
            ).scalar_one_or_none()
            if row is not None or not account:
                return row
            return session.execute(
                select(ProactiveTarget).where(
                    ProactiveTarget.platform == str(platform),
                    ProactiveTarget.bot_account_id == "",
                    ProactiveTarget.chat_id == str(chat_id or ""),
                ).limit(1)
            ).scalar_one_or_none()

    def list_targets(
        self, *, enabled_only: bool = False, limit: int = 500
    ) -> list[ProactiveTarget]:
        with self._session() as session:
            statement = select(ProactiveTarget).order_by(ProactiveTarget.id.asc())
            if enabled_only:
                statement = statement.where(ProactiveTarget.enabled == 1)
            statement = statement.limit(max(1, int(limit)))
            return list(session.execute(statement).scalars().all())

    def get_target_by_id(self, target_id: int) -> ProactiveTarget | None:
        with self._session() as session:
            return session.get(ProactiveTarget, int(target_id))

    def set_enabled(
        self, platform: str, bot_account_id: str, chat_id: str, enabled: bool
    ) -> bool:
        row = self.get_target(platform, bot_account_id, chat_id)
        if row is None:
            return False
        with self._session() as session:
            current = session.get(ProactiveTarget, row.id)
            if current is None:
                return False
            current.enabled = 1 if enabled else 0
            current.updated_at = datetime.now()
            session.commit()
            return True

    def set_enabled_by_id(self, target_id: int, enabled: bool) -> bool:
        with self._session() as session:
            current = session.get(ProactiveTarget, int(target_id))
            if current is None:
                return False
            current.enabled = 1 if enabled else 0
            current.updated_at = datetime.now()
            session.commit()
            return True

    def delete_target(
        self, platform: str, bot_account_id: str, chat_id: str
    ) -> bool:
        return self.delete_by_id_values(
            platform=platform,
            bot_account_id=bot_account_id,
            chat_id=chat_id,
        )

    def delete_by_id_values(
        self,
        *,
        platform: str,
        bot_account_id: str,
        chat_id: str,
    ) -> bool:
        with self._session() as session:
            row = session.execute(
                select(ProactiveTarget).where(
                    ProactiveTarget.platform == str(platform),
                    ProactiveTarget.bot_account_id == str(bot_account_id or ""),
                    ProactiveTarget.chat_id == str(chat_id or ""),
                ).limit(1)
            ).scalar_one_or_none()
            if row is None:
                return False
            session.delete(row)
            session.commit()
            return True

    def delete_by_id(self, target_id: int) -> bool:
        with self._session() as session:
            row = session.get(ProactiveTarget, int(target_id))
            if row is None:
                return False
            session.delete(row)
            session.commit()
            return True

    # --------------------------------------------------------------- send log
    def record_send(
        self,
        *,
        platform: str,
        bot_account_id: str,
        chat_id: str,
        kind: str = "text",
        trigger: str = "proactive",
        content_preview: str = "",
        ok: bool = True,
        error: str = "",
        sent_at: datetime | None = None,
    ) -> ProactiveSendLog:
        row = ProactiveSendLog(
            platform=str(platform or ""),
            bot_account_id=str(bot_account_id or ""),
            chat_id=str(chat_id or ""),
            sent_at=sent_at or datetime.now(),
            kind=str(kind or "text"),
            trigger=str(trigger or "proactive"),
            content_preview=str(content_preview or "")[:500],
            ok=1 if ok else 0,
            error=str(error or "")[:300] or None,
        )
        with self._session() as session:
            session.add(row)
            session.commit()
            session.refresh(row)
            return row

    def count_since(
        self,
        *,
        platform: str,
        bot_account_id: str,
        chat_id: str,
        since: datetime,
    ) -> int:
        with self._session() as session:
            return int(
                session.execute(
                    select(func.count(ProactiveSendLog.id)).where(
                        ProactiveSendLog.platform == str(platform),
                        ProactiveSendLog.bot_account_id == str(bot_account_id or ""),
                        ProactiveSendLog.chat_id == str(chat_id or ""),
                        ProactiveSendLog.ok == 1,
                        ProactiveSendLog.sent_at >= since,
                    )
                ).scalar() or 0
            )

    def last_send_at(
        self, platform: str, bot_account_id: str, chat_id: str
    ) -> datetime | None:
        with self._session() as session:
            return session.execute(
                select(ProactiveSendLog.sent_at)
                .where(
                    ProactiveSendLog.platform == str(platform),
                    ProactiveSendLog.bot_account_id == str(bot_account_id or ""),
                    ProactiveSendLog.chat_id == str(chat_id or ""),
                    ProactiveSendLog.ok == 1,
                )
                .order_by(ProactiveSendLog.sent_at.desc())
                .limit(1)
            ).scalar()


    def list_send_log(
        self,
        *,
        platform: str = "",
        bot_account_id: str = "",
        chat_id: str = "",
        limit: int = 100,
    ) -> list[ProactiveSendLog]:
        with self._session() as session:
            statement = select(ProactiveSendLog).order_by(
                ProactiveSendLog.sent_at.desc(), ProactiveSendLog.id.desc()
            )
            if platform:
                statement = statement.where(
                    ProactiveSendLog.platform == str(platform)
                )
            if bot_account_id:
                statement = statement.where(
                    ProactiveSendLog.bot_account_id == str(bot_account_id)
                )
            if chat_id:
                statement = statement.where(
                    ProactiveSendLog.chat_id == str(chat_id)
                )
            statement = statement.limit(max(1, min(int(limit), 1000)))
            return list(session.execute(statement).scalars().all())
