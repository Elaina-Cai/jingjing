"""Sender-filter checks for the P2 evidence/search path.

Run directly: python tests/test_message_filters.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy.orm import Session

from db.models import Message, init_db
from db.repo_messages import MessageRepository

_ROOT = Path(__file__).resolve().parent.parent
_DB = _ROOT / "data" / "_test_message_filters.db"


def _cleanup() -> None:
    for suffix in ("", "-wal", "-shm"):
        Path(str(_DB) + suffix).unlink(missing_ok=True)


def main() -> bool:
    print("=" * 50)
    print("sender filter checks")
    print("=" * 50)
    _cleanup()
    try:
        engine = init_db(str(_DB))
        repo = MessageRepository(engine)
        now = datetime.now()
        with Session(engine) as s:
            s.add_all([
                Message(
                    msg_id=101, sender_id="wxid_a", sender_name="张三",
                    chat_id="group@chatroom", chat_type="group",
                    content="A 的消息", source="wechat", account_id="wxid_bot",
                    created_at=now,
                ),
                Message(
                    msg_id=102, sender_id="wxid_b", sender_name="李四",
                    chat_id="group@chatroom", chat_type="group",
                    content="B 的消息", source="wechat", account_id="wxid_bot",
                    created_at=now,
                ),
            ])
            s.commit()

        only_a = repo.get_recent(chat_id="group@chatroom", sender_id="wxid_a", limit=10)
        only_b = repo.get_by_chat("group@chatroom", limit=10, sender_id="wxid_b")
        all_msgs = repo.get_recent(chat_id="group@chatroom", limit=10)

        ok = len(all_msgs) == 2
        ok &= len(only_a) == 1 and only_a[0].sender_id == "wxid_a"
        ok &= len(only_b) == 1 and only_b[0].sender_id == "wxid_b"
        print(f"  all={len(all_msgs)} sender_a={len(only_a)} sender_b={len(only_b)}")
        print("  ok" if ok else "  FAIL")
        return bool(ok)
    finally:
        try:
            engine.dispose()
        except Exception:
            pass
        _cleanup()


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
