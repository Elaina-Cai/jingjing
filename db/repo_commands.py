"""DB projection repository for the Phase 2 CommandService.

The append-only fact is ``terminal_events``; this repository persists the
command/authorization/query projection and its status transitions.  It never
deletes rows, so a revoked command retains its full audit history.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select

from core import temporal
from core.terminal.command_ledger import (
    AuthorizationRecord,
    CommandQueryLog,
    CommandRecord,
    CommandStatus,
    IdentityMode,
    PublicRenderPolicy,
)
from db.meta_models import (
    AuthorizationRecordRow,
    CommandQueryLogRow,
    CommandRecordRow,
)
from db.repo_base import BaseRepository
from safelog import warn


def _naive_local(value: Any) -> Any:
    """归一为应用时钟（naive 本地墙钟）；换算口径唯一 owner = ``core.temporal``。

    旧名 ``_aware_utc`` + 旧 docstring「command code always uses aware UTC」是错的：
    本仓库写入一律 ``datetime.now()``（naive 本地），这里**从不**产出 aware/UTC
    （产物是 naive，正是为了让读出的行能与落库值直接比较）。
    """
    if value is None:
        return None
    if getattr(value, "tzinfo", None) is None:
        return value
    return temporal.to_naive_local(value)


def _loads(value: Any, fallback: Any) -> Any:
    if value in (None, ""):
        return fallback
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except Exception:
        warn("db/repo_commands.py:54 Exception", err=repr(__import__("sys").exc_info()[1]))
        return fallback


def _dumps(value: Any, fallback: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        warn("db/repo_commands.py:61 Exception", err=repr(__import__("sys").exc_info()[1]))
        return json.dumps(fallback, ensure_ascii=False)


def _command_values(record: CommandRecord) -> dict[str, Any]:
    return {
        "command_id": str(record.command_id or ""),
        "created_at": record.created_at or datetime.now(),
        "issuer_person_id": str(record.issuer_person_id or ""),
        "issuer_account_id": str(record.issuer_account_id or ""),
        "issuer_role": str(record.issuer_role or "ordinary"),
        "origin_platform": str(record.origin_platform or ""),
        "origin_bot_account_id": str(record.origin_bot_account_id or ""),
        "origin_chat_id": str(record.origin_chat_id or ""),
        "origin_chat_type": str(record.origin_chat_type or "private"),
        "raw_request": str(record.raw_request or ""),
        "clarified_spec_json": _dumps(dict(record.clarified_spec or {}), {}),
        "status": (
            record.status.value
            if isinstance(record.status, CommandStatus)
            else str(record.status or CommandStatus.RECEIVED.value)
        ),
        "identity_mode": (
            record.identity_mode.value
            if isinstance(record.identity_mode, IdentityMode)
            else str(record.identity_mode or IdentityMode.AS_YINYUE.value)
        ),
        "nominal_name": str(record.nominal_name or ""),
        "target_person_ids_json": _dumps(list(record.target_person_ids or ()), []),
        "action_type": str(record.action_type or ""),
        "execution_ids_json": _dumps(list(record.execution_ids or ()), []),
        "group_queryable_after": record.group_queryable_after,
        "public_render_policy": (
            record.public_render_policy.value
            if isinstance(record.public_render_policy, PublicRenderPolicy)
            else str(
                record.public_render_policy or PublicRenderPolicy.AS_YINYUE.value
            )
        ),
        "authorization_id": str(record.authorization_id or ""),
        "revoked_at": record.revoked_at,
        "metadata_json": _dumps(dict(record.metadata or {}), {}),
    }


def _command_from_row(row: CommandRecordRow) -> CommandRecord:
    def _enum(enum_cls, value, default):
        try:
            return enum_cls(str(value if value not in (None, "") else default))
        except ValueError:
            warn("db/repo_commands.py:110 ValueError", err=repr(__import__("sys").exc_info()[1]))
            return enum_cls(default)

    return CommandRecord(
        command_id=str(getattr(row, "command_id", "") or ""),
        created_at=_naive_local(getattr(row, "created_at", None)) or datetime.now(),
        issuer_person_id=str(getattr(row, "issuer_person_id", "") or ""),
        issuer_account_id=str(getattr(row, "issuer_account_id", "") or ""),
        issuer_role=str(getattr(row, "issuer_role", "") or "ordinary"),
        origin_platform=str(getattr(row, "origin_platform", "") or ""),
        origin_bot_account_id=str(getattr(row, "origin_bot_account_id", "") or ""),
        origin_chat_id=str(getattr(row, "origin_chat_id", "") or ""),
        origin_chat_type=str(getattr(row, "origin_chat_type", "") or "private"),
        raw_request=str(getattr(row, "raw_request", "") or ""),
        clarified_spec=dict(_loads(getattr(row, "clarified_spec_json", "{}"), {}) or {}),
        status=_enum(
            CommandStatus,
            getattr(row, "status", ""),
            CommandStatus.RECEIVED.value,
        ),
        identity_mode=_enum(
            IdentityMode,
            getattr(row, "identity_mode", ""),
            IdentityMode.AS_YINYUE.value,
        ),
        nominal_name=str(getattr(row, "nominal_name", "") or ""),
        target_person_ids=tuple(
            _loads(getattr(row, "target_person_ids_json", "[]"), []) or ()
        ),
        action_type=str(getattr(row, "action_type", "") or ""),
        execution_ids=tuple(
            _loads(getattr(row, "execution_ids_json", "[]"), []) or ()
        ),
        group_queryable_after=_naive_local(getattr(row, "group_queryable_after", None)),
        public_render_policy=_enum(
            PublicRenderPolicy,
            getattr(row, "public_render_policy", ""),
            PublicRenderPolicy.AS_YINYUE.value,
        ),
        authorization_id=str(getattr(row, "authorization_id", "") or ""),
        revoked_at=_naive_local(getattr(row, "revoked_at", None)),
        metadata=dict(_loads(getattr(row, "metadata_json", "{}"), {}) or {}),
    )


def _authorization_from_row(row: AuthorizationRecordRow) -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id=str(getattr(row, "authorization_id", "") or ""),
        command_id=str(getattr(row, "command_id", "") or ""),
        raw_owner_conversation=list(
            _loads(getattr(row, "raw_owner_conversation_json", "[]"), []) or []
        ),
        clarification_events=list(
            _loads(getattr(row, "clarification_events_json", "[]"), []) or []
        ),
        final_confirmation=str(getattr(row, "final_confirmation", "") or ""),
        created_at=_naive_local(getattr(row, "created_at", None)) or datetime.now(),
        owner_only=bool(getattr(row, "owner_only", 1)),
    )


def _query_log_from_row(row: CommandQueryLogRow) -> CommandQueryLog:
    return CommandQueryLog(
        query_id=str(getattr(row, "query_id", "") or ""),
        requester_person_id=str(getattr(row, "requester_person_id", "") or ""),
        requester_account_id=str(getattr(row, "requester_account_id", "") or ""),
        queried_at=_naive_local(getattr(row, "queried_at", None)) or datetime.now(),
        command_ids=tuple(
            _loads(getattr(row, "command_ids_json", "[]"), []) or ()
        ),
        answer=str(getattr(row, "answer", "") or ""),
        context=dict(_loads(getattr(row, "context_json", "{}"), {}) or {}),
    )


class CommandRepository(BaseRepository):
    """Persistence projection for CommandService."""

    def __init__(self, engine):
        super().__init__(engine)

    # ------------------------------------------------------------ commands
    def upsert_command(self, record: CommandRecord) -> None:
        values = _command_values(record)
        with self._session() as session:
            row = session.execute(
                select(CommandRecordRow).where(
                    CommandRecordRow.command_id == values["command_id"]
                )
            ).scalars().first()
            if row is None:
                row = CommandRecordRow(command_id=values["command_id"])
                session.add(row)
            for key, value in values.items():
                setattr(row, key, value)
            row.updated_at = datetime.now()
            session.commit()

    def get_command(self, command_id: str) -> CommandRecord | None:
        if not command_id:
            return None
        with self._session() as session:
            row = session.execute(
                select(CommandRecordRow).where(
                    CommandRecordRow.command_id == str(command_id)
                )
            ).scalars().first()
        return _command_from_row(row) if row is not None else None

    def list_commands(self, limit: int | None = None) -> list[CommandRecord]:
        with self._session() as session:
            statement = select(CommandRecordRow).order_by(
                CommandRecordRow.created_at.asc(), CommandRecordRow.id.asc()
            )
            if limit is not None:
                statement = statement.limit(max(1, int(limit)))
            rows = session.execute(statement).scalars().all()
        return [_command_from_row(row) for row in rows]

    def count_commands(self) -> int:
        with self._session() as session:
            return int(
                session.execute(select(func.count(CommandRecordRow.id))).scalar() or 0
            )

    # ------------------------------------------------------- authorizations
    def upsert_authorization(self, record: AuthorizationRecord) -> None:
        with self._session() as session:
            row = session.execute(
                select(AuthorizationRecordRow).where(
                    AuthorizationRecordRow.authorization_id
                    == str(record.authorization_id)
                )
            ).scalars().first()
            if row is None:
                row = AuthorizationRecordRow(
                    authorization_id=str(record.authorization_id)
                )
                session.add(row)
            row.command_id = str(record.command_id or "")
            row.raw_owner_conversation_json = _dumps(
                list(record.raw_owner_conversation or ()), []
            )
            row.clarification_events_json = _dumps(
                list(record.clarification_events or ()), []
            )
            row.final_confirmation = str(record.final_confirmation or "")
            row.owner_only = 1 if record.owner_only else 0
            row.created_at = record.created_at or datetime.now()
            session.commit()

    def get_authorization(self, authorization_id: str) -> AuthorizationRecord | None:
        if not authorization_id:
            return None
        with self._session() as session:
            row = session.execute(
                select(AuthorizationRecordRow).where(
                    AuthorizationRecordRow.authorization_id
                    == str(authorization_id)
                )
            ).scalars().first()
        return _authorization_from_row(row) if row is not None else None

    def list_authorizations(
        self, limit: int | None = None
    ) -> list[AuthorizationRecord]:
        with self._session() as session:
            statement = select(AuthorizationRecordRow).order_by(
                AuthorizationRecordRow.created_at.asc(),
                AuthorizationRecordRow.id.asc(),
            )
            if limit is not None:
                statement = statement.limit(max(1, int(limit)))
            rows = session.execute(statement).scalars().all()
        return [_authorization_from_row(row) for row in rows]

    # ---------------------------------------------------------- query logs
    def add_query_log(self, log: CommandQueryLog) -> None:
        with self._session() as session:
            if not log.query_id:
                return
            existing = session.execute(
                select(CommandQueryLogRow.id).where(
                    CommandQueryLogRow.query_id == str(log.query_id)
                )
            ).scalar()
            if existing is not None:
                return
            session.add(CommandQueryLogRow(
                query_id=str(log.query_id),
                requester_person_id=str(log.requester_person_id or ""),
                requester_account_id=str(log.requester_account_id or ""),
                queried_at=log.queried_at or datetime.now(),
                command_ids_json=_dumps(list(log.command_ids or ()), []),
                answer=str(log.answer or ""),
                context_json=_dumps(dict(log.context or {}), {}),
            ))
            session.commit()

    def list_query_logs(self, limit: int | None = None) -> list[CommandQueryLog]:
        with self._session() as session:
            statement = select(CommandQueryLogRow).order_by(
                CommandQueryLogRow.queried_at.asc(), CommandQueryLogRow.id.asc()
            )
            if limit is not None:
                statement = statement.limit(max(1, int(limit)))
            rows = session.execute(statement).scalars().all()
        return [_query_log_from_row(row) for row in rows]

    def count_query_logs(self) -> int:
        with self._session() as session:
            return int(
                session.execute(select(func.count(CommandQueryLogRow.id))).scalar() or 0
            )
