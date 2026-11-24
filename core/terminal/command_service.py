"""Phase 2 DB-backed command service.

``CommandLedger`` remains the pure in-memory policy/query engine used by the
Phase 0 golden tests.  ``CommandService`` adds a persistence projection
(``command_records`` / ``authorization_records`` / ``command_query_logs``)
without changing any of the visibility/render semantics.

All writes are best-effort from the caller's perspective: the message/event
recording path already wraps them in try/except so a DB failure never blocks a
reply.  Query APIs return the same objects as ``CommandLedger``.
"""

from __future__ import annotations

from typing import Any, Iterable

import structlog

from core.terminal.command_ledger import (
    AuthorizationRecord,
    CommandLedger,
    CommandQueryLog,
    CommandRecord,
    CommandStatus,
)
from core.terminal.identity_service import IdentityService
from core.terminal.shadow import link_event_best_effort
from safelog import warn

log = structlog.get_logger()


class CommandService(CommandLedger):
    """Command ledger with an optional SQLite persistence projection."""

    def __init__(
        self,
        identity_service: IdentityService | None = None,
        *,
        repository: Any = None,
    ):
        super().__init__(identity_service)
        self.repository = repository
        self._link_store: Any | None = None
        if repository is not None:
            self._load_from_repository()

    def _load_from_repository(self) -> None:
        for command in self.repository.list_commands():
            self._commands[command.command_id] = command
        for authorization in self.repository.list_authorizations():
            self._authorizations[authorization.authorization_id] = authorization
        self._query_logs = list(self.repository.list_query_logs())

    # ---------------------------------------------------------------- write
    def record_command(self, *, command_id: str = "", **kwargs: object) -> CommandRecord:
        command = super().record_command(command_id=command_id, **kwargs)
        self._persist_command(command)
        return command

    def load_records(self, records: Iterable[CommandRecord]) -> int:
        records = list(records)
        count = super().load_records(records)
        if self.repository is not None:
            for record in records:
                self.repository.upsert_command(record)
        return count

    def record_authorization(
        self,
        command_id: str,
        *,
        raw_owner_conversation: Iterable[dict] | None = None,
        clarification_events: Iterable[dict] | None = None,
        final_confirmation: str = "",
        authorization_id: str = "",
    ) -> AuthorizationRecord:
        record = super().record_authorization(
            command_id,
            raw_owner_conversation=raw_owner_conversation,
            clarification_events=clarification_events,
            final_confirmation=final_confirmation,
            authorization_id=authorization_id,
        )
        if self.repository is not None:
            self.repository.upsert_authorization(record)
            command = self._commands.get(command_id)
            if command is not None:
                self.repository.upsert_command(command)
        # A1-a：授权是在消息事件 append 之后才建立的，回填到 command_id 指向的
        # 那条事件行；事件行还没写（或 store 不支持窄更新）时静默跳过。
        if record.authorization_id:
            self._link_events_for_command(
                command_id, authorization_id=record.authorization_id
            )
        return record

    def record_execution(
        self,
        command_id: str,
        *,
        execution_event_ids: Iterable[str],
        status: CommandStatus | None = None,
    ) -> CommandRecord:
        """登记一次执行：去重追加 ``execution_ids`` 并回填执行事件行。

        A1-a：``execution_ids_json`` 此前从未被赋值——命令能建、执行却无处可查。
        这里把执行事件 id 去重追加（保持首次登记顺序，重复登记幂等），可选推进
        ``status``；同时把 ``command_id`` 回填到这些执行事件行（action/tool_call），
        让事件流能按 command_id join 出「谁下达 → 执行到哪」。
        """
        command = self._commands.get(command_id)
        if command is None:
            raise KeyError(f"unknown command: {command_id}")
        merged = tuple(
            dict.fromkeys(
                [
                    *command.execution_ids,
                    *(str(event_id) for event_id in (execution_event_ids or ()) if str(event_id)),
                ]
            )
        )
        command.execution_ids = merged
        if status is not None:
            command.status = status
        self._persist_command(command)
        for event_id in merged:
            link_event_best_effort(
                self._event_store(), event_id, command_id=command_id
            )
        return command

    def set_status(self, command_id: str, status: CommandStatus) -> CommandRecord:
        command = super().set_status(command_id, status)
        self._persist_command(command)
        return command

    def revoke(self, command_id: str, *, at=None) -> CommandRecord:
        command = super().revoke(command_id, at=at)
        self._persist_command(command)
        return command

    def _persist_command(self, command: CommandRecord) -> None:
        if self.repository is not None:
            self.repository.upsert_command(command)

    # ---------------------------------------------------------------- links
    def _event_store(self) -> Any | None:
        """事件流 store：复用命令投影的 meta engine；未接仓库时返回 None。

        刻意**不用** ``event_runtime.get_terminal_event_store(force=True)``：那个访问器
        在缓存为空时会走 ``core.bootstrap.init_singletons()``，把整个应用（含 meta 库与
        各 account 库的迁移）拉起来。2026-09-29 实测：``tests/test_command_service_db.py``
        的 roundtrip 用例只调 ``record_authorization``，就会把 pending 迁移落到真实库。
        命令投影的 engine 就是 meta engine，链接回填读同一个库，不需要 bootstrap。
        （回归用例：test_event_chain_links.test_authorization_link_does_not_bootstrap）
        """
        if self._link_store is None:
            engine = getattr(self.repository, "engine", None)
            if engine is None:
                return None
            try:
                from core.terminal.event_store_db import TerminalEventStore

                self._link_store = TerminalEventStore(engine)
            except Exception:  # noqa: BLE001 - 回填是 best-effort
                warn("core/terminal/command_service.py:165 Exception", err=repr(__import__("sys").exc_info()[1]))
                return None
        return self._link_store

    def _link_events_for_command(self, command_id: str, **fields: str) -> int:
        """把链接字段回填到事件流里 ``command_id`` 指向该命令的行。"""
        store = self._event_store()
        query = getattr(store, "query", None)
        if store is None or query is None:
            return 0
        try:
            events = list(query(command_id=str(command_id)))
        except Exception as exc:  # noqa: BLE001 - 回填是 best-effort
            log.warning(
                "terminal_command_events_query_failed",
                command_id=str(command_id)[:64],
                error=str(exc)[:200],
            )
            return 0
        linked = 0
        for event in events:
            if link_event_best_effort(
                store, getattr(event, "event_id", ""), **fields
            ):
                linked += 1
        return linked

    # ---------------------------------------------------------------- read
    def render_query(
        self,
        requester,
        *,
        group_id: str = "",
        include_revoked: bool = True,
    ) -> str:
        answer = super().render_query(
            requester, group_id=group_id, include_revoked=include_revoked
        )
        if self.repository is not None and self._query_logs:
            self.repository.add_query_log(self._query_logs[-1])
        return answer

    def reload(self) -> int:
        """Reload the in-memory view from the repository; returns command count."""
        if self.repository is None:
            return len(self._commands)
        self._commands.clear()
        self._authorizations.clear()
        self._query_logs.clear()
        self._load_from_repository()
        return len(self._commands)


class TerminalCommandService(CommandService):
    """Alias used by runtime/bootstrap wiring for readability."""


def command_recorder_for_service(
    service: CommandService,
    identity_service: IdentityService | None = None,
    *,
    path: str | None = None,
):
    """Build a recorder that writes through ``CommandService``.

    Reuses the Phase 0 classification adapter so command recognition stays in
    one place; ``path=None`` disables the JSONL side-write.
    """
    from core.terminal.command_shadow import ShadowCommandRecorder

    return ShadowCommandRecorder(
        identity_service or service.identity_service,
        path=path,
        ledger=service,
        write_jsonl=False,
    )
