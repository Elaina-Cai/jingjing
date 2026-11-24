"""Cross-process leases for feedback consumption.

The lease is acquired **before** the model call and released after the attempt.
A second worker that sees an unexpired lease skips the sample without calling
the model.  The table is intentionally not created implicitly: activation
requires an explicit migration step, so turning the flag on never changes a
production schema behind the operator's back.
"""
from __future__ import annotations

import os
import socket
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text

from core.infra.envflags import flag

TABLE = "feedback_leases"
FLAG = "YINYUE_FEEDBACK_LEASE"
DEFAULT_TTL_S = 600

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS feedback_leases (
    sample_id INTEGER PRIMARY KEY,
    owner TEXT NOT NULL,
    token TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
)
"""

_OWNER = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


class LeaseUnavailable(RuntimeError):
    """The opt-in lease table is missing; fail closed instead of auto-DDL."""


def enabled() -> bool:
    return flag(FLAG, False)


def owner() -> str:
    return _OWNER


def _stamp(value: datetime | None = None) -> str:
    base = value or datetime.now()
    return base.replace(microsecond=0).isoformat(sep=" ")


def ensure_schema(engine: Any) -> None:
    """Create the lease table explicitly (tests / migration script only)."""
    with engine.begin() as conn:
        conn.execute(text(CREATE_SQL))


def table_exists(engine: Any) -> bool:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:name"),
            {"name": TABLE},
        ).first()
        return row is not None


def try_acquire(
    engine: Any,
    sample_id: int,
    *,
    owner_id: str | None = None,
    ttl_s: int = DEFAULT_TTL_S,
    now: datetime | None = None,
) -> str:
    """Return a lease token, or "" when another live owner holds it."""
    if not table_exists(engine):
        raise LeaseUnavailable(TABLE)
    who = owner_id or _OWNER
    base = now or datetime.now()
    acquired = _stamp(base)
    expires = _stamp(base + timedelta(seconds=max(1, int(ttl_s))))
    token = uuid.uuid4().hex
    sql = text(
        """
        INSERT INTO feedback_leases
            (sample_id, owner, token, acquired_at, expires_at)
        VALUES (:sample_id, :owner, :token, :acquired, :expires)
        ON CONFLICT(sample_id) DO UPDATE SET
            owner=excluded.owner,
            token=excluded.token,
            acquired_at=excluded.acquired_at,
            expires_at=excluded.expires_at
        WHERE feedback_leases.expires_at <= :acquired
        """
    )
    with engine.begin() as conn:
        result = conn.execute(
            sql,
            {
                "sample_id": int(sample_id),
                "owner": who,
                "token": token,
                "acquired": acquired,
                "expires": expires,
            },
        )
    return token if result.rowcount == 1 else ""


def release(engine: Any, sample_id: int, token: str) -> bool:
    if not token:
        return False
    with engine.begin() as conn:
        result = conn.execute(
            text("DELETE FROM feedback_leases WHERE sample_id=:sid AND token=:token"),
            {"sid": int(sample_id), "token": str(token)},
        )
    return result.rowcount == 1


def held(engine: Any, sample_id: int, token: str, *, now: datetime | None = None) -> bool:
    if not token:
        return False
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT 1 FROM feedback_leases "
                "WHERE sample_id=:sid AND token=:token AND expires_at > :now"
            ),
            {"sid": int(sample_id), "token": str(token), "now": _stamp(now)},
        ).first()
    return row is not None


__all__ = [
    "DEFAULT_TTL_S",
    "FLAG",
    "LeaseUnavailable",
    "TABLE",
    "enabled",
    "ensure_schema",
    "held",
    "owner",
    "release",
    "table_exists",
    "try_acquire",
]
