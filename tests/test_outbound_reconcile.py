"""Outbound-reply reconciliation checks (P3).

Run directly: python tests/test_outbound_reconcile.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.meta_repo import MetaRepositoryManager
from core.conversation.domain import ConversationWindow, WindowState
from core.conversation.reconcile import reconcile_bot_message_ids
from core.conversation.store import MetaWindowStore, make_key

_ROOT = Path(__file__).resolve().parent.parent
_DB = _ROOT / "data" / "_test_outbound_reconcile.db"


def _cleanup() -> None:
    for suffix in ("", "-wal", "-shm"):
        Path(str(_DB) + suffix).unlink(missing_ok=True)


class _FakeSendLogRepo:
    def __init__(self, row):
        self._row = row

    def get(self, send_log_id: int):
        if self._row is not None and int(send_log_id) == int(self._row.id):
            return self._row
        return None


def main() -> bool:
    print("=" * 50)
    print("outbound reconcile checks")
    print("=" * 50)
    _cleanup()
    meta = None
    try:
        meta = MetaRepositoryManager(str(_DB))
        store = MetaWindowStore(meta.conversation_windows)
        key = make_key("wechat", "wxid_bot", "group@chatroom", "wxid_a")
        window = ConversationWindow(
            platform="wechat", account_id="wxid_bot",
            chat_id="group@chatroom", person_id="wxid_a",
            state=WindowState.OPEN,
            last_bot_message_id="",
            last_send_log_id="42",
        )
        store.save(key, window)

        confirmed = SimpleNamespace(id=42, confirmed_msg_id=999)
        updated = reconcile_bot_message_ids(store, _FakeSendLogRepo(confirmed))

        row = store.get(key)
        ok = updated == 1
        ok &= row is not None and row.last_bot_message_id == "999"
        ok &= row is not None and row.last_send_log_id == ""
        ok &= store.list_pending_bot_replies() == []

        # Not-yet-confirmed send logs must stay pending, not be cleared.
        store.save(key, ConversationWindow(
            platform="wechat", account_id="wxid_bot",
            chat_id="group@chatroom", person_id="wxid_a",
            state=WindowState.OPEN,
            last_send_log_id="43",
        ))
        pending = SimpleNamespace(id=43, confirmed_msg_id=None)
        updated = reconcile_bot_message_ids(store, _FakeSendLogRepo(pending))
        ok &= updated == 0
        ok &= len(store.list_pending_bot_replies()) == 1

        print(f"  updated={updated} bot_id={row.last_bot_message_id if row else ''}")
        print("  ok" if ok else "  FAIL")
        return bool(ok)
    finally:
        try:
            if meta is not None:
                meta.engine.dispose()
        except Exception:
            pass
        _cleanup()


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
