"""Command ledger with owner/issuer/group visibility for Phase 0.

The ledger stores commands (questions, requests and explicit instructions),
authorization records, and query logs.  It renders answers in natural language
and never returns raw JSON to a non-owner respondent.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Iterable

from core import temporal
from core.terminal.identity_service import IdentityService
from core.terminal.principal import Capability, PrincipalContext
from safelog import warn


class CommandStatus(StrEnum):
    RECEIVED = "received"
    PENDING_CLARIFY = "pending_clarify"
    CONFIRMED = "confirmed"
    EXECUTING = "executing"
    DONE = "done"
    FAILED = "failed"
    REVOKED = "revoked"


class IdentityMode(StrEnum):
    AS_YINYUE = "as_yinyue"
    AS_OWNER_NAMED = "as_owner_named"


class PublicRenderPolicy(StrEnum):
    AS_YINYUE = "as_yinyue"
    OWNER_NAMED = "owner_named"
    REDACTED = "redacted"


class CommandAccessDenied(RuntimeError):
    pass  # noqa: silent（有意忽略，见 CONTRACT 第八条）


def utcnow() -> datetime:
    return datetime.now()


@dataclass
class CommandRecord:
    command_id: str
    created_at: datetime
    issuer_person_id: str
    issuer_account_id: str
    issuer_role: str
    origin_platform: str
    origin_bot_account_id: str
    origin_chat_id: str
    origin_chat_type: str
    raw_request: str
    clarified_spec: dict = field(default_factory=dict)
    status: CommandStatus = CommandStatus.RECEIVED
    identity_mode: IdentityMode = IdentityMode.AS_YINYUE
    nominal_name: str = ""
    target_person_ids: tuple[str, ...] = ()
    action_type: str = ""
    execution_ids: tuple[str, ...] = ()
    group_queryable_after: datetime | None = None
    public_render_policy: PublicRenderPolicy = PublicRenderPolicy.AS_YINYUE
    authorization_id: str = ""
    revoked_at: datetime | None = None
    metadata: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "command_id": self.command_id,
            "created_at": self.created_at.isoformat() if self.created_at else "",
            "issuer_person_id": self.issuer_person_id,
            "issuer_account_id": self.issuer_account_id,
            "issuer_role": self.issuer_role,
            "origin_platform": self.origin_platform,
            "origin_bot_account_id": self.origin_bot_account_id,
            "origin_chat_id": self.origin_chat_id,
            "origin_chat_type": self.origin_chat_type,
            "raw_request": self.raw_request,
            "clarified_spec": self.clarified_spec,
            "status": self.status.value,
            "identity_mode": self.identity_mode.value,
            "nominal_name": self.nominal_name,
            "target_person_ids": list(self.target_person_ids),
            "action_type": self.action_type,
            "execution_ids": list(self.execution_ids),
            "group_queryable_after": (
                self.group_queryable_after.isoformat()
                if self.group_queryable_after
                else ""
            ),
            "public_render_policy": self.public_render_policy.value,
            "authorization_id": self.authorization_id,
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else "",
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CommandRecord":
        def _dt(value):
            if not value:
                return None
            try:
                return datetime.fromisoformat(str(value))
            except ValueError:
                warn("core/terminal/command_ledger.py:112 ValueError", err=repr(__import__("sys").exc_info()[1]))
                return None

        return cls(
            command_id=str(data.get("command_id") or uuid.uuid4().hex),
            created_at=_dt(data.get("created_at")) or utcnow(),
            issuer_person_id=str(data.get("issuer_person_id") or ""),
            issuer_account_id=str(data.get("issuer_account_id") or ""),
            issuer_role=str(data.get("issuer_role") or "ordinary"),
            origin_platform=str(data.get("origin_platform") or ""),
            origin_bot_account_id=str(data.get("origin_bot_account_id") or ""),
            origin_chat_id=str(data.get("origin_chat_id") or ""),
            origin_chat_type=str(data.get("origin_chat_type") or "private"),
            raw_request=str(data.get("raw_request") or ""),
            clarified_spec=dict(data.get("clarified_spec") or {}),
            status=CommandStatus(str(data.get("status") or CommandStatus.RECEIVED.value)),
            identity_mode=IdentityMode(
                str(data.get("identity_mode") or IdentityMode.AS_YINYUE.value)
            ),
            nominal_name=str(data.get("nominal_name") or ""),
            target_person_ids=tuple(data.get("target_person_ids") or ()),
            action_type=str(data.get("action_type") or ""),
            execution_ids=tuple(data.get("execution_ids") or ()),
            group_queryable_after=_dt(data.get("group_queryable_after")),
            public_render_policy=PublicRenderPolicy(
                str(
                    data.get("public_render_policy")
                    or PublicRenderPolicy.AS_YINYUE.value
                )
            ),
            authorization_id=str(data.get("authorization_id") or ""),
            revoked_at=_dt(data.get("revoked_at")),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass
class AuthorizationRecord:
    authorization_id: str
    command_id: str
    raw_owner_conversation: list[dict] = field(default_factory=list)
    clarification_events: list[dict] = field(default_factory=list)
    final_confirmation: str = ""
    created_at: datetime = field(default_factory=utcnow)
    owner_only: bool = True


@dataclass
class CommandQueryLog:
    query_id: str
    requester_person_id: str
    requester_account_id: str
    queried_at: datetime
    command_ids: tuple[str, ...]
    answer: str
    context: dict = field(default_factory=dict)


class CommandLedger:
    def __init__(self, identity_service: IdentityService | None = None):
        self.identity_service = identity_service
        self._commands: dict[str, CommandRecord] = {}
        self._authorizations: dict[str, AuthorizationRecord] = {}
        self._query_logs: list[CommandQueryLog] = []

    # ---------------------------------------------------------------- write
    def record_command(
        self,
        *,
        issuer_person_id: str,
        issuer_account_id: str,
        issuer_role: str,
        origin_platform: str,
        origin_bot_account_id: str,
        origin_chat_id: str,
        origin_chat_type: str,
        raw_request: str,
        created_at: datetime | None = None,
        **kwargs: object,
    ) -> CommandRecord:
        command = CommandRecord(
            command_id=str(kwargs.pop("command_id", "") or uuid.uuid4().hex),
            created_at=created_at or utcnow(),
            issuer_person_id=str(issuer_person_id or ""),
            issuer_account_id=str(issuer_account_id or ""),
            issuer_role=str(issuer_role or "ordinary"),
            origin_platform=str(origin_platform or ""),
            origin_bot_account_id=str(origin_bot_account_id or ""),
            origin_chat_id=str(origin_chat_id or ""),
            origin_chat_type=str(origin_chat_type or "private"),
            raw_request=str(raw_request or ""),
            **kwargs,
        )
        self._commands[command.command_id] = command
        return command

    def record_authorization(
        self,
        command_id: str,
        *,
        raw_owner_conversation: Iterable[dict] | None = None,
        clarification_events: Iterable[dict] | None = None,
        final_confirmation: str = "",
        authorization_id: str = "",
    ) -> AuthorizationRecord:
        if command_id not in self._commands:
            raise KeyError(f"unknown command: {command_id}")
        record = AuthorizationRecord(
            authorization_id=authorization_id or uuid.uuid4().hex,
            command_id=command_id,
            raw_owner_conversation=list(raw_owner_conversation or ()),
            clarification_events=list(clarification_events or ()),
            final_confirmation=final_confirmation,
        )
        self._authorizations[record.authorization_id] = record
        command = self._commands[command_id]
        command.authorization_id = record.authorization_id
        return record

    def set_status(self, command_id: str, status: CommandStatus) -> CommandRecord:
        command = self._commands[command_id]
        command.status = status
        return command

    def revoke(self, command_id: str, *, at: datetime | None = None) -> CommandRecord:
        command = self._commands[command_id]
        command.status = CommandStatus.REVOKED
        command.revoked_at = at or utcnow()
        return command

    # ---------------------------------------------------------------- read
    def get(self, command_id: str) -> CommandRecord | None:
        return self._commands.get(command_id)

    def __len__(self) -> int:
        return len(self._commands)

    def load_records(self, records: Iterable[CommandRecord]) -> int:
        """Load already-recorded commands (e.g. from a shadow JSONL file)."""
        count = 0
        for record in records:
            if record.command_id not in self._commands:
                self._commands[record.command_id] = record
                count += 1
        return count

    def all_commands(self) -> list[CommandRecord]:
        return list(self._commands.values())

    def authorization(self, authorization_id: str) -> AuthorizationRecord | None:
        return self._authorizations.get(authorization_id)

    def query_logs(self) -> list[CommandQueryLog]:
        return list(self._query_logs)

    def _is_group_query(self, requester: PrincipalContext, group_id: str) -> bool:
        return bool(group_id or requester.group_id or requester.is_group)

    @staticmethod
    def _sees_all_commands(requester: PrincipalContext) -> bool:
        return requester.is_owner or requester.has(Capability.VIEW_ALL_COMMANDS)

    def _membership_covers(
        self,
        requester: PrincipalContext,
        command: CommandRecord,
        *,
        group_id: str,
    ) -> bool:
        if requester.is_owner:
            return True
        if self.identity_service is None:
            return False
        return self.identity_service.is_member_at(
            command.origin_platform,
            group_id or command.origin_chat_id,
            requester.person_id,
            command.created_at,
            account_id=command.origin_bot_account_id,
        )

    def _group_query_allowed(
        self,
        requester: PrincipalContext,
        command: CommandRecord,
        *,
        group_id: str,
    ) -> bool:
        if self.identity_service is not None:
            if not self.identity_service.group_query_policy(
                command.origin_platform,
                command.origin_bot_account_id,
                group_id or command.origin_chat_id,
            ):
                raise CommandAccessDenied("group command query is disabled for this group")
            if not self._membership_covers(requester, command, group_id=group_id):
                return False
        return self._membership_covers(requester, command, group_id=group_id)

    def query_commands(
        self,
        requester: PrincipalContext,
        *,
        group_id: str = "",
        include_revoked: bool = True,
    ) -> list[CommandRecord]:
        """Return commands visible to ``requester``.

        Owner accounts see everything.  Group members see commands from their
        membership periods only.  Ordinary contacts see only their own.
        """
        effective_group = group_id or requester.group_id
        commands = self.all_commands()
        if not include_revoked:
            commands = [c for c in commands if c.status != CommandStatus.REVOKED]
        if self._sees_all_commands(requester):
            return sorted(commands, key=lambda c: c.created_at)
        if self._is_group_query(requester, effective_group):
            visible: list[CommandRecord] = []
            for command in commands:
                if command.origin_chat_type != "group":
                    continue
                if effective_group and command.origin_chat_id != effective_group:
                    continue
                if self._group_query_allowed(
                    requester, command, group_id=effective_group
                ):
                    visible.append(command)
            return sorted(visible, key=lambda c: c.created_at)
        return sorted(
            [
                command
                for command in commands
                if command.issuer_person_id
                and command.issuer_person_id == requester.person_id
            ],
            key=lambda c: c.created_at,
        )

    # ---------------------------------------------------------------- render
    @staticmethod
    def _date(value: datetime) -> str:
        """本地墙钟渲染；换算口径唯一 owner = ``temporal``（naive 输入原样）。"""
        return temporal.to_naive_local(value).strftime("%Y-%m-%d %H:%M")

    def render_command(self, command: CommandRecord, requester: PrincipalContext) -> str:
        """Render a command as natural language, never raw JSON."""
        if self._sees_all_commands(requester):
            issuer = command.issuer_account_id or command.issuer_person_id or "未知来源"
            return (
                f"{self._date(command.created_at)}，{issuer} 下达了命令："
                f"{command.raw_request}（状态：{command.status.value}）"
            )
        summary = str(
            command.metadata.get("public_summary")
            or command.clarified_spec.get("summary")
            or command.action_type
            or "一件事"
        )
        if command.public_render_policy == PublicRenderPolicy.OWNER_NAMED:
            name = command.nominal_name or str(
                command.metadata.get("issuing_account_name") or "一位群成员"
            )
            return f"{self._date(command.created_at)}，鲸鲸以 {name} 的名义做了：{summary}"
        if command.public_render_policy == PublicRenderPolicy.REDACTED:
            return f"{self._date(command.created_at)}，鲸鲸执行过一件事"
        return f"{self._date(command.created_at)}，鲸鲸自己决定做了：{summary}"

    def render_query(
        self,
        requester: PrincipalContext,
        *,
        group_id: str = "",
        include_revoked: bool = True,
    ) -> str:
        commands = self.query_commands(
            requester, group_id=group_id, include_revoked=include_revoked
        )
        answer = "\n".join(self.render_command(command, requester) for command in commands)
        self._query_logs.append(
            CommandQueryLog(
                query_id=uuid.uuid4().hex,
                requester_person_id=requester.person_id,
                requester_account_id=requester.bot_account_id,
                queried_at=utcnow(),
                command_ids=tuple(command.command_id for command in commands),
                answer=answer,
                context={"group_id": group_id or requester.group_id},
            )
        )
        return answer
