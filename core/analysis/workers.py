"""Event-sourced analysis workers for Phase 5 Track B.

Track B5 removed the legacy classifier/orchestrator chain, so
``scheduler.main._run_analysis_pipeline`` now always calls
:func:`run_event_analysis_worker`.

* the workers are idempotent and isolated from the live reply path;
* a cursor is advanced only after every draft in the batch was written, so a
  transient failure retries the same window instead of losing provenance.
"""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any, Iterable

import structlog

from core.analysis.digest import build_daily_digest
from core.analysis.event_reader import AnalysisEventReader
from core.analysis.projectors import (
    AnalysisProjectionDraft,
    project_events,
    project_group_summaries,
    project_message_analyses,
)
from safelog import warn

log = structlog.get_logger()

ANALYSIS_CURSOR_KEY = "analysis_from_events"


class MessageAnalysisWorker:
    """Pure projector for private-chat importance/topic/attention drafts."""

    name = "message_analysis"

    def project(
        self, events: Iterable[Any]
    ) -> list[AnalysisProjectionDraft]:
        return project_message_analyses(events)

    def run(self, events: Iterable[Any]) -> dict[str, Any]:
        drafts = self.project(events)
        return {"projector": self.name, "projected": len(drafts)}


class GroupSummaryWorker:
    """Pure projector for group summary / topic / relation-clue drafts."""

    name = "group_summary"

    def project(
        self, events: Iterable[Any]
    ) -> list[AnalysisProjectionDraft]:
        return project_group_summaries(events)

    def run(self, events: Iterable[Any]) -> dict[str, Any]:
        drafts = self.project(events)
        return {"projector": self.name, "projected": len(drafts)}


class AnalysisFromEventsWorker:
    """Incremental event -> analysis projection worker.

    ``repository`` is any object implementing ``get_cursor`` / ``set_cursor`` /
    ``upsert`` (the production adapter is
    :class:`db.repo_analysis_projections.AnalysisProjectionRepository`).
    """

    def __init__(self, event_store: Any, repository: Any, *, cursor_key: str = ANALYSIS_CURSOR_KEY):
        self.event_store = event_store
        self.repository = repository
        self.cursor_key = str(cursor_key or ANALYSIS_CURSOR_KEY)
        self.reader = AnalysisEventReader(event_store)

    def run(self, *, limit: int = 200) -> dict[str, Any]:
        if self.event_store is None or self.repository is None:
            return {
                "scanned": 0,
                "projected": 0,
                "written": 0,
                "failed": 0,
                "cursor": 0,
                "error": "event store or analysis repository unavailable",
            }

        try:
            cursor, cursor_id = self._read_cursor()
        except Exception as exc:  # noqa: BLE001 - worker must stay best-effort
            warn("core/analysis/workers.py:91 Exception", err=repr(exc))
            return {
                "scanned": 0,
                "projected": 0,
                "written": 0,
                "failed": 0,
                "cursor": 0,
                "error": f"cursor_read_failed: {str(exc)[:140]}",
            }

        batch = self.reader.read_events(cursor, last_id=cursor_id, limit=limit)
        if not batch:
            return {
                "scanned": 0,
                "projectable": 0,
                "projected": 0,
                "written": 0,
                "failed": 0,
                "cursor": cursor,
            }

        projectable = self.reader.projectable_events(batch)
        skipped_sensitive = len(batch) - len(projectable)
        try:
            drafts = project_events(projectable)
        except Exception as exc:  # noqa: BLE001 - cursor stays put for retry
            warn("core/analysis/workers.py:116 Exception", err=repr(exc))
            return {
                "scanned": len(batch),
                "projectable": len(projectable),
                "projected": 0,
                "written": 0,
                "failed": 0,
                "cursor": cursor,
                "error": f"projector_failed: {str(exc)[:140]}",
            }

        # Defensive contract: a projection without a source event must never be
        # persisted, even if a future projector bug tries to emit one.
        valid_drafts = [draft for draft in drafts if draft.source_event_ids]
        dropped_without_source = len(drafts) - len(valid_drafts)

        written = 0
        failed = 0
        for draft in valid_drafts:
            try:
                self.repository.upsert(draft)
                written += 1
            except Exception as exc:  # noqa: BLE001 - isolate one draft
                failed += 1
                log.warning(
                    "analysis_projection_upsert_failed",
                    projection_id=draft.projection_id,
                    error=str(exc)[:160],
                )

        # ENG-23：游标落在批内最后一条的 (sequence, id) 上，不是 max(sequence)。
        next_cursor, next_cursor_id = self.reader.latest_cursor_pair(batch)
        if failed:
            # Retry the same cursor.  Existing projections use deterministic
            # ids, so the retry overwrites rather than duplicates.
            return {
                "scanned": len(batch),
                "projectable": len(projectable),
                "projected": len(valid_drafts),
                "written": written,
                "failed": failed,
                "cursor": cursor,
                "error": "retry_cursor_after_write_failure",
            }

        if (next_cursor, next_cursor_id) > (cursor, cursor_id):
            self._write_cursor(next_cursor, next_cursor_id)

        return {
            "scanned": len(batch),
            "projectable": len(projectable),
            "projected": len(valid_drafts),
            "written": written,
            "failed": 0,
            "cursor": max(cursor, next_cursor),
            "cursor_pair": self._read_cursor(),
            "skipped_sensitive": skipped_sensitive,
            "dropped_without_source": dropped_without_source,
        }

    # ----------------------------------------------------------------- cursor
    def _read_cursor(self) -> tuple[int, int]:
        """当前游标 ``(last_sequence, last_id)``；读失败向上抛给 run() 兜。"""
        pair_reader = getattr(self.repository, "get_cursor_pair", None)
        if callable(pair_reader):
            sequence, last_id = pair_reader(self.cursor_key)
            return (max(0, int(sequence or 0)), max(0, int(last_id or 0)))
        return (max(0, int(self.repository.get_cursor(self.cursor_key) or 0)), 0)

    def _write_cursor(self, sequence: int, last_id: int) -> None:
        pair_writer = getattr(self.repository, "set_cursor_pair", None)
        if callable(pair_writer):
            pair_writer(self.cursor_key, int(sequence or 0), int(last_id or 0))
            return
        self.repository.set_cursor(self.cursor_key, int(sequence or 0))


def run_analysis_worker_once(
    event_store: Any,
    repository: Any,
    *,
    limit: int = 200,
) -> dict[str, Any]:
    """Synchronous entry point used by tests and the async wrapper."""
    return AnalysisFromEventsWorker(event_store, repository).run(limit=limit)


def _runtime_stores() -> tuple[Any, Any, str]:
    try:
        from core.terminal.store_port import get_meta_repo
        from core.terminal.event_runtime import get_terminal_event_store

        meta_repo = get_meta_repo()
        repository = getattr(meta_repo, "analysis_projections", None)
        if repository is None:
            from db.repo_analysis_projections import AnalysisProjectionRepository

            repository = AnalysisProjectionRepository(meta_repo.engine)
        event_store = get_terminal_event_store(force=True)
        if event_store is None:
            return None, repository, "terminal event store unavailable"
        return event_store, repository, ""
    except Exception as exc:  # noqa: BLE001 - bootstrap may fail in isolated use
        warn("core/analysis/workers.py:218 Exception", err=repr(exc))
        return None, None, str(exc)[:180]


async def run_event_analysis_worker(
    *,
    messages: Any = None,
    account_ctx: Any = None,
    limit: int = 200,
) -> dict[str, Any]:
    """Async bridge used by ``scheduler.main._run_analysis_pipeline``.

    ``messages`` / ``account_ctx`` are accepted for call-site compatibility with
    the handoff pseudocode; the worker deliberately reads the canonical event
    stream instead of trusting the sync payload.
    """

    def _job() -> dict[str, Any]:
        event_store, repository, error = _runtime_stores()
        if error or event_store is None or repository is None:
            return {
                "scanned": 0,
                "projected": 0,
                "written": 0,
                "failed": 0,
                "cursor": 0,
                "error": error or "analysis runtime unavailable",
            }
        result = run_analysis_worker_once(
            event_store, repository, limit=max(1, int(limit or 200))
        )
        result.setdefault("account_id", getattr(account_ctx, "account_id", ""))
        return result

    try:
        return await asyncio.to_thread(_job)
    except Exception as exc:  # noqa: BLE001 - background path must not raise
        warn("core/analysis/workers.py:254 Exception", err=repr(exc))
        return {
            "scanned": 0,
            "projected": 0,
            "written": 0,
            "failed": 0,
            "cursor": 0,
            "error": str(exc)[:180],
        }


def run_daily_digest_once(
    event_store: Any,
    repository: Any,
    *,
    target_date: date | None = None,
    max_batches: int = 200,
) -> dict[str, Any]:
    """Build/refresh one ``daily_digest`` projection from canonical events."""
    worker_date = target_date or date.today()
    reader = AnalysisEventReader(event_store)
    events = reader.read_all(max_batches=max_batches)
    try:
        analyses = repository.all(limit=None)
    except Exception:
        warn("core/analysis/workers.py:278 Exception", err=repr(__import__("sys").exc_info()[1]))
        analyses = []
    draft = build_daily_digest(
        worker_date,
        events=events,
        analyses=analyses,
    )
    if draft is None:
        return {
            "date": worker_date.isoformat(),
            "written": 0,
            "scanned_events": len(events),
            "reason": "no_source_events",
        }
    try:
        repository.upsert(draft)
    except Exception as exc:  # noqa: BLE001 - digest failure stays in background
        warn("core/analysis/workers.py:294 Exception", err=repr(exc))
        return {
            "date": worker_date.isoformat(),
            "written": 0,
            "scanned_events": len(events),
            "error": str(exc)[:180],
        }
    return {
        "date": worker_date.isoformat(),
        "written": 1,
        "projection_id": draft.projection_id,
        "source_events": len(draft.source_event_ids),
        "scanned_events": len(events),
    }


async def run_daily_digest_worker(
    *,
    target_date: date | None = None,
    max_batches: int = 200,
) -> dict[str, Any]:
    """Async bridge used by the scheduler when Track B owns the digest."""
    event_store, repository, error = _runtime_stores()
    if error or event_store is None or repository is None:
        return {
            "written": 0,
            "error": error or "analysis runtime unavailable",
        }
    try:
        return await asyncio.to_thread(
            run_daily_digest_once,
            event_store,
            repository,
            target_date=target_date,
            max_batches=max_batches,
        )
    except Exception as exc:  # noqa: BLE001
        warn("core/analysis/workers.py:330 Exception", err=repr(exc))
        return {"written": 0, "error": str(exc)[:180]}


def analysis_projection_cursor_key() -> str:
    return ANALYSIS_CURSOR_KEY


__all__ = [
    "ANALYSIS_CURSOR_KEY",
    "AnalysisFromEventsWorker",
    "GroupSummaryWorker",
    "MessageAnalysisWorker",
    "analysis_projection_cursor_key",
    "run_analysis_worker_once",
    "run_daily_digest_once",
    "run_daily_digest_worker",
    "run_event_analysis_worker",
]
