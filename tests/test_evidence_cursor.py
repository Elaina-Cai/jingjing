"""Evidence index worker cursor checks (P2).

Run directly: python tests/test_evidence_cursor.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.evidence import EvidenceIndexer
from db.meta_repo import MetaRepositoryManager

_ROOT = Path(__file__).resolve().parent.parent
_DB = _ROOT / "data" / "_test_evidence_cursor.db"


def _cleanup() -> None:
    for suffix in ("", "-wal", "-shm"):
        Path(str(_DB) + suffix).unlink(missing_ok=True)


def _row(index: int, synced_at: datetime, *, chat_id: str = "group@chatroom"):
    return {
        "id": 1000 + index,
        "msg_id": str(index),
        "sender_id": "wxid_a",
        "sender_name": "张三",
        "chat_id": chat_id,
        "chat_type": "group",
        "content": f"第 {index} 条",
        "msg_type_name": "text",
        "is_self": 0,
        "msg_source": "user_direct",
        "source": "wechat",
        "account_id": "wxid_bot",
        "synced_at": synced_at,
    }


class _FakeMessages:
    def __init__(self, rows: list[dict[str, Any]]):
        self.rows = sorted(rows, key=lambda r: (r["synced_at"], int(r["id"])))

    def get_synced_after(self, *, after_synced_at=None, after_id: int = 0,
                         limit: int = 500):
        result = []
        for row in self.rows:
            synced_at = row["synced_at"]
            if after_synced_at is not None:
                if synced_at < after_synced_at:
                    continue
                if synced_at == after_synced_at and int(row["id"]) <= after_id:
                    continue
            result.append(row)
            if len(result) >= limit:
                break
        return result

    def to_dict(self, row):
        return dict(row)


class _FakeAccountRepo:
    def __init__(self, rows: list[dict[str, Any]]):
        self.messages = _FakeMessages(rows)


def test_cursor_worker() -> bool:
    print("\n[evidence cursor worker]")
    _cleanup()
    meta = None
    try:
        meta = MetaRepositoryManager(str(_DB))
        now = datetime(2026, 9, 21, 10, 0, 0)
        rows = [_row(index, now + timedelta(seconds=index)) for index in range(1, 4)]
        repo = _FakeAccountRepo(rows)
        indexer = EvidenceIndexer(meta.evidence, meta.people)

        # Batch cap stops mid-stream and leaves a resumable cursor.
        written = indexer.index_pending_for_account(
            repo, platform="wechat", account_id="wxid_bot",
            cursors=meta.evidence_cursors, batch_size=2, max_batches=1,
        )
        cursor = meta.evidence_cursors.get("wechat", "wxid_bot")
        ok = written == 2
        ok &= meta.evidence.count() == 2
        ok &= cursor is not None and cursor.last_message_id == 1002
        ok &= cursor is not None and cursor.indexed_total == 2

        # Next pass resumes from the cursor and indexes only the remainder.
        written = indexer.index_pending_for_account(
            repo, platform="wechat", account_id="wxid_bot",
            cursors=meta.evidence_cursors, batch_size=2, max_batches=2,
        )
        cursor = meta.evidence_cursors.get("wechat", "wxid_bot")
        ok &= written == 1
        ok &= meta.evidence.count() == 3
        ok &= cursor is not None and cursor.last_message_id == 1003
        ok &= cursor is not None and cursor.indexed_total == 3

        # An empty pass is a no-op, not a rescan.
        written = indexer.index_pending_for_account(
            repo, platform="wechat", account_id="wxid_bot",
            cursors=meta.evidence_cursors, batch_size=2, max_batches=2,
        )
        ok &= written == 0 and meta.evidence.count() == 3

        # New sync rows are picked up from the stored boundary.
        repo.messages.rows.extend([
            _row(4, now + timedelta(seconds=10)),
            _row(5, now + timedelta(seconds=11)),
        ])
        repo.messages.rows.sort(key=lambda r: (r["synced_at"], int(r["id"])))
        written = indexer.index_pending_for_account(
            repo, platform="wechat", account_id="wxid_bot",
            cursors=meta.evidence_cursors, batch_size=5, max_batches=2,
        )
        cursor = meta.evidence_cursors.get("wechat", "wxid_bot")
        ok &= written == 2 and meta.evidence.count() == 5
        ok &= cursor is not None and cursor.indexed_total == 5

        print(
            f"  messages indexed={meta.evidence.count()} "
            f"cursor={cursor.last_message_id if cursor else None}"
        )
        print("  ok" if ok else "  FAIL")
        return bool(ok)
    finally:
        try:
            if meta is not None:
                meta.engine.dispose()
        except Exception:
            pass
        _cleanup()


def main() -> bool:
    print("=" * 50)
    print("evidence cursor checks")
    print("=" * 50)
    ok = test_cursor_worker()
    print("\n" + ("all passed" if ok else "FAILED"))
    return bool(ok)


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
