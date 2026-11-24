"""Recover media clues for messages that were ingested before anything kept them.

Background
----------
``messages.media_path`` was written once, at ingest, and any image whose file
was not on disk yet at that instant was left NULL forever -- with the md5 that
would have allowed a retry discarded.  The raw WeChat rows still hold that md5
in ``packed_info_data``, so this walks the affected messages, re-reads their raw
row from the decrypted database, and registers a media row that the normal retry
pass can then resolve.

The same pass registers replies that quote a picture, which have no path of
their own in any version of the schema.

Usage::

    python scripts/backfill_media_registry.py --dry-run
    python scripts/backfill_media_registry.py
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, text  # noqa: E402

from core.conversation.media_registry import (  # noqa: E402
    KIND_IMAGE,
    KIND_QUOTE_IMAGE,
    quoted_is_media,
    register,
    resolve_pending,
    session_dir_for,
)
from db.content_extract import md5_from_packed_info  # noqa: E402
from db.migrations import run_migrations  # noqa: E402

# Message kinds whose raw row carries a picture we may have failed to resolve.
#
# Only real images.  Stickers (msg_type 47) are not listed on purpose: they are
# handled by the emoji pipeline, which resolves them from ``emoji_md5`` into
# data/emoji_cache and never used ``media_path`` in the first place -- treating
# their NULL as "missing" would queue hundreds of rows that are not broken.
_MEDIA_TYPES = {"image": KIND_IMAGE}


def _decrypted_msg_db(platform: str, account_id: str) -> Path:
    from core.config.settings import config

    base = Path(str(config.paths.get_account_decrypted_dir(platform, account_id)))
    for name in ("merge_msg.db", "msg_message_0.db"):
        candidate = base / name
        if candidate.exists():
            return candidate
    return base / "merge_msg.db"


def _raw_md5(con: sqlite3.Connection, table: str, msg_id: int,
             local_id: int | None) -> str:
    """Read one raw message row and pull the md5 out of packed_info_data."""
    try:
        row = con.execute(
            f"select packed_info_data from [{table}] where server_id = ?",
            (msg_id,),
        ).fetchone()
        if row is None and local_id:
            row = con.execute(
                f"select packed_info_data from [{table}] where local_id = ?",
                (local_id,),
            ).fetchone()
    except sqlite3.Error:
        return ""
    if row is None or not row[0]:
        return ""
    return md5_from_packed_info(row[0])


def backfill_conversation_paths(meta_db: Path, media_engine, *,
                                window_seconds: int = 120,
                                dry_run: bool = False) -> dict:
    """Point transcript rows at the image they were about.

    ``conversations`` rows that say "[图片]" predate the column, so a renderer
    has nothing to look the description up by.  The media rows have both the
    conversation and the moment, so they can be matched back.
    """
    from sqlalchemy import text as _text

    from core.conversation.media_registry import nearby_resolved_path

    stats = {"scanned": 0, "filled": 0, "unmatched": 0}
    if not meta_db.exists():
        return stats
    engine = create_engine(f"sqlite:///{meta_db}")
    try:
        with engine.connect() as con:
            rows = con.execute(_text(
                "select id, chat_id, created_at from conversations "
                "where content like '%[图片]%' "
                "and (media_path is null or media_path = '')"
            )).fetchall()
        stats["scanned"] = len(rows)
        updates = []
        for row in rows:
            cid, chat_id, created_at = row
            if isinstance(created_at, str):
                try:
                    created_at = datetime.fromisoformat(created_at)
                except ValueError:
                    created_at = None
            path = nearby_resolved_path(media_engine, str(chat_id or ""),
                                        created_at, window_seconds)
            if path:
                updates.append({"i": cid, "p": path})
            else:
                stats["unmatched"] += 1
        if updates and not dry_run:
            with engine.begin() as con:
                for item in updates:
                    con.execute(
                        _text("update conversations set media_path = :p where id = :i"),
                        item,
                    )
        stats["filled"] = len(updates)
    finally:
        engine.dispose()
    return stats


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--db", default="data/platforms/wechat/wxid_seed02/account.db")
    parser.add_argument("--account-id", default="wxid_seed02")
    parser.add_argument("--platform", default="wechat")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"[error] 找不到 {db_path}")
        return 1
    if not args.dry_run:
        run_migrations(str(db_path))
    engine = create_engine(f"sqlite:///{db_path}")

    raw_db = _decrypted_msg_db(args.platform, args.account_id)
    print(f"账号数据库 : {db_path}")
    print(f"解密消息库 : {raw_db}  exists={raw_db.exists()}")
    raw_con = sqlite3.connect(f"file:{raw_db}?mode=ro", uri=True) if raw_db.exists() else None

    limit_clause = f" limit {int(args.limit)}" if args.limit else ""
    with engine.connect() as con:
        # Every image message, not only the ones still missing a path: a media
        # row registered by an earlier run may hold the wrong clues (a row
        # written before the timestamp was carried over says when it was
        # *written*, not when the message was sent, and that breaks the match
        # back to its transcript line).  Re-registering is idempotent and
        # corrects them.
        with_col = con.execute(text(
            "select id, msg_id, local_id, chat_id, sender_id, msg_type_name, "
            "quote_reply_id, created_at "
            "from messages "
            "where msg_type_name = 'image' "
            f"order by id desc{limit_clause}"
        )).fetchall()
        quotes = con.execute(text(
            "select id, msg_id, local_id, chat_id, sender_id, msg_type_name, "
            "quote_reply_id, created_at "
            "from messages "
            "where msg_type_name = 'quote_reply' "
            "and quote_reply_id is not null and quote_reply_id <> '' "
            f"order by id desc{limit_clause}"
        )).fetchall()

    print(f"\n缺失媒体的图片/表情消息 : {len(with_col)} 条")
    print(f"引用类消息               : {len(quotes)} 条")

    registered = recovered = no_md5 = no_raw = register_failed = 0
    for row in with_col:
        mid, msg_id, local_id, chat_id, sender_id, type_name, _q, created_at = row
        kind = _MEDIA_TYPES.get(str(type_name), KIND_IMAGE)
        md5 = ""
        if raw_con is not None:
            table = f"Msg_{session_dir_for(str(chat_id or ''))}"
            md5 = _raw_md5(raw_con, table, int(msg_id or 0),
                           int(local_id) if local_id else None)
        if md5:
            recovered += 1
        elif raw_con is None:
            no_raw += 1
        else:
            no_md5 += 1
        if args.dry_run:
            print(f"  id={mid} msg_id={msg_id} {type_name:8} chat={str(chat_id)[:22]:22} md5={md5 or '(未找到)'}")
            continue
        if register(engine, message_id=int(mid), msg_id=int(msg_id or 0),
                    chat_id=str(chat_id or ""), sender_id=str(sender_id or ""),
                    kind=kind, md5=md5, account_id=args.account_id,
                    platform=args.platform, created_at=created_at):
            registered += 1
        else:
            register_failed += 1

    quote_registered = 0
    for row in quotes:
        mid, msg_id, local_id, chat_id, sender_id, _type_name, quote_ref, created_at = row
        if not quoted_is_media(engine, quote_ref):
            continue
        if args.dry_run:
            print(f"  [引用] id={mid} 引用 {str(quote_ref)[:20]} chat={str(chat_id)[:24]}")
            continue
        if register(engine, message_id=int(mid), msg_id=int(msg_id or 0),
                    chat_id=str(chat_id or ""), sender_id=str(sender_id or ""),
                    kind=KIND_QUOTE_IMAGE, quote_ref=str(quote_ref),
                    account_id=args.account_id, platform=args.platform,
                    created_at=created_at):
            quote_registered += 1
        else:
            register_failed += 1

    if raw_con is not None:
        raw_con.close()

    print(f"\n恢复出 md5 的      : {recovered}")
    print(f"原始行里没有 md5   : {no_md5}")
    print(f"解密库不可用       : {no_raw}")
    if args.dry_run:
        print("(dry run：没有写库)")
        return 0
    print(f"已注册媒体行       : {registered}")
    print(f"已注册引用行       : {quote_registered}")
    if register_failed:
        print(f"注册失败           : {register_failed}  (见上面 media_register_failed)")

    stats = resolve_pending(engine, limit=10000, force=True)
    print(f"\n解析结果: {stats}")

    with engine.connect() as con:
        filled = con.execute(text(
            "select count(*) from messages where media_path is not null "
            "and msg_type_name in ('image','sticker')"
        )).fetchone()[0]
        total = con.execute(text(
            "select count(*) from messages where msg_type_name in ('image','sticker')"
        )).fetchone()[0]
        by_status = con.execute(text(
            "select status, count(*) from message_media group by status"
        )).fetchall()
    print(f"\n图片/表情消息有路径 : {filled}/{total}")
    print(f"message_media 状态  : {[(s, c) for s, c in by_status]}")

    # Finally, point the transcript rows at their images so the reply prompt and
    # the rolling summary can say what a picture showed.
    try:
        from core.config.settings import config

        for meta_db in (Path(str(config.paths.meta_db_path)), db_path):
            if not meta_db.exists() or meta_db == db_path:
                continue
            run_migrations(str(meta_db))
            conv = backfill_conversation_paths(meta_db, engine)
            print(f"会话行回填 {meta_db.name}: {conv}")
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 会话行回填失败: {str(exc)[:140]}")

    engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
