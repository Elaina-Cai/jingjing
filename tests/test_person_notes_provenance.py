"""person_notes 溯源（P2 收口，2026-09-29）。

覆盖：
- 批准写备注：source_proposal_id / evidence_json 落库；
- 普通写入不覆盖已有溯源；撤回（clear_provenance）清除；
- Migration 48 的历史回填（按 subject/chat + 内容包含匹配已批准提案）。

Run directly: python tests/test_person_notes_provenance.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import text

from core.context.person_notes import list_notes, upsert_note
from core.quality.memory_proposals import approve, revoke
from db.meta_repo import MetaRepositoryManager
from db.migrations import run_migrations

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "_test_person_notes_provenance.db"


def _fresh_meta():
    for suffix in ("", "-wal", "-shm"):
        Path(str(DB_PATH) + suffix).unlink(missing_ok=True)
    meta = MetaRepositoryManager(str(DB_PATH))
    run_migrations(str(DB_PATH))
    return meta


def _cleanup(meta) -> None:
    meta.engine.dispose()
    for suffix in ("", "-wal", "-shm"):
        Path(str(DB_PATH) + suffix).unlink(missing_ok=True)


def _seed_proposal(meta, *, content: str, status: str = "pending") -> int:
    with meta.engine.begin() as conn:
        result = conn.execute(
            text(
                "insert into memory_proposals "
                "(slot, subject_name, subject_key, chat_id, content, reason, "
                " evidence_json, status, created_at) "
                "values ('preference', :name, :key, :chat, :content, 'repeat', "
                " :evidence, :status, :now)"
            ),
            {
                "name": "P6",
                "key": "u1",
                "chat": "c1",
                "content": content,
                "evidence": '["ev9"]',
                "status": status,
                "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            },
        )
        return int(result.lastrowid)


def test_upsert_preserves_and_clears_provenance() -> None:
    meta = _fresh_meta()
    try:
        with patch("core.bootstrap.get_meta_repo", return_value=meta):
            assert upsert_note(
                person_key="u1", content="喜好：喜欢奶茶", chat_id="c1",
                source="auto", source_proposal_id=7, evidence_json='["ev1"]',
            )
            row = {r["person_key"]: r for r in list_notes("c1")}["u1"]
            assert row["source_proposal_id"] == 7
            assert "ev1" in row["evidence_json"]

            # 普通写入不覆盖已有溯源
            upsert_note(person_key="u1", content="喜好：喜欢奶茶", chat_id="c1")
            row = {r["person_key"]: r for r in list_notes("c1")}["u1"]
            assert row["source_proposal_id"] == 7 and "ev1" in row["evidence_json"]

            # 撤回 → 清除
            upsert_note(
                person_key="u1", content="喜好：", chat_id="c1",
                clear_provenance=True,
            )
            row = {r["person_key"]: r for r in list_notes("c1")}["u1"]
            assert row["source_proposal_id"] == 0
            assert row["evidence_json"] == ""
    finally:
        _cleanup(meta)


def test_approve_writes_and_revoke_clears_provenance() -> None:
    meta = _fresh_meta()
    try:
        proposal_id = _seed_proposal(meta, content="喜欢奶茶")
        with patch("core.bootstrap.get_meta_repo", return_value=meta):
            assert approve(proposal_id) is True
            row = {r["person_key"]: r for r in list_notes("c1")}["u1"]
            assert row["source_proposal_id"] == proposal_id
            assert "ev9" in row["evidence_json"]
            assert "奶茶" in row["content"]

            assert revoke(proposal_id) is True
            row = {r["person_key"]: r for r in list_notes("c1")}["u1"]
            assert row["source_proposal_id"] == 0
            assert row["evidence_json"] == ""
    finally:
        _cleanup(meta)


def test_migration_48_backfills_from_approved_proposals() -> None:
    from db.migrations import _m048

    meta = _fresh_meta()
    try:
        with meta.engine.begin() as conn:
            conn.execute(
                text(
                    "insert into person_notes "
                    "(platform, account_id, person_key, chat_id, content, source, active) "
                    "values ('', '', 'u1', 'c1', '喜好：喜欢奶茶', 'auto', 1)"
                )
            )
        _seed_proposal(meta, content="喜欢奶茶", status="approved")
        with meta.engine.begin() as conn:
            _m048(conn, None)
        with meta.engine.connect() as conn:
            row = conn.execute(
                text(
                    "select source_proposal_id, evidence_json from person_notes "
                    "where person_key='u1' and chat_id='c1'"
                )
            ).mappings().first()
        assert row is not None and row["source_proposal_id"] > 0, dict(row or {})
        assert "ev9" in row["evidence_json"]
    finally:
        _cleanup(meta)


def test_migration_48_is_safe_without_proposals_table() -> None:
    """账号库（有 person_notes 但无 memory_proposals）不得让 48 迁移炸掉。

    2026-09-29 线上实测：老迁移链会给部分账号库顺带建 person_notes，
    但 memory_proposals 只在部分库存在；48 的回填必须双表守卫。
    """
    import sqlite3

    from sqlalchemy import create_engine, text as sa_text

    from db.migrations import _m048

    db_path = ROOT / "data" / "_test_m048_no_proposals.db"
    for suffix in ("", "-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)
    con = sqlite3.connect(str(db_path))
    engine = None
    try:
        con.execute(
            "CREATE TABLE person_notes ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT DEFAULT '', "
            "account_id TEXT DEFAULT '', person_key TEXT, chat_id TEXT, "
            "content TEXT, source TEXT, active INTEGER DEFAULT 1, "
            "created_at DATETIME, updated_at DATETIME)"
        )
        con.commit()
        engine = create_engine(f"sqlite:///{db_path}")
        with engine.begin() as conn:
            _m048(conn, None)
            cols = {
                r[1]
                for r in conn.execute(sa_text("PRAGMA table_info(person_notes)"))
            }
        assert {"source_proposal_id", "evidence_json"} <= cols, cols
    finally:
        if engine is not None:
            engine.dispose()
        con.close()
        for suffix in ("", "-wal", "-shm"):
            Path(str(db_path) + suffix).unlink(missing_ok=True)


def main() -> int:
    tests = [
        test_upsert_preserves_and_clears_provenance,
        test_approve_writes_and_revoke_clears_provenance,
        test_migration_48_backfills_from_approved_proposals,
        test_migration_48_is_safe_without_proposals_table,
    ]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback

            print(f"FAIL {test.__name__}: {exc!r}")
            traceback.print_exc()
    print(f"{len(tests) - failed}/{len(tests)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
