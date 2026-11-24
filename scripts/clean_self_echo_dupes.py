# -*- coding: utf-8 -*-
"""一次性清理：terminal_events 里自我回声的双份投影（受控、可 dry-run）。

同一句回复会被写两次：发送侧一条 ``reply``（带事件链，没有 external_msg_id），
微信同步回来的 ``is_self`` 一条 ``message``（带 external_msg_id，没有父链）。
写入侧 2026-10-16 起按 ``core.terminal.shadow._same_echo_body`` 合并，读侧
（``core/context/chat_window._dedupe_self_echoes``）也按同一判据去重；本脚本只
处理**存量**：把 external_msg_id 回填给 reply 行，再删掉冗余的 message 行
（消息行无子行引用时才删）。

用法：
  .venv_it/Scripts/python.exe scripts/clean_self_echo_dupes.py --dry-run
  .venv_it/Scripts/python.exe scripts/clean_self_echo_dupes.py --apply --limit 27
"""
from __future__ import annotations

import argparse
import re
import sqlite3
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "yinyue_meta.db"
WINDOW_SECONDS = 900
SCHEDULER_LOCK = ROOT / "logs" / "scheduler.lock"

_MENTION_RE = re.compile(
    r"^[\u200b\u200c\u200d\u2060\ufeff]*@[^\s@\u2005]+[\u200b\u200c\u200d\u2060\ufeff\u2005]*"
)


def _strip_mentions(text: object) -> str:
    body = str(text or "").lstrip("\u200b\u200c\u200d\u2060\ufeff \t\u2005")
    for _ in range(4):
        m = _MENTION_RE.match(body)
        if m is None:
            break
        body = body[m.end():]
    return body


def _same_body(one: str, two: str) -> bool:
    left, right = _strip_mentions(one).strip(), _strip_mentions(two).strip()
    if not left or not right:
        return False
    if left == right:
        return True
    short, long = (left, right) if len(left) <= len(right) else (right, left)
    return len(short) >= 8 and long.startswith(short)


def _ts(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except Exception:
        return None


def find_pairs(conn: sqlite3.Connection) -> list[tuple[sqlite3.Row, sqlite3.Row]]:
    rows = conn.execute(
        "select id, event_id, event_type, occurred_at, chat_id, content, "
        "coalesce(external_msg_id,'') as ext, coalesce(parent_event_id,'') as parent "
        "from terminal_events where event_type in ('reply','message') order by occurred_at, sequence"
    ).fetchall()
    pairs: list[tuple[sqlite3.Row, sqlite3.Row]] = []
    for index, row in enumerate(rows):
        if row["event_type"] != "message" or not row["ext"]:
            continue
        if len(_strip_mentions(row["content"]).strip()) < 8:
            continue
        for back in range(max(0, index - 30), index):
            prev = rows[back]
            if prev["event_type"] != "reply" or prev["chat_id"] != row["chat_id"]:
                continue
            if not _same_body(row["content"], prev["content"]):
                continue
            one, two = _ts(row["occurred_at"]), _ts(prev["occurred_at"])
            if one is None or two is None or abs((one - two).total_seconds()) > WINDOW_SECONDS:
                continue
            pairs.append((prev, row))
            break
    return pairs


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--force", action="store_true", help="库被占用时也继续（默认拒绝）")
    args = parser.parse_args()
    db = Path(args.db)
    if not db.exists():
        print("数据库不存在:", db)
        return 2
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.execute("pragma busy_timeout = 5000")
    pairs = find_pairs(conn)
    if args.limit:
        pairs = pairs[: args.limit]
    print("识别到自我回声双份对:", len(pairs))
    blocked = 0
    for reply, message in pairs:
        kids = conn.execute(
            "select count(*) from terminal_events where parent_event_id = ?",
            (message["event_id"],),
        ).fetchone()[0]
        tag = "子行=%d" % kids if kids else "无子行"
        print(
            "  reply id=%s ext=%s parent=%s | message id=%s ext=%s %s -> %s"
            % (
                reply["id"], reply["ext"] or "-", "有" if reply["parent"] else "无",
                message["id"], message["ext"], tag,
                "回填+删 message" if (kids == 0 and not reply["ext"]) else "跳过（需人工核）",
            )
        )
        if kids or reply["ext"]:
            blocked += 1
    print("可自动处理:", len(pairs) - blocked, "| 需人工:", blocked)
    if not args.apply:
        print("（dry-run，未改动；加 --apply 执行）")
        return 0
    if SCHEDULER_LOCK.exists() and not args.force:
        print("logs/scheduler.lock 存在 = 生产调度器在跑；先停进程或用 --force")
        return 3
    changed = 0
    for reply, message in pairs:
        kids = conn.execute(
            "select count(*) from terminal_events where parent_event_id = ?",
            (message["event_id"],),
        ).fetchone()[0]
        if kids or reply["ext"]:
            continue
        # 顺序要紧：先删冗余 message 行再回填，否则 reply 行写 external_msg_id
        # 会撞 (platform, bot_account_id, chat_id, external_msg_id) 唯一索引
        # （那条 message 行还占着这个四元组）。两条同事务，失败一起回滚。
        conn.execute("delete from terminal_events where id = ?", (message["id"],))
        conn.execute(
            "update terminal_events set external_msg_id = ? where id = ? and coalesce(external_msg_id,'') = ''",
            (message["ext"], reply["id"]),
        )
        changed += 1
    conn.commit()
    print("已处理:", changed, "对（external_msg_id 回填给 reply，冗余 message 行删除）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
