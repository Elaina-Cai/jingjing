"""Phase 4 audit/export/retention helpers.

The terminal tables are append-only in normal operation; this module provides
the owner-facing escape hatch required by the handoff (export / retention /
delete strategy).  It never deletes implicitly:

* ``export_all`` writes JSONL + manifest for every audit table;
* ``retention_preview`` only counts what *would* be removed;
* ``apply_retention`` needs an explicit call, writes a SYSTEM tombstone event
  first, then deletes rows older than the configured window.

Commands / authorizations / link-history default to ``days=0`` (never
auto-delete) because they are the legal-audit records.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session
from safelog import warn


def _utcnow() -> datetime:
    return datetime.now()


def _row_to_dict(row: Any) -> dict:
    table = getattr(row, "__table__", None)
    columns = list(table.columns) if table is not None else []
    out: dict[str, Any] = {}
    for column in columns:
        value = getattr(row, column.name, None)
        if isinstance(value, datetime):
            value = value.isoformat()
        out[column.name] = value
    return out


@dataclass(frozen=True)
class RetentionPolicy:
    event_days: int = 365
    projection_days: int = 365
    command_days: int = 0
    authorization_days: int = 0
    query_log_days: int = 180
    link_edge_days: int = 0
    membership_days: int = 0

    def as_table_days(self) -> dict[str, int]:
        return {
            "terminal_events": int(self.event_days or 0),
            "memory_projections": int(self.projection_days or 0),
            "command_records": int(self.command_days or 0),
            "authorization_records": int(self.authorization_days or 0),
            "command_query_logs": int(self.query_log_days or 0),
            "account_link_edges": int(self.link_edge_days or 0),
            "group_membership_periods": int(self.membership_days or 0),
        }


def _models() -> dict[str, tuple[Any, str]]:
    from db.meta_models import (
        AccountLinkEdge,
        AuthorizationRecordRow,
        CommandQueryLogRow,
        CommandRecordRow,
        GroupMembershipPeriod,
        MemoryProjectionRow,
        TerminalEvent,
    )

    return {
        "terminal_events": (TerminalEvent, "occurred_at"),
        "memory_projections": (MemoryProjectionRow, "created_at"),
        "command_records": (CommandRecordRow, "created_at"),
        "authorization_records": (AuthorizationRecordRow, "created_at"),
        "command_query_logs": (CommandQueryLogRow, "queried_at"),
        "account_link_edges": (AccountLinkEdge, "created_at"),
        "group_membership_periods": (GroupMembershipPeriod, "joined_at"),
    }


def export_table(
    engine: Any,
    table_name: str,
    path: str | Path,
    *,
    batch_size: int = 1000,
) -> int:
    model, _ = _models()[table_name]
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with Session(engine) as session:
        with target.open("w", encoding="utf-8") as handle:
            offset = 0
            while True:
                rows = list(
                    session.execute(
                        select(model).limit(max(1, int(batch_size))).offset(offset)
                    ).scalars().all()
                )
                if not rows:
                    break
                for row in rows:
                    handle.write(json.dumps(
                        _row_to_dict(row), ensure_ascii=False, default=str
                    ))
                    handle.write("\n")
                    count += 1
                if len(rows) < batch_size:
                    break
                offset += len(rows)
    return count


def export_all(engine: Any, out_dir: str | Path) -> dict[str, Any]:
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    started = _utcnow()
    tables: dict[str, int] = {}
    for table_name in _models():
        try:
            tables[table_name] = export_table(
                engine, table_name, target / f"{table_name}.jsonl"
            )
        except Exception as exc:  # noqa: BLE001 - a missing table is not fatal
            warn("core/terminal/audit_export.py:132 Exception", err=repr(exc))
            tables[table_name] = -1
            (target / f"{table_name}.error.txt").write_text(
                str(exc)[:500], encoding="utf-8"
            )
    manifest = {
        "created_at": started.isoformat(),
        "out_dir": str(target),
        "tables": tables,
    }
    (target / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def retention_preview(
    engine: Any,
    policy: RetentionPolicy,
) -> dict[str, int]:
    now = _utcnow()
    out: dict[str, int] = {}
    models = _models()
    with Session(engine) as session:
        for table_name, days in policy.as_table_days().items():
            if days <= 0:
                out[table_name] = 0
                continue
            model, column_name = models[table_name]
            column = getattr(model, column_name)
            cutoff = now - timedelta(days=days)
            try:
                out[table_name] = int(
                    session.execute(
                        select(func.count()).select_from(model).where(column < cutoff)
                    ).scalar() or 0
                )
            except Exception:
                warn("core/terminal/audit_export.py:169 Exception", err=repr(__import__("sys").exc_info()[1]))
                out[table_name] = -1
    return out


def _write_tombstone(engine: Any, summary: str) -> None:
    """先落一条 SYSTEM 墓碑事件，再删除（顺序即约束，见模块 docstring）。

    取号交给 :meth:`TerminalEventStore.append` 的**库内原子取号**（§5.10）：
    不再「读 max → Python +1 → 直插」——那既与发号约定矛盾，多写入者下还会重号。
    失败**不吞**（旧实现 ``except: pass`` 会让「删了但没留痕」静默发生）：
    这里向上抛，由 :func:`apply_retention` 在动手删除之前中止。
    """
    from core.terminal.event_model import Event, EventType
    from core.terminal.event_store_db import TerminalEventStore
    from core.terminal.principal import VisibilityScope

    TerminalEventStore(engine).append(
        Event.new(
            event_type=EventType.SYSTEM,
            occurred_at=_utcnow(),
            content=f"[retention] {summary}",
            visibility_scope=frozenset({VisibilityScope.OWNER_ONLY}),
            metadata={"type": "retention_purge"},
        )
    )


def apply_retention(
    engine: Any,
    policy: RetentionPolicy,
    *,
    dry_run: bool = True,
) -> dict[str, Any]:
    preview = retention_preview(engine, policy)
    if dry_run:
        return {"dry_run": True, "would_delete": preview}

    now = _utcnow()
    deleted: dict[str, int] = {}
    models = _models()
    table_days = policy.as_table_days()
    # 墓碑在删除事务**之前**单独提交：写不进去就整个中止，绝不「删了却没留痕」。
    _write_tombstone(
        engine,
        "purge " + ", ".join(
            f"{name}:>{days}d" for name, days in table_days.items() if days > 0
        ),
    )
    with Session(engine) as session:
        for table_name, days in table_days.items():
            if days <= 0:
                deleted[table_name] = 0
                continue
            model, column_name = models[table_name]
            column = getattr(model, column_name)
            cutoff = now - timedelta(days=days)
            try:
                result = session.execute(
                    delete(model).where(column < cutoff)
                )
                deleted[table_name] = int(result.rowcount or 0)
            except Exception:
                warn("core/terminal/audit_export.py:231 Exception", err=repr(__import__("sys").exc_info()[1]))
                deleted[table_name] = -1
        session.commit()
    return {"dry_run": False, "deleted": deleted, "preview": preview}


def audit_counts(engine: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    with Session(engine) as session:
        for table_name, (model, _column) in _models().items():
            try:
                counts[table_name] = int(
                    session.execute(
                        select(func.count()).select_from(model)
                    ).scalar() or 0
                )
            except Exception:
                warn("core/terminal/audit_export.py:247 Exception", err=repr(__import__("sys").exc_info()[1]))
                counts[table_name] = -1
    return counts
