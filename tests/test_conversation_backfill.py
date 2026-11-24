"""Historical conversation identity backfill checks (P2).

Run directly: python tests/test_conversation_backfill.py
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.identity import ConversationIdentityBackfiller
from db.meta_models import ChatSession, Conversation
from db.meta_repo import MetaRepositoryManager

_ROOT = Path(__file__).resolve().parent.parent
_DB = _ROOT / "data" / "_test_backfill.db"


def _cleanup() -> None:
    for suffix in ("", "-wal", "-shm"):
        Path(str(_DB) + suffix).unlink(missing_ok=True)


def _session(meta, *, source: str, chat_id: str = "", name: str = ""):
    return meta.sessions.add(ChatSession(
        persona_id=1,
        title=name or chat_id or "web",
        source=source,
        metadata_=json.dumps(
            {"platform": source, "chat_id": chat_id}, ensure_ascii=False,
        ) if chat_id else None,
        created_at=datetime.now(),
        updated_at=datetime.now(),
    ))


def test_backfill() -> bool:
    print("\n[conversation identity backfill]")
    _cleanup()
    meta = None
    try:
        meta = MetaRepositoryManager(str(_DB))
        now = datetime.now()
        group = _session(meta, source="wechat", chat_id="group@chatroom", name="群")
        private = _session(meta, source="wechat", chat_id="wxid_friend")
        unmatched = _session(meta, source="wechat", chat_id="other@chatroom")
        local = _session(meta, source="web")

        meta.conversations.add(Conversation(
            persona_id=1, session_id=group.id, role="user",
            content="群里的提问", created_at=now,
        ))
        meta.conversations.add(Conversation(
            persona_id=1, session_id=private.id, role="user",
            content="私聊提问", created_at=now,
        ))
        meta.conversations.add(Conversation(
            persona_id=1, session_id=private.id, role="assistant",
            content="私聊回答", created_at=now,
        ))
        meta.conversations.add(Conversation(
            persona_id=1, session_id=unmatched.id, role="user",
            content="无人匹配的群消息", created_at=now,
        ))
        meta.conversations.add(Conversation(
            persona_id=1, session_id=local.id, role="user",
            content="本地网页消息", created_at=now,
        ))

        backfiller = ConversationIdentityBackfiller(
            meta.conversations,
            meta.sessions,
            sender_matcher=lambda conversation, platform, account_id, chat_id: (
                "wxid_a" if chat_id == "group@chatroom" else ""
            ),
            account_resolver=lambda platform, chat_id, metadata: "wxid_bot",
        )

        # Dry run must report the same work without touching the table.
        dry = backfiller.run(batch_size=2, dry_run=True)
        ok = dry.scanned == 5
        ok &= dry.updated == 4
        ok &= dry.skipped == 1
        ok &= dry.sender_resolved == 2
        ok &= len(meta.conversations.list_missing_identity()) == 5

        report = backfiller.run(batch_size=2)
        ok &= report.scanned == 5
        ok &= report.updated == 4
        ok &= report.skipped == 1
        ok &= report.fields.get("platform") == 4
        ok &= report.fields.get("chat_id") == 4
        ok &= report.fields.get("account_id") == 4
        ok &= report.fields.get("sender_id") == 2
        ok &= report.next_after_id > 0

        rows = {
            row.content: row
            for row in meta.conversations.get_by_persona(1, limit=20)
        }
        group_row = rows["群里的提问"]
        private_row = rows["私聊提问"]
        assistant_row = rows["私聊回答"]
        unmatched_row = rows["无人匹配的群消息"]
        local_row = rows["本地网页消息"]

        ok &= group_row.platform == "wechat"
        ok &= group_row.account_id == "wxid_bot"
        ok &= group_row.chat_id == "group@chatroom"
        ok &= group_row.sender_id == "wxid_a"
        ok &= private_row.sender_id == "wxid_friend"
        ok &= assistant_row.platform == "wechat"
        ok &= assistant_row.chat_id == "wxid_friend"
        ok &= assistant_row.sender_id is None
        ok &= unmatched_row.platform == "wechat"
        ok &= unmatched_row.chat_id == "other@chatroom"
        ok &= unmatched_row.sender_id is None
        ok &= local_row.platform is None

        # Unmatched group sender + the local web row are still unmapped.
        ok &= report.remaining == 2
        ok &= len(meta.conversations.list_missing_identity()) == 2

        print(
            f"  updated={report.updated} skipped={report.skipped} "
            f"remaining={report.remaining}"
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
    print("conversation backfill checks")
    print("=" * 50)
    ok = test_backfill()
    print("\n" + ("all passed" if ok else "FAILED"))
    return bool(ok)


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
