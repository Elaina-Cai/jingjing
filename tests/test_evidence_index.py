"""P2 evidence index checks.

Run directly: python tests/test_evidence_index.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.meta_repo import MetaRepositoryManager
from core.evidence import EvidenceIndexer

_ROOT = Path(__file__).resolve().parent.parent
_DB = _ROOT / "data" / "_test_evidence.db"


def _cleanup(path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)


class _FakeMessages:
    def __init__(self, rows: list[dict[str, Any]]):
        self._rows = rows

    def get_recent(self, *, synced_since=None, limit: int = 100, offset: int = 0):
        return self._rows[offset:offset + limit]

    def to_dict(self, row):
        return dict(row)


class _FakeAccountRepo:
    def __init__(self, rows: list[dict[str, Any]]):
        self.messages = _FakeMessages(rows)


def test_evidence_index() -> bool:
    print("\n[evidence index]")
    _cleanup(_DB)
    meta = None
    try:
        meta = MetaRepositoryManager(str(_DB))
        rows = [
            {
                "msg_id": "101",
                "sender_id": "wxid_zhang",
                "sender_name": "张三",
                "chat_id": "group@chatroom",
                "chat_type": "group",
                "content": "大家早",
                "msg_type_name": "text",
                "is_self": 0,
                "msg_source": "user_direct",
                "source": "wechat",
                "account_id": "wxid_bot",
            },
            {
                "msg_id": "102",
                "sender_id": "wxid_bot",
                "sender_name": "鲸鲸",
                "chat_id": "group@chatroom",
                "chat_type": "group",
                "content": "早呀",
                "msg_type_name": "text",
                "is_self": 1,
                "msg_source": "yinyue_own",
                "source": "wechat",
                "account_id": "wxid_bot",
            },
            {
                "msg_id": "103",
                "sender_id": "wxid_zhang",
                "sender_name": "张三",
                "chat_id": "group@chatroom",
                "chat_type": "group",
                "content": "今天有会吗",
                "msg_type_name": "text",
                "is_self": 0,
                "msg_source": "user_direct",
                "source": "wechat",
                "account_id": "wxid_bot",
            },
        ]
        indexer = EvidenceIndexer(meta.evidence, meta.people)
        indexed = indexer.index_recent_for_account(
            _FakeAccountRepo(rows),
            platform="wechat",
            account_id="wxid_bot",
        )

        ok = indexed == 3
        ok &= meta.evidence.count() == 3
        bot = meta.evidence.find_bot_message(
            "wechat", "wxid_bot", "group@chatroom", "102",
        )
        ok &= bot is not None and bot.role == "assistant"
        user_row = meta.evidence.get("wechat", "wxid_bot", "group@chatroom", "101")
        ok &= user_row is not None and user_row.role == "user"
        ok &= bool(user_row.person_id)

        # Idempotent upsert on the same key.
        meta.evidence.upsert(
            platform="wechat", account_id="wxid_bot",
            chat_id="group@chatroom", message_id="101",
            role="user", kind="text", content="大家早",
        )
        ok &= meta.evidence.count() == 3

        if user_row and user_row.person_id:
            rows_by_person = meta.evidence.list_by_person(user_row.person_id)
            ok &= len(rows_by_person) == 2
        send_log = SimpleNamespace(
            id=77, platform="wechat", chat_id="group@chatroom",
            account="wxid_bot", content="早呀", msg_type="text",
            trigger="route_will_reply", trigger_msg_id=101,
            source="yinyue_own", sent_at=None, confirmed_at=None,
            created_at=None, confirmed_msg_id=202,
        )
        send_row = indexer.index_send_log(send_log, account_id="wxid_bot")
        ok &= send_row is not None and send_row.role == "assistant"
        ok &= meta.evidence.find_bot_message(
            "wechat", "wxid_bot", "group@chatroom", "202",
        ) is not None

        print(f"  indexed={indexed} bot_role={bot.role if bot else None}")
        print("  ok" if ok else "  FAIL")
        return bool(ok)
    finally:
        try:
            if meta is not None:
                meta.engine.dispose()
        except Exception:
            pass
        _cleanup(_DB)


def test_role_classifier() -> bool:
    print("\n[role classifier]")
    ok = EvidenceIndexer.role_for_message({"is_self": 0}) == "user"
    ok &= EvidenceIndexer.role_for_message(
        {"is_self": 1, "msg_source": "yinyue_own"}
    ) == "assistant"
    ok &= EvidenceIndexer.role_for_message(
        {"is_self": 1, "msg_source": "user_direct"}
    ) == "user"
    print("  ok" if ok else "  FAIL")
    return bool(ok)


def main() -> bool:
    print("=" * 50)
    print("evidence index checks")
    print("=" * 50)
    ok = test_evidence_index()
    ok &= test_role_classifier()
    print("\n" + ("all passed" if ok else "FAILED"))
    return bool(ok)


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
