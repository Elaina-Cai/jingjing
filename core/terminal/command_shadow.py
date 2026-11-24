"""Shadow CommandRecord recorder for Phase 0.

Converts incoming question/request/command messages into CommandRecord rows and
appends them to a JSONL file.  It is read-only with respect to existing data
and never executes a command.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core import temporal
from core.terminal.command_intent import IntentResult, classify_intent
from core.terminal.command_ledger import (
    CommandLedger,
    CommandRecord,
    PublicRenderPolicy,
)
from core.terminal.event_model import EventType
from core.terminal.identity_service import IdentityService
from core.terminal.principal import PrincipalContext
from safelog import warn

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_COMMAND_SHADOW_PATH = ROOT / "logs" / "terminal_commands_shadow.jsonl"

_RECORD_TYPES = {
    EventType.COMMAND,
    EventType.REQUEST,
    EventType.QUESTION,
}

_lock = threading.RLock()
_recorder: "ShadowCommandRecorder | None" = None


def _parse_time(value: Any) -> datetime:
    """ datetime / ISO 文本 → 应用时钟（naive 本地墙钟），解析不出来给 now()。

    ``core/terminal/shadow.parse_time`` 曾是同一段代码的第二份复制件；两者现在
    都走 owner ``temporal.parse_naive_local``。
    """
    return temporal.parse_naive_local(value) or datetime.now()


class ShadowCommandRecorder:
    def __init__(
        self,
        identity_service: IdentityService | None = None,
        *,
        path: str | Path | None = None,
        ledger: CommandLedger | None = None,
        write_jsonl: bool = True,
    ):
        self.identity_service = identity_service
        # NOTE: an empty CommandLedger is falsy because it defines __len__, so
        # ``ledger or CommandLedger(...)`` would silently discard a DB-backed
        # CommandService before its first command is loaded.  Use an explicit
        # None check.
        self.ledger = ledger if ledger is not None else CommandLedger(identity_service)
        if write_jsonl:
            self.path = Path(path) if path else DEFAULT_COMMAND_SHADOW_PATH
        else:
            self.path = None
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def record_from_message(
        self,
        message: dict,
        *,
        platform: str,
        account_id: str,
        occurred_at: datetime | None = None,
        principal: PrincipalContext | None = None,
        intent: IntentResult | None = None,
    ) -> CommandRecord | None:
        if not isinstance(message, dict):
            return None
        content = message.get("content") or ""
        if isinstance(content, bytes):
            content = content.decode("utf-8", errors="replace")
        content = str(content).strip()
        if not content:
            return None
        resolved_intent = intent or classify_intent(content)
        if resolved_intent.event_type not in _RECORD_TYPES:
            return None

        chat_id = str(message.get("chat_id") or "")
        chat_type = str(message.get("chat_type") or "private")
        sender_id = str(message.get("sender_id") or message.get("sender_wxid") or "")
        sender_name = str(message.get("sender_name") or "")
        if principal is None and self.identity_service is not None:
            try:
                principal = self.identity_service.resolve_principal(
                    platform=platform,
                    bot_account_id=account_id,
                    chat_id=chat_id,
                    chat_type=chat_type,
                    sender_id=sender_id,
                    sender_name=sender_name,
                    occurred_at=occurred_at,
                    group_id=chat_id if chat_type == "group" else "",
                )
            except Exception:
                warn("core/terminal/command_shadow.py:110 Exception", err=repr(__import__("sys").exc_info()[1]))
                principal = None

        if principal is not None:
            issuer_person_id = principal.person_id
            issuer_role = (
                "master"
                if principal.is_owner
                else ("group_member" if chat_type == "group" else "ordinary")
            )
        else:
            issuer_person_id = sender_id
            issuer_role = "group_member" if chat_type == "group" else "ordinary"

        metadata = {
            "msg_id": str(message.get("msg_id") or message.get("id") or ""),
            "sender_name": sender_name,
            "intent": resolved_intent.event_type.value,
            "intent_reasons": list(resolved_intent.reasons),
            "group_id": chat_id if chat_type == "group" else "",
            "source": str(message.get("source") or platform),
            "principal": principal.as_dict() if principal is not None else {},
        }
        record = self.ledger.record_command(
            created_at=occurred_at or _parse_time(message.get("created_at")),
            issuer_person_id=issuer_person_id,
            issuer_account_id=sender_id,
            issuer_role=issuer_role,
            origin_platform=platform,
            origin_bot_account_id=account_id,
            origin_chat_id=chat_id,
            origin_chat_type=chat_type,
            raw_request=content,
            action_type=resolved_intent.event_type.value,
            public_render_policy=PublicRenderPolicy.AS_YINYUE,
            metadata=metadata,
        )
        self._append(record)
        return record

    def _append(self, record: CommandRecord) -> None:
        if self.path is None:
            return
        try:
            with self._lock:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record.as_dict(), ensure_ascii=False))
                    handle.write("\n")
        except Exception:
            warn("core/terminal/command_shadow.py:158 Exception", err=repr(__import__("sys").exc_info()[1]))

    def all(self) -> list[CommandRecord]:
        return self.ledger.all_commands()

    def count(self) -> int:
        return len(self.ledger)

    @staticmethod
    def load_jsonl(path: str | Path) -> list[CommandRecord]:
        file_path = Path(path)
        if not file_path.exists():
            return []
        out: list[CommandRecord] = []
        for line in file_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(CommandRecord.from_dict(json.loads(line)))
            except Exception:
                warn("core/terminal/command_shadow.py:179 Exception", err=repr(__import__("sys").exc_info()[1]))
                continue
        return out


def get_shadow_command_recorder(
    identity_service: IdentityService | None = None,
    *,
    path: str | Path | None = None,
) -> ShadowCommandRecorder:
    global _recorder
    with _lock:
        if _recorder is None:
            _recorder = ShadowCommandRecorder(identity_service, path=path)
        elif identity_service is not None and _recorder.identity_service is None:
            _recorder.identity_service = identity_service
            _recorder.ledger.identity_service = identity_service
        return _recorder


def reset_shadow_command_recorder() -> None:
    global _recorder
    with _lock:
        _recorder = None
