"""Event-window reader for the Phase 5 Track B analysis workers.

The reader is deliberately small and deterministic.  Track B moves analysis
off the legacy ``MessageClassifier -> Orchestrator -> Agent`` chain and onto
the append-only ``terminal_events`` fact stream.  These rules are load-bearing:

* read only through ``TerminalEventStore.since_sequence`` (or
  ``query_visible`` when a caller already has a principal);
* group by ``bot_account_id`` / ``platform`` / ``chat_id`` while preserving
  every ``event_id``;
* owner chain-of-thought events never enter an analysis projection;
* every resulting projection must carry ``source_event_ids``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from core import temporal
from core.terminal.event_model import Event, EventType, Sensitivity
from safelog import warn


# These are the event types the old analysis chain actually reasoned about.
ANALYSIS_EVENT_TYPES: frozenset[EventType] = frozenset(
    {
        EventType.MESSAGE,
        EventType.QUESTION,
        EventType.REQUEST,
        EventType.COMMAND,
        EventType.REPLY,
    }
)


def aware(value: datetime | None) -> datetime:
    """归一为应用时钟（naive 本地墙钟）；换算口径唯一 owner = ``temporal``。

    名字里的 "aware" 是历史包袱：本项目**从不**用 aware datetime 做内部比较
    （旧 docstring 写「Return an aware UTC datetime」是错的，行为一直是转本地
    再去掉 tzinfo）。改名会牵连调用方，这里只把口径写实。
    """
    if value is None:
        return datetime.now()
    if value.tzinfo is None:
        return value
    return temporal.to_naive_local(value)


def is_analysis_event(event: Any) -> bool:
    """True when one event may feed an analysis projection."""
    if not isinstance(event, Event):
        return False
    if not event.event_id:
        return False
    if event.sensitivity == Sensitivity.OWNER_CHAIN_OF_THOUGHT:
        return False
    return event.event_type in ANALYSIS_EVENT_TYPES


def latest_sequence(events: Iterable[Event]) -> int:
    """Largest known event sequence, or 0 for an empty batch."""
    maximum = 0
    for event in events:
        if isinstance(event, Event):
            maximum = max(maximum, int(event.sequence or 0))
    return maximum


def _event_row_id(event_store: Any, event: Event) -> int:
    """``terminal_events.id`` for one event（复合游标后半键，ENG-23）。

    ``Event`` 不带行 id，所以回查一次主键；读失败返回 0 = 调用方退回「只按号推进」。
    """
    reader = getattr(event_store, "sequence_row_id", None)
    if not callable(reader):
        return 0
    try:
        return max(0, int(reader(str(event.event_id or "")) or 0))
    except Exception:  # noqa: BLE001 - 游标推进不能因回查失败而中断
        warn("core/analysis/event_reader.py:81 Exception", err=repr(__import__("sys").exc_info()[1]))
        return 0


def latest_cursor_pair(
    events: Iterable[Event], event_store: Any = None
) -> tuple[int, int]:
    """``(sequence, id)`` of the LAST event in the batch, ``(0, 0)`` when empty.

    游标必须落在批内**最后一条**的 ``(sequence, id)`` 上，不能是 ``max(sequence)``：
    同号行的后继要靠 ``sequence = :seq AND id > :id`` 才能读到（ENG-23）。最后一条
    的 event_id 需要回库换行 id，所以这里要 ``event_store``。
    """
    last: Event | None = None
    for event in events:
        if isinstance(event, Event):
            last = event
    if last is None:
        return (0, 0)
    row_id = _event_row_id(event_store, last) if event_store is not None else 0
    if row_id:
        return (int(last.sequence or 0), row_id)
    # 回查不到行 id：不能写 ``(S, 0)`` —— 读侧 ``sequence = S AND id > 0`` 会把这
    # 批同号行再读一遍，batch 小于同号行数时是死循环；跳到 ``S + 1``。
    return (int(last.sequence or 0) + 1, 0)


@dataclass(frozen=True)
class ConversationWindow:
    """All analyzable events for one account + conversation pair."""

    platform: str
    bot_account_id: str
    chat_id: str
    chat_type: str
    events: tuple[Event, ...]

    @property
    def source_event_ids(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for event in self.events:
            if event.event_id:
                seen.setdefault(event.event_id, None)
        return tuple(seen)

    @property
    def max_sequence(self) -> int:
        return latest_sequence(self.events)

    @property
    def occurred_at(self) -> datetime:
        if not self.events:
            return datetime.now()
        return max(aware(event.occurred_at) for event in self.events)


class AnalysisEventReader:
    """Read bounded event windows from the formal SQLite event store."""

    def __init__(self, event_store: Any, *, batch_limit: int = 200):
        self.event_store = event_store
        self.batch_limit = max(1, int(batch_limit or 200))

    def read_events(
        self, cursor: int = 0, *, last_id: int = 0, limit: int | None = None
    ) -> list[Event]:
        """Read one raw batch after ``(cursor, last_id)``; skipped rows stay in it.

        Keeping skipped rows in the batch matters: the worker still needs to
        advance its cursor past a THOUGHT event even though no projection is
        written for it.

        ``last_id`` is the second half of the cursor (ENG-23).  Leaving it at 0
        keeps the old "sequence only" behaviour for single-value callers.
        """
        if self.event_store is None:
            return []
        size = max(1, int(limit or self.batch_limit))
        try:
            return list(
                self.event_store.since_sequence(
                    int(cursor or 0), limit=size, last_id=int(last_id or 0)
                )
            )
        except Exception:
            warn("core/analysis/event_reader.py:165 Exception", err=repr(__import__("sys").exc_info()[1]))
            return []

    def read_all(
        self,
        *,
        batch_size: int = 500,
        max_batches: int = 200,
    ) -> list[Event]:
        """Page through the stream up to a safety bound (best-effort)."""
        cursor, last_id = 0, 0
        events: list[Event] = []
        for _ in range(max(1, int(max_batches))):
            batch = self.read_events(cursor, last_id=last_id, limit=batch_size)
            if not batch:
                break
            events.extend(batch)
            next_cursor, next_id = self.latest_cursor_pair(batch)
            if (next_cursor, next_id) <= (cursor, last_id):
                break
            cursor, last_id = next_cursor, next_id
            if len(batch) < max(1, int(batch_size)):
                break
        return events

    @staticmethod
    def projectable_events(events: Iterable[Event]) -> list[Event]:
        """Return the subset that may become a Track B analysis projection."""
        return [event for event in events if is_analysis_event(event)]

    @staticmethod
    def latest_sequence(events: Iterable[Event]) -> int:
        """Largest sequence in one raw batch, including skipped events."""
        return latest_sequence(events)

    def latest_cursor_pair(self, events: Iterable[Event]) -> tuple[int, int]:
        """``(sequence, id)`` of the batch's last event（ENG-23 cursor advance）."""
        return latest_cursor_pair(events, self.event_store)

    @staticmethod
    def group_conversations(
        events: Iterable[Event],
        *,
        projectable_only: bool = True,
    ) -> list[ConversationWindow]:
        """Group events by platform + bot account + chat id.

        The grouping key intentionally separates tenants: a projection made for
        ``bot_a`` must never be reused for ``bot_b`` even when the raw chat id
        happens to be identical.
        """
        buckets: dict[tuple[str, str, str], list[Event]] = {}
        for event in events:
            if projectable_only and not is_analysis_event(event):
                continue
            key = (
                str(event.platform or ""),
                str(event.bot_account_id or ""),
                str(event.chat_id or ""),
            )
            buckets.setdefault(key, []).append(event)

        windows: list[ConversationWindow] = []
        for (platform, account_id, chat_id), bucket in buckets.items():
            ordered = tuple(
                sorted(
                    bucket,
                    key=lambda event: (
                        int(event.sequence or 0),
                        aware(event.occurred_at),
                        str(event.event_id or ""),
                    ),
                )
            )
            chat_type = str(
                next(
                    (
                        event.chat_type
                        for event in reversed(ordered)
                        if event.chat_type
                    ),
                    "",
                )
                or ""
            )
            windows.append(
                ConversationWindow(
                    platform=platform,
                    bot_account_id=account_id,
                    chat_id=chat_id,
                    chat_type=chat_type,
                    events=ordered,
                )
            )
        windows.sort(
            key=lambda window: (
                window.platform,
                window.bot_account_id,
                window.chat_id,
            )
        )
        return windows


def group_conversations(
    events: Iterable[Event],
    *,
    projectable_only: bool = True,
) -> list[ConversationWindow]:
    """Module-level convenience wrapper around the reader helper."""
    return AnalysisEventReader.group_conversations(
        events, projectable_only=projectable_only
    )


def projectable_events(events: Iterable[Event]) -> list[Event]:
    """Module-level wrapper used by pure projectors and tests."""
    return AnalysisEventReader.projectable_events(events)

