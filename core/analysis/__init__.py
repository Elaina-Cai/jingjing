"""Phase 5 Track B: analysis moved onto the terminal event stream.

The package is intentionally split into four layers:

``event_reader``
    Read-only event windows from ``TerminalEventStore``.
``projectors``
    Pure functions from events to explainable projection drafts.
``digest``
    Pure daily-digest assembly (the event-stream replacement for the old
    ``DailyDigestAgent`` prompt).
``workers``
    Cursor, idempotency, persistence and error isolation around the rules.

No module in this package writes ``conversations``, ``memories``,
``core_memories`` or the legacy account-level ``analysis`` table.
"""

from core.analysis import digest, event_reader, projectors, service, workers

__all__ = ["digest", "event_reader", "projectors", "service", "workers"]
