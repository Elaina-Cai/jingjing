"""Repository for Phase 2 memory projections and projection cursors."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Sequence

from sqlalchemy import and_, delete, func, or_, select

from core import temporal
from core.terminal.memory_projection import MemoryProjectionItem
from db.meta_models import MemoryProjectionCursor, MemoryProjectionRow
from db.repo_base import BaseRepository
from safelog import warn


def _loads(value: Any, fallback: Any) -> Any:
    if value in (None, ""):
        return fallback
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except Exception:
        warn("db/repo_memory_projections.py:24 Exception", err=repr(__import__("sys").exc_info()[1]))
        return fallback


def _dumps(value: Any, fallback: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        warn("db/repo_memory_projections.py:31 Exception", err=repr(__import__("sys").exc_info()[1]))
        return json.dumps(fallback, ensure_ascii=False)


def _aware(value: Any) -> datetime | None:
    """SQLite 回来的是 naive 本地墙钟；aware 的转本地再去 tzinfo（owner = ``temporal``）。"""
    if value is None:
        return None
    if getattr(value, "tzinfo", None) is None:
        return value
    return temporal.to_naive_local(value)


def _item_from_row(row: MemoryProjectionRow) -> MemoryProjectionItem:
    scopes = _loads(getattr(row, "visibility_scope_json", '["public"]'), ["public"])
    if isinstance(scopes, str):
        scopes = [scopes]
    subjects = _loads(getattr(row, "subject_person_ids_json", "[]"), [])
    sources = _loads(getattr(row, "source_event_ids_json", "[]"), [])
    metadata = _loads(getattr(row, "metadata_json", "{}"), {})
    return MemoryProjectionItem(
        memory_id=str(getattr(row, "memory_id", "") or ""),
        memory_type=str(getattr(row, "memory_type", "") or "episode"),
        content=str(getattr(row, "content", "") or ""),
        platform=str(getattr(row, "platform", "") or ""),
        bot_account_id=str(getattr(row, "bot_account_id", "") or ""),
        subject_person_ids=tuple(str(value) for value in (subjects or ())),
        source_event_ids=tuple(str(value) for value in (sources or ())),
        event_sequence=int(getattr(row, "event_sequence", 0) or 0),
        occurred_at=_aware(getattr(row, "valid_from", None)),
        valid_to=_aware(getattr(row, "valid_to", None)),
        confidence=float(getattr(row, "confidence", 0.5) or 0.0),
        visibility_scope=tuple(str(value) for value in (scopes or ())),
        sensitivity=str(getattr(row, "sensitivity", "") or "normal"),
        linked_component_id=str(getattr(row, "linked_component_id", "") or ""),
        link_revision=int(getattr(row, "link_revision", 0) or 0),
        metadata=dict(metadata or {}),
    )


def _values(item: MemoryProjectionItem) -> dict[str, Any]:
    return {
        "memory_id": str(item.memory_id or ""),
        "memory_type": str(item.memory_type or "episode"),
        "content": str(item.content or ""),
        "platform": str(item.platform or ""),
        "bot_account_id": str(item.bot_account_id or ""),
        "subject_person_ids_json": _dumps(list(item.subject_person_ids or ()), []),
        "source_event_ids_json": _dumps(list(item.source_event_ids or ()), []),
        "event_sequence": int(item.event_sequence or 0),
        "valid_from": item.occurred_at,
        "valid_to": item.valid_to,
        "confidence": float(item.confidence or 0.0),
        "visibility_scope_json": _dumps(
            list(item.visibility_scope or ("public",)), ["public"]
        ),
        "sensitivity": str(item.sensitivity or "normal"),
        "linked_component_id": str(item.linked_component_id or ""),
        "link_revision": int(item.link_revision or 0),
        "metadata_json": _dumps(dict(item.metadata or {}), {}),
    }


class MemoryProjectionRepository(BaseRepository):
    """Persist/read extracted memory projections."""

    def assert_writable(self) -> None:
        from core.memory.legacy_write_freeze import assert_projection_writable
        assert_projection_writable(self.read_engine())

    def upsert(self, item: MemoryProjectionItem) -> None:
        """Insert or refresh one projection.  ``valid_to`` is **sticky**（ENG-23）.

        语义：既有行 ``valid_to`` 非空（已被纠错学习等路径失效）时，本次写入
        **保留**它，只刷新内容列。理由：

        * ``valid_to`` 是"这条记忆已失效"的墓碑，只能由失效路径（``invalidate``）
          或人工解除，**不能**被一次重放/重投影顺手清除；
        * 投影目标是 ``proj:<event_id>``（事件 id 级），而 ``invalidate`` 必须按
          **记忆 id** 整条失效——回补 8 行重号漏行要 ``backfill``/重放，没有这条
          保护就会把现网 53 条已失效记忆的 ``valid_to`` 覆写回 NULL（复活）。
        """
        with self._write_session() as session:
            self.upsert_in_session(session, item)
            session.commit()

    def upsert_in_session(self, session, item: MemoryProjectionItem) -> None:
        """复用同一投影写规则；事务由显式记忆提交入口持有。"""
        values = _values(item)
        row = session.execute(
            select(MemoryProjectionRow).where(
                MemoryProjectionRow.memory_id == values["memory_id"]
            )
        ).scalars().first()
        if row is None:
            row = MemoryProjectionRow(memory_id=values["memory_id"])
            session.add(row)
        elif row.valid_to is not None:
            values.pop("valid_to", None)
        for key, value in values.items():
            setattr(row, key, value)
        row.updated_at = datetime.now()

    def get(self, memory_id: str) -> MemoryProjectionItem | None:
        if not memory_id:
            return None
        with self._session() as session:
            row = session.execute(
                select(MemoryProjectionRow).where(
                    MemoryProjectionRow.memory_id == str(memory_id)
                )
            ).scalars().first()
        return _item_from_row(row) if row is not None else None

    def count(self) -> int:
        with self._session() as session:
            return int(
                session.execute(
                    select(func.count(MemoryProjectionRow.id))
                ).scalar() or 0
            )

    def query_candidates(
        self,
        *,
        limit: int = 500,
        platform: str = "",
        bot_account_id: str = "",
        component_id: str = "",
        accounts: Sequence[tuple[str, str]] | None = None,
    ) -> list[MemoryProjectionItem]:
        with self._session() as session:
            statement = select(MemoryProjectionRow)
            filters = []

            if platform and bot_account_id:
                filters.append(
                    (MemoryProjectionRow.platform == str(platform))
                    & (MemoryProjectionRow.bot_account_id == str(bot_account_id))
                )

            account_filters = [
                (
                    (MemoryProjectionRow.platform == str(account_platform))
                    & (
                        MemoryProjectionRow.bot_account_id
                        == str(account_id)
                    )
                )
                for account_platform, account_id in (accounts or ())
                if account_platform and account_id
            ]
            if account_filters:
                filters.append(or_(*account_filters))

            if component_id:
                filters.append(
                    MemoryProjectionRow.linked_component_id == str(component_id)
                )

            if filters:
                statement = statement.where(or_(*filters))

            statement = (
                statement.order_by(
                    MemoryProjectionRow.event_sequence.desc(),
                    MemoryProjectionRow.id.desc(),
                ).limit(max(1, int(limit)))
            )
            rows = list(session.execute(statement).scalars().all())
            rows.reverse()
        return [_item_from_row(row) for row in rows]

    def all(self, limit: int | None = None) -> list[MemoryProjectionItem]:
        with self._session() as session:
            statement = select(MemoryProjectionRow).order_by(
                MemoryProjectionRow.event_sequence.asc(),
                MemoryProjectionRow.id.asc(),
            )
            if limit is not None:
                statement = statement.limit(max(1, int(limit)))
            rows = list(session.execute(statement).scalars().all())
        return [_item_from_row(row) for row in rows]

    # --------------------------------------------------------------- cursor
    # ENG-23：游标是复合键 ``(last_sequence, last_id)``。``last_id`` 是
    # ``terminal_events.id``（自增主键），与号一起构成全序；只按号推进会漏读
    # 同号的后续行（跨批 + 迟到行）。
    def get_cursor_pair(self, key: str) -> tuple[int, int]:
        """返回 ``(last_sequence, last_id)``；无行 / 老行 → ``last_id = 0``。"""
        if not key:
            return (0, 0)
        with self._session() as session:
            row = session.get(MemoryProjectionCursor, str(key))
            if row is None:
                return (0, 0)
            return (
                max(0, int(getattr(row, "last_sequence", 0) or 0)),
                max(0, int(getattr(row, "last_id", 0) or 0)),
            )

    def set_cursor_pair(self, key: str, sequence: int, last_id: int = 0) -> None:
        """写入整对游标。清游标必须整对清（只清 sequence 会把 ``last_id`` 留下）。"""
        if not key:
            return
        with self._write_session() as session:
            connection = session.connection()
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            from core.memory.legacy_write_freeze import assert_projection_writable
            assert_projection_writable(self.engine, connection=connection)
            row = session.get(MemoryProjectionCursor, str(key))
            if row is None:
                row = MemoryProjectionCursor(key=str(key))
                session.add(row)
            row.last_sequence = max(0, int(sequence or 0))
            row.last_id = max(0, int(last_id or 0))
            row.updated_at = datetime.now()
            session.commit()

    def get_cursor(self, key: str) -> int:
        return self.get_cursor_pair(key)[0]

    def set_cursor(self, key: str, sequence: int, last_id: int = 0) -> None:
        """兼容旧单值入口：``set_cursor(key, 0)`` 仍等于「整对归零」。"""
        self.set_cursor_pair(key, sequence, last_id)

    def invalidate(self, memory_ids, at: datetime | None = None) -> int:
        """Set ``valid_to`` on the given projections (fact stays in events).

        Used by the correction-learning worker: when the user corrects a fact,
        the old projection is closed instead of deleted, so recall stops using
        it while the event stream keeps the whole history.
        """
        ids = [str(mid) for mid in (memory_ids or ()) if str(mid or "").strip()]
        if not ids:
            return 0
        moment = _aware(at) or datetime.now()
        closed = 0
        with self._write_session() as session:
            for memory_id in ids:
                row = session.execute(
                    select(MemoryProjectionRow).where(
                        MemoryProjectionRow.memory_id == memory_id
                    )
                ).scalars().first()
                if row is None:
                    continue
                row.valid_to = moment
                closed += 1
            session.commit()
        return closed

    def delete(self, memory_id: str) -> None:
        if not memory_id:
            return
        with self._write_session() as session:
            session.execute(
                delete(MemoryProjectionRow).where(
                    MemoryProjectionRow.memory_id == str(memory_id)
                )
            )
            session.commit()

    def delete_all(self) -> None:
        with self._write_session() as session:
            session.execute(delete(MemoryProjectionRow))
            session.execute(delete(MemoryProjectionCursor))
            session.commit()
