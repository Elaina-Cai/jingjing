"""Event-stream model for the Phase 0 terminal prototype.

Everything that happens is an event: messages, commands, replies, actions,
tool calls, emotions and owner-visible chain-of-thought.  The event log is
append-only and carries both origin and visibility metadata.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from core.contracts.events import observation_event_id
from core.terminal.principal import VisibilityScope
from safelog import warn


class EventType(StrEnum):
    MESSAGE = "message"
    QUESTION = "question"
    REQUEST = "request"
    COMMAND = "command"
    REPLY = "reply"
    ACTION = "action"
    TOOL_CALL = "tool_call"
    THOUGHT = "thought"
    EMOTION = "emotion"
    MEMORY_WRITE = "memory_write"
    TASK_UPDATE = "task_update"
    SYSTEM = "system"
    REVOKE = "revoke"
    # WorkItem 事件契约（B6，docs/任务模型.md「事件契约」）：必须注册在这里，
    # 否则 event_store_db 读回时 EventType(...) 抛 ValueError → 静默降级成
    # message（事件类型丢失）。payload 字段见文档。
    WORK_ITEM_CREATED = "work_item_created"
    WORK_ITEM_RUN_STARTED = "work_item_run_started"
    WORK_ITEM_RUN_FINISHED = "work_item_run_finished"
    WORK_ITEM_CANCELLED = "work_item_cancelled"
    WORK_ITEM_RESCHEDULED = "work_item_rescheduled"


HUMAN_SPEECH_TYPES = frozenset(
    {
        EventType.MESSAGE,
        EventType.QUESTION,
        EventType.REQUEST,
        EventType.COMMAND,
    }
)
HUMAN_SPEECH_VALUES = frozenset(str(item) for item in HUMAN_SPEECH_TYPES)


class Sensitivity(StrEnum):
    NORMAL = "normal"
    SENSITIVE = "sensitive"
    OWNER_CHAIN_OF_THOUGHT = "owner_chain_of_thought"


def localnow() -> datetime:
    """事件时间的默认值：**本地墙钟、naive**（与采集侧一致）。

    2026-09-22 修：此前默认 UTC，导致她自己的 thought/reply/action 比入站消息
    早 8 小时（全库 459 条），窗口按"今天/小时"分桶与排序全乱。
    """
    return datetime.now()


def utcnow() -> datetime:
    """保留旧名字，语义已统一成本地墙钟（不要再用于新代码）。"""
    return localnow()


@dataclass
class Event:
    event_id: str
    occurred_at: datetime
    sequence: int = 0
    platform: str = ""
    bot_account_id: str = ""
    chat_id: str = ""
    chat_type: str = ""
    actor_person_id: str = ""
    actor_account_id: str = ""
    recipient_person_ids: tuple[str, ...] = ()
    event_type: EventType = EventType.MESSAGE
    content: str = ""
    action_payload: dict[str, Any] = field(default_factory=dict)
    command_id: str = ""
    authorization_id: str = ""
    parent_event_id: str = ""
    # 回合并集键（A1-c）：一条入站消息进入事件流时生成一个 turn_id，同一回合的
    # message / thought / action / reply 共用它；批次合并时取主消息的那个。
    turn_id: str = ""
    link_revision: int = 0
    visibility_scope: frozenset[VisibilityScope] = field(
        default_factory=lambda: frozenset({VisibilityScope.PUBLIC})
    )
    sensitivity: Sensitivity = Sensitivity.NORMAL
    metadata: dict[str, Any] = field(default_factory=dict)
    external_msg_id: str = ""

    @classmethod
    def new(
        cls,
        *,
        event_type: EventType,
        occurred_at: datetime | None = None,
        visibility_scope: frozenset[VisibilityScope] | None = None,
        **kwargs: Any,
    ) -> "Event":
        event_id = kwargs.pop("event_id", "") or observation_event_id(
            platform=str(kwargs.get("platform") or ""),
            account_id=str(kwargs.get("bot_account_id") or ""),
            chat_id=str(kwargs.get("chat_id") or ""),
            external_id=str(kwargs.get("external_msg_id") or ""),
        )
        return cls(
            event_id=event_id or uuid.uuid4().hex,
            occurred_at=occurred_at or localnow(),
            event_type=event_type,
            visibility_scope=visibility_scope or frozenset({VisibilityScope.PUBLIC}),
            **kwargs,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "occurred_at": self.occurred_at.isoformat(),
            "sequence": self.sequence,
            "platform": self.platform,
            "bot_account_id": self.bot_account_id,
            "chat_id": self.chat_id,
            "chat_type": self.chat_type,
            "actor_person_id": self.actor_person_id,
            "actor_account_id": self.actor_account_id,
            "recipient_person_ids": list(self.recipient_person_ids),
            "event_type": self.event_type.value,
            "content": self.content,
            "action_payload": self.action_payload,
            "command_id": self.command_id,
            "authorization_id": self.authorization_id,
            "parent_event_id": self.parent_event_id,
            "turn_id": self.turn_id,
            "external_msg_id": self.external_msg_id,
            "link_revision": self.link_revision,
            "visibility_scope": sorted(scope.value for scope in self.visibility_scope),
            "sensitivity": self.sensitivity.value,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Event":
        occurred_at = data.get("occurred_at")
        if isinstance(occurred_at, str):
            try:
                occurred_at = datetime.fromisoformat(occurred_at)
            except ValueError:
                warn("core/terminal/event_model.py:140 ValueError", err=repr(__import__("sys").exc_info()[1]))
                occurred_at = localnow()
        return cls(
            event_id=str(data.get("event_id") or uuid.uuid4().hex),
            occurred_at=occurred_at or localnow(),
            sequence=int(data.get("sequence") or 0),
            platform=str(data.get("platform") or ""),
            bot_account_id=str(data.get("bot_account_id") or ""),
            chat_id=str(data.get("chat_id") or ""),
            chat_type=str(data.get("chat_type") or ""),
            actor_person_id=str(data.get("actor_person_id") or ""),
            actor_account_id=str(data.get("actor_account_id") or ""),
            recipient_person_ids=tuple(data.get("recipient_person_ids") or ()),
            event_type=EventType(str(data.get("event_type") or EventType.MESSAGE.value)),
            content=str(data.get("content") or ""),
            action_payload=dict(data.get("action_payload") or {}),
            command_id=str(data.get("command_id") or ""),
            authorization_id=str(data.get("authorization_id") or ""),
            parent_event_id=str(data.get("parent_event_id") or ""),
            turn_id=str(data.get("turn_id") or ""),
            external_msg_id=str(data.get("external_msg_id") or ""),
            link_revision=int(data.get("link_revision") or 0),
            visibility_scope=frozenset(
                VisibilityScope(str(scope))
                for scope in (data.get("visibility_scope") or [])
            ),
            sensitivity=Sensitivity(
                str(data.get("sensitivity") or Sensitivity.NORMAL.value)
            ),
            metadata=dict(data.get("metadata") or {}),
        )


def is_owner_thought(event: Event) -> bool:
    return (
        event.event_type == EventType.THOUGHT
        and event.sensitivity == Sensitivity.OWNER_CHAIN_OF_THOUGHT
    )
