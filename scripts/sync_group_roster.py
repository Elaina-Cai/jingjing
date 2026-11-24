#!/usr/bin/env python
"""手动同步群名单：逐人群内条目 + owner_only 唯一准版（dry-run 默认）。

    python scripts/sync_group_roster.py --text-file roster.txt --chat-id 10000000001@chatroom
    python scripts/sync_group_roster.py --text-file roster.txt --chat-id ... --apply
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

META_DB = ROOT / "data" / "yinyue_meta.db"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--text", default="", help="名单原文")
    parser.add_argument("--text-file", default="", help="从文件读名单原文")
    parser.add_argument("--chat-id", default="", help="群 chat_id（留空则按成员昵称自动识别）")
    parser.add_argument("--owner-chat-id", default="", help="主人私聊 chat_id（写 canonical 用）")
    parser.add_argument("--bot-account-id", default=os.environ.get("WECHAT_YINYUE_WXID", ""))
    parser.add_argument("--actor-person-id", default="", help="主人 person_id（provenance）")
    parser.add_argument("--apply", action="store_true", help="执行（默认 dry-run）")
    args = parser.parse_args()

    text = args.text
    if args.text_file:
        text = Path(args.text_file).read_text(encoding="utf-8")
    if not str(text).strip():
        print("[err] 需要 --text 或 --text-file")
        return 2
    if not META_DB.exists():
        print(f"[err] meta db not found: {META_DB}")
        return 2

    from core.quality.roster_sync import sync_roster
    from core.terminal.event_runtime import install_terminal_event_store
    from core.terminal.event_store_db import TerminalEventStore
    from db.meta_repo import MetaRepositoryManager

    meta = MetaRepositoryManager(str(META_DB))
    install_terminal_event_store(TerminalEventStore(meta.engine))
    try:
        result = sync_roster(
            meta,
            roster_text=text,
            chat_id=args.chat_id,
            owner_chat_id=args.owner_chat_id,
            platform="wechat",
            bot_account_id=args.bot_account_id,
            actor_person_id=args.actor_person_id,
            apply=args.apply,
        )
    finally:
        meta.engine.dispose()

    print(result)
    if result.get("error"):
        return 1
    if not args.apply:
        print("（dry-run；加 --apply 执行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
