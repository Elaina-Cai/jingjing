"""Work item / job 到 Intention 的隔离关联表。

不修改 ``jobs`` / ``work_items`` 的既有列；新表只在显式迁移脚本或测试里创建。
读取方缺失该表时应退回到事件流里的 ``work_item_created`` 快照。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text

TABLE = "goal_links"
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS goal_links (
    subject_kind TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    goal_id TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    created_at DATETIME,
    PRIMARY KEY (subject_kind, subject_id, goal_id)
)
"""


def ensure_schema(engine: Any) -> None:
    with engine.begin() as conn:
        conn.execute(text(CREATE_SQL))


def link_goal(
    engine: Any,
    *,
    subject_kind: str,
    subject_id: str,
    goal_id: str,
    source: str = "",
    at: str = "",
) -> bool:
    kind = str(subject_kind or "").strip()
    subject = str(subject_id or "").strip()
    goal = str(goal_id or "").strip()
    if not kind or not subject or not goal:
        return False
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT OR IGNORE INTO goal_links "
                "(subject_kind, subject_id, goal_id, source, created_at) "
                "VALUES (:kind, :subject, :goal, :source, :at)"
            ),
            {"kind": kind, "subject": subject, "goal": goal, "source": source, "at": at or None},
        )
    return True


def goal_for(engine: Any, *, subject_kind: str, subject_id: str) -> str:
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT goal_id FROM goal_links "
                    "WHERE subject_kind=:kind AND subject_id=:subject "
                    "ORDER BY created_at IS NULL, created_at, rowid LIMIT 1"
                ),
                {"kind": str(subject_kind or ""), "subject": str(subject_id or "")},
            ).first()
        return str(row[0]) if row else ""
    except Exception:
        # 表未迁移时读取方必须能继续工作；调用方会退回事件快照。
        return ""
