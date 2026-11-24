from __future__ import annotations

import asyncio
import structlog
from collections import deque
from enum import Enum, auto
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable
from safelog import warn

log = structlog.get_logger()


class EventType(Enum):
    NEW_MESSAGES_SYNCED = "new_messages_synced"
    INBOUND_MESSAGE = "inbound_message"
    ANALYSIS_COMPLETED = "analysis_completed"
    DAILY_DIGEST_READY = "daily_digest_ready"
    DAILY_DIGEST_TRIGGER = "daily_digest_trigger"
    KEY_EXPIRED = "key_expired"
    BUDGET_EXCEEDED = "budget_exceeded"
    PROACTIVE_TRIGGER = "proactive_trigger"
    MEMORY_CREATED = "memory_created"
    PERSONA_MESSAGE = "persona_message"
    SYNC_STARTED = "sync_started"
    SYNC_FAILED = "sync_failed"
    DREAM_TRIGGER = "dream_trigger"


@dataclass
class Event:
    type: EventType
    timestamp: datetime = field(default_factory=datetime.now)
    payload: dict[str, Any] = field(default_factory=dict)
    source: str = ""
    priority: int = 5
    platform: str = ""
    account_id: str = ""


@dataclass
class Subscription:
    handler: Callable
    event_type: EventType
    priority: int = 5
    filter_fn: Callable[[Event], bool] | None = None
    is_async: bool = False


@dataclass
class EventRecord:
    event: Event
    dispatched_at: datetime = field(default_factory=datetime.now)
    handler_errors: list[tuple[str, str]] = field(default_factory=list)


class EventBus:
    _subscriptions: dict[str, Subscription] = {}
    _next_sub_id: int = 0
    _pending_tasks: set = set()
    _history: deque[EventRecord] = deque(maxlen=200)
    _history_enabled: bool = True

    @classmethod
    def subscribe(
        cls,
        event_type: EventType,
        handler: Callable,
        priority: int = 5,
        filter_fn: Callable[[Event], bool] | None = None,
    ) -> str:
        sub_id = f"sub_{cls._next_sub_id}"
        cls._next_sub_id += 1

        is_async = asyncio.iscoroutinefunction(handler)

        cls._subscriptions[sub_id] = Subscription(
            handler=handler,
            event_type=event_type,
            priority=priority,
            filter_fn=filter_fn,
            is_async=is_async,
        )
        return sub_id

    @classmethod
    def unsubscribe(cls, sub_id: str):
        cls._subscriptions.pop(sub_id, None)

    @classmethod
    def publish(cls, event: Event):
        sorted_subs = sorted(
            [s for s in cls._subscriptions.values() if s.event_type == event.type],
            key=lambda s: s.priority,
        )

        record = EventRecord(event=event) if cls._history_enabled else None

        for sub in sorted_subs:
            if sub.filter_fn and not sub.filter_fn(event):
                continue
            if sub.is_async:
                try:
                    loop = asyncio.get_running_loop()
                    task = loop.create_task(sub.handler(event))
                    cls._pending_tasks.add(task)
                    task.add_done_callback(cls._pending_tasks.discard)
                except RuntimeError:
                    warn("core/infra/event_bus.py:108 RuntimeError", err=repr(__import__("sys").exc_info()[1]))
                    asyncio.run(sub.handler(event))
            else:
                try:
                    sub.handler(event)
                except Exception as e:
                    log.error("event_handler_error", handler=sub.handler.__name__, error=str(e))
                    if record is not None:
                        record.handler_errors.append((sub.handler.__name__, str(e)))

        if record is not None:
            cls._history.append(record)

    @classmethod
    async def await_pending(cls, timeout: float = 30.0):
        if not cls._pending_tasks:
            return
        done, pending = await asyncio.wait(cls._pending_tasks, timeout=timeout)
        if pending:
            log.debug("event_pending_timeout", pending=len(pending))

    @classmethod
    def publish_sync(cls, event: Event):
        cls.publish(event)

    @classmethod
    def subscribers(cls, event_type: EventType) -> list[dict]:
        return [
            {
                "id": sid,
                "handler": sub.handler.__name__,
                "priority": sub.priority,
                "is_async": sub.is_async,
                "has_filter": sub.filter_fn is not None,
            }
            for sid, sub in cls._subscriptions.items()
            if sub.event_type == event_type
        ]

    @classmethod
    def history(cls, event_type: EventType | None = None, limit: int = 50) -> list[EventRecord]:
        records = list(cls._history)
        if event_type is not None:
            records = [r for r in records if r.event.type == event_type]
        return records[-limit:]

    @classmethod
    def enable_history(cls, enabled: bool = True, maxlen: int = 200):
        cls._history_enabled = enabled
        if enabled:
            cls._history = deque(cls._history, maxlen=maxlen)

    @classmethod
    def stats(cls) -> dict[str, Any]:
        by_type: dict[str, int] = {}
        for record in cls._history:
            by_type[record.event.type.value] = by_type.get(record.event.type.value, 0) + 1
        error_count = sum(len(r.handler_errors) for r in cls._history)
        return {
            "total_events": len(cls._history),
            "by_type": by_type,
            "handler_errors": error_count,
            "active_subscriptions": len(cls._subscriptions),
            "pending_async_tasks": len(cls._pending_tasks),
        }

    @classmethod
    def clear(cls):
        cls._subscriptions.clear()
        cls._pending_tasks.clear()
        cls._history.clear()


def on(
    event_type: EventType,
    priority: int = 5,
    filter_fn: Callable[[Event], bool] | None = None,
):
    def decorator(func):
        EventBus.subscribe(event_type, func, priority=priority, filter_fn=filter_fn)
        return func
    return decorator
