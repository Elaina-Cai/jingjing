from __future__ import annotations

from datetime import datetime, timedelta

from db.models import SendLog
from db.repo_base import BaseRepository


class SendLogRepository(BaseRepository):
    def get(self, send_log_id: int) -> SendLog | None:
        with self._session() as s:
            return s.get(SendLog, send_log_id)


    def add(self, platform: str, chat_id: str, content: str | None,
            msg_type: str | None, source: str, trigger: str,
            account: str, chat_type: str | None = None,
            trigger_msg_id: int | None = None,
            extra: str | None = None) -> int:
        with self._session() as s:
            log = SendLog(
                platform=platform,
                chat_id=chat_id,
                chat_type=chat_type,
                content=content,
                msg_type=msg_type,
                source=source,
                trigger=trigger,
                trigger_msg_id=trigger_msg_id,
                account=account,
                status="pending",
                sent_at=datetime.now(),
                extra=extra,
            )
            s.add(log)
            s.commit()
            s.refresh(log)
            return log.id

    def claim_pending(self, platform: str, chat_id: str, session=None) -> SendLog | None:
        if session:
            log = session.query(SendLog).filter(
                SendLog.platform == platform,
                SendLog.chat_id == chat_id,
                SendLog.status == "pending",
            ).order_by(SendLog.sent_at.asc()).first()
            if log:
                log.status = "confirmed"
                log.confirmed_at = datetime.now()
            return log
        with self._session() as s:
            log = s.query(SendLog).filter(
                SendLog.platform == platform,
                SendLog.chat_id == chat_id,
                SendLog.status == "pending",
            ).order_by(SendLog.sent_at.asc()).first()
            if log:
                log.status = "confirmed"
                log.confirmed_at = datetime.now()
                s.commit()
                s.refresh(log)
            return log

    def mark_failed(self, send_log_id: int, error: str | None = None) -> None:
        with self._session() as s:
            log = s.get(SendLog, send_log_id)
            if log:
                log.status = "failed"
                log.error = error
                s.commit()

    def confirm_with_msg_id(self, send_log_id: int, msg_id: int, session=None) -> None:
        if session:
            log = session.get(SendLog, send_log_id)
            if log:
                log.status = "confirmed"
                log.confirmed_at = datetime.now()
                log.confirmed_msg_id = msg_id
            return
        with self._session() as s:
            log = s.get(SendLog, send_log_id)
            if log:
                log.status = "confirmed"
                log.confirmed_at = datetime.now()
                log.confirmed_msg_id = msg_id
                s.commit()

    def cleanup_stale(self, timeout_minutes: int = 5) -> int:
        cutoff = datetime.now() - timedelta(minutes=timeout_minutes)
        with self._session() as s:
            count = s.query(SendLog).filter(
                SendLog.status == "pending",
                SendLog.sent_at < cutoff,
            ).update({SendLog.status: "failed", SendLog.error: "超时未认领"})
            s.commit()
            return count

    def add_from_dict(self, d: dict) -> SendLog:
        with self._session() as s:
            log = SendLog()
            for k, v in d.items():
                if hasattr(log, k):
                    setattr(log, k, v)
            s.add(log)
            s.commit()
            s.refresh(log)
            return log
