"""End-to-end reply-chain regression (P3).

Run directly: python tests/test_reply_chain_e2e.py

Chain under test:

    send_log.confirmed_msg_id
      -> evidence index (assistant)
      -> window reconciliation
      -> window.last_bot_message_id
      -> incoming reply_to_id
      -> resolver REPLY
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.contracts import Addressee, ChatType, DecisionAction, MessageEvent
from core.conversation import AddresseeResolver, reconcile_bot_message_ids
from core.conversation.domain import ConversationWindow, WindowState
from core.conversation.store import MetaWindowStore, make_key
from core.evidence import EvidenceIndexer
from db.meta_repo import MetaRepositoryManager
from db.models import init_db
from db.repo_sendlog import SendLogRepository

_ROOT = Path(__file__).resolve().parent.parent
_META_DB = _ROOT / "data" / "_test_reply_chain_meta.db"
_ACCOUNT_DB = _ROOT / "data" / "_test_reply_chain_account.db"


def _cleanup() -> None:
    for path in (_META_DB, _ACCOUNT_DB):
        for suffix in ("", "-wal", "-shm"):
            Path(str(path) + suffix).unlink(missing_ok=True)


def main() -> bool:
    print("=" * 50)
    print("reply chain e2e checks")
    print("=" * 50)
    _cleanup()
    meta = None
    account_engine = None
    try:
        meta = MetaRepositoryManager(str(_META_DB))
        account_engine = init_db(str(_ACCOUNT_DB))
        send_repo = SendLogRepository(account_engine)

        send_log_id = send_repo.add(
            platform="wechat",
            chat_id="group@chatroom",
            content="是周五吗？",
            msg_type="text",
            source="yinyue_own",
            trigger="route_will_reply",
            account="wxid_bot",
            chat_type="group",
            trigger_msg_id=101,
        )
        send_repo.confirm_with_msg_id(send_log_id, 987654)
        send_log = send_repo.get(send_log_id)
        ok = send_log is not None and send_log.confirmed_msg_id == 987654

        indexer = EvidenceIndexer(meta.evidence, meta.people)
        evidence = indexer.index_send_log(send_log, account_id="wxid_bot")
        ok &= evidence is not None and evidence.role == "assistant"
        ok &= meta.evidence.find_bot_message(
            "wechat", "wxid_bot", "group@chatroom", "987654",
        ) is not None

        store = MetaWindowStore(meta.conversation_windows)
        key = make_key("wechat", "wxid_bot", "group@chatroom", "wxid_a")
        store.save(key, ConversationWindow(
            platform="wechat", account_id="wxid_bot",
            chat_id="group@chatroom", person_id="wxid_a",
            state=WindowState.OPEN,
            last_send_log_id=str(send_log_id),
        ))
        updated = reconcile_bot_message_ids(store, send_repo)
        window = store.get(key)
        ok &= updated == 1
        ok &= window is not None and window.last_bot_message_id == "987654"
        ok &= window is not None and window.last_bot_asked_question is True

        event = MessageEvent.new(
            platform="wechat", account_id="wxid_bot",
            chat_id="group@chatroom", chat_type=ChatType.GROUP,
            message_id="m-after-bot", sender_id="wxid_a", sender_name="张三",
            content="对，周五下午三点前发你", reply_to_id="987654",
            created_at=datetime.now(),
        )
        decision = AddresseeResolver().evaluate(event, window=window)
        ok &= decision.action == DecisionAction.REPLY
        ok &= decision.addressee == Addressee.BOT

        print(f"  send_log={send_log_id} bot_msg={window.last_bot_message_id if window else ''}")
        print(f"  resolver={decision.action.value}/{decision.addressee.value}")
        print("  ok" if ok else "  FAIL")
        return bool(ok)
    finally:
        try:
            if account_engine is not None:
                account_engine.dispose()
        except Exception:
            pass
        try:
            if meta is not None:
                meta.engine.dispose()
        except Exception:
            pass
        _cleanup()


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
