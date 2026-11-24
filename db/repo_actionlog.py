from __future__ import annotations

from typing import Sequence

from sqlalchemy import select

from db.models import ActionLog
from db.repo_base import BaseRepository


class ActionLogRepository(BaseRepository):
    def add(self, tool_name: str, trigger: str, status: str,
            tool_args: str | None = None, result_summary: str | None = None,
            platform: str | None = None, account: str | None = None,
            target: str | None = None, trigger_msg_id: int | None = None,
            thread_id: int | None = None, error: str | None = None,
            duration_ms: int | None = None, token_usage: int | None = None,
            parent_action_id: int | None = None,
            related_send_log_id: int | None = None) -> int:
        with self._session() as s:
            log = ActionLog(
                tool_name=tool_name,
                tool_args=tool_args,
                result_summary=result_summary,
                platform=platform,
                account=account,
                target=target,
                trigger=trigger,
                trigger_msg_id=trigger_msg_id,
                thread_id=thread_id,
                status=status,
                error=error,
                duration_ms=duration_ms,
                token_usage=token_usage,
                parent_action_id=parent_action_id,
                related_send_log_id=related_send_log_id,
            )
            s.add(log)
            s.commit()
            s.refresh(log)
            return log.id

    def get_by_trigger_msg(self, trigger_msg_id: int) -> Sequence[ActionLog]:
        with self._session() as s:
            return s.execute(
                select(ActionLog)
                .where(ActionLog.trigger_msg_id == trigger_msg_id)
                .order_by(ActionLog.created_at.asc())
            ).scalars().all()

    def get_by_thread(self, thread_id: int) -> Sequence[ActionLog]:
        with self._session() as s:
            return s.execute(
                select(ActionLog)
                .where(ActionLog.thread_id == thread_id)
                .order_by(ActionLog.created_at.asc())
            ).scalars().all()

    def add_from_dict(self, d: dict) -> ActionLog:
        with self._session() as s:
            log = ActionLog()
            for k, v in d.items():
                if hasattr(log, k):
                    setattr(log, k, v)
            s.add(log)
            s.commit()
            s.refresh(log)
            return log
