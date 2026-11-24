from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class SyncRecord:
    started_at: str
    finished_at: str = ""
    status: str = "running"
    new_messages: int = 0
    agents_run: int = 0
    analyses_generated: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class SystemStatus:
    started_at: str
    last_sync: SyncRecord | None = None
    sync_count: int = 0
    sync_failures: int = 0
    last_error: str = ""
    ollama_ok: bool = False
    wechat_key_ok: bool = False
    current_phase: str = "idle"

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "current_phase": self.current_phase,
            "sync_count": self.sync_count,
            "sync_failures": self.sync_failures,
            "last_error": self.last_error,
            "ollama_ok": self.ollama_ok,
            "wechat_key_ok": self.wechat_key_ok,
            "last_sync": self.last_sync.__dict__ if self.last_sync else None,
        }


class StatusTracker:
    def __init__(self):
        self._status = SystemStatus(started_at=datetime.now().isoformat())

    @property
    def status(self) -> SystemStatus:
        return self._status

    def begin_sync(self) -> SyncRecord:
        rec = SyncRecord(started_at=datetime.now().isoformat())
        self._status.last_sync = rec
        self._status.current_phase = "syncing"
        return rec

    def finish_sync(self, rec: SyncRecord, new_messages: int = 0, agents_run: int = 0, analyses: int = 0):
        rec.finished_at = datetime.now().isoformat()
        rec.new_messages = new_messages
        rec.agents_run = agents_run
        rec.analyses_generated = analyses
        if rec.errors:
            rec.status = "failed"
            self._status.sync_failures += 1
            self._status.last_error = rec.errors[-1]
        else:
            rec.status = "ok"
            self._status.sync_count += 1
            self._status.last_error = ""
        self._status.current_phase = "idle"

    def set_phase(self, phase: str):
        self._status.current_phase = phase

    def set_ollama(self, ok: bool):
        self._status.ollama_ok = ok

    def set_wechat_key(self, ok: bool):
        self._status.wechat_key_ok = ok



def get_status_tracker() -> StatusTracker:
    from core.bootstrap import _singletons
    if "status" not in _singletons:
        _singletons["status"] = StatusTracker()
    return _singletons["status"]


status_tracker = property(lambda self: get_status_tracker())
