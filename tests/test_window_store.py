"""Durable conversation-window checks (P3 persistence).

Run directly: python tests/test_window_store.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.meta_repo import MetaRepositoryManager
from core.contracts import DecisionAction
from core.conversation.shadow import ConversationShadowObserver
from core.conversation.store import MetaWindowStore

_ROOT = Path(__file__).resolve().parent.parent
_DB = _ROOT / "data" / "_test_window_store.db"
_TRACE = _ROOT / "logs" / "_test_window_store.jsonl"


def _cleanup() -> None:
    for suffix in ("", "-wal", "-shm"):
        Path(str(_DB) + suffix).unlink(missing_ok=True)
    _TRACE.unlink(missing_ok=True)


def _message(index: int, content: str):
    return {
        "msg_id": str(index),
        "sender_id": "wxid_a",
        "sender_name": "张三",
        "chat_id": "group@chatroom",
        "chat_type": "group",
        "content": content,
        "msg_type_name": "text",
        "source": "wechat",
        "account_id": "wxid_bot",
        "created_at": "2026-09-21 10:00:00",
    }


def _legacy(should_reply: bool):
    return SimpleNamespace(should_reply=should_reply, reason="legacy_test")


def main() -> bool:
    print("=" * 50)
    print("window store checks")
    print("=" * 50)
    _cleanup()
    meta = None
    try:
        meta = MetaRepositoryManager(str(_DB))
        store = MetaWindowStore(meta.conversation_windows)
        bot_ids = {"wxid_bot"}
        aliases = ["鲸鲸"]

        first = ConversationShadowObserver(store=store)
        results = first.observe_batch(
            [_message(1, "鲸鲸帮我查下天气")],
            [_legacy(True)],
            bot_ids=bot_ids, aliases=aliases, force=True,
            trace_path=_TRACE,
        )
        window_key = ("wechat", "wxid_bot", "group@chatroom", "wxid_a")
        window = store.get(window_key)
        ok = results[0].action == DecisionAction.REPLY
        ok &= window is not None and window.last_bot_message_id == ""

        # A new observer instance must see the durable window.
        second = ConversationShadowObserver(store=store)
        results = second.observe_batch(
            [_message(2, "那你觉得呢")],
            [_legacy(False)],
            bot_ids=bot_ids, aliases=aliases, force=True,
            trace_path=_TRACE,
        )
        ok &= results[0].action == DecisionAction.REPLY
        ok &= store.get(window_key) is not None

        # Bot reply feedback marks the window as waiting for an answer.
        second.mark_bot_reply(
            platform="wechat", account_id="wxid_bot",
            chat_id="group@chatroom", person_id="wxid_a",
            reply="是周五吗？",
        )
        window = store.get(window_key)
        ok &= window is not None and window.last_bot_asked_question is True
        open_windows = store.list_open(platform="wechat", account_id="wxid_bot")
        ok &= any(key[-1] == "wxid_a" for key, _ in open_windows)

        # Real LLM replies often ask with "哪/什么/怎么" rather than "?".
        window.last_bot_asked_question = False
        store.save(window_key, window)
        second.mark_bot_reply(
            platform="wechat", account_id="wxid_bot",
            chat_id="group@chatroom", person_id="wxid_a",
            reply="你得告诉我你在哪个城市",
        )
        window = store.get(window_key)
        ok &= window is not None and window.last_bot_asked_question is True

        print(f"  saved_window={window is not None} open={len(open_windows)}")
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
