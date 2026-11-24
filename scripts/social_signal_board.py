#!/usr/bin/env python
"""社交信号看板（P4.1）：把「她的消息被撤回」变成可看的信号。

原理（2026-09-23 调研）：
  * 微信把撤回写成 `<sysmsg type="revokemsg">` 的消息（msg_type=10000/system），
    已能同步进 `messages` 与 `terminal_events`；
  * `<content>你撤回了一条消息</content>` = **她账号的消息被撤**（主人操作的
    无声反馈——她说了不该说的）；`X 撤回了一条消息` = 别人撤自己的，跳过；
  * `<revoketime>` 是撤回时刻（Unix 秒，本地时区）；
  * 被撤回的那条 ≈ 撤回时刻前（窗口默认 180s）同会话里最后一条她的
    question/reply（排除 revokemsg 自身）。

用法：
  python scripts/social_signal_board.py                 # 近 1 天，dry-run 清单
  python scripts/social_signal_board.py --days 3 --json
  python scripts/social_signal_board.py --window 300

只读（本版不写库；写入 feedback_samples 的格式待 P4.2 设计）。
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

META_DB = ROOT / "data" / "yinyue_meta.db"
PLATFORM_DIR = ROOT / "data" / "platforms" / "wechat"

_CONTENT_RE = re.compile(r"<content>([^<]*)</content>")
_REVOKE_TIME_RE = re.compile(r"<revoketime>(\d+)</revoketime>")

# 粗分类提示：撤回内容里常见的问题关键词（只做标记，不做判定）
_FLAG_WORDS = ("主人", "账", "债", "分", "锅", "跑腿", "盯", "签", "撤")


def _account_dbs() -> list[Path]:
    if not PLATFORM_DIR.exists():
        return []
    out = []
    for account_dir in sorted(PLATFORM_DIR.iterdir()):
        db = account_dir / "account.db"
        if db.exists():
            out.append(db)
    return out


def _meta_events(chat_id: str, lo: str, hi: str) -> list[tuple]:
    con = sqlite3.connect(f"file:{META_DB}?mode=ro", uri=True)
    try:
        return con.execute(
            "select occurred_at, event_type, id, content from terminal_events "
            "where chat_id = ? and occurred_at >= ? and occurred_at <= ? "
            "and event_type in ('question','reply') "
            "and content not like '[引用%revokemsg%' "
            "and content not like '{%' "
            "order by occurred_at desc limit 5",
            (chat_id, lo, hi),
        ).fetchall()
    finally:
        con.close()


def _looks_like_revoke_row(content: str) -> bool:
    return "revokemsg" in str(content or "")


def collect(days: int, window: int) -> list[dict]:
    since = (datetime.now().timestamp() - max(1, days) * 86400)
    hits: list[dict] = []
    for db in _account_dbs():
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            rows = con.execute(
                "select created_at, chat_id, content from messages "
                "where content like '%revokemsg%' and created_at >= ? "
                "order by created_at",
                (datetime.fromtimestamp(since).strftime("%Y-%m-%d %H:%M:%S"),),
            ).fetchall()
            con.close()
        except sqlite3.Error:
            continue
        for created_at, chat_id, content in rows:
            m = _CONTENT_RE.search(content or "")
            who = m.group(1) if m else ""
            if "你撤回了" not in who:
                continue
            rm = _REVOKE_TIME_RE.search(content or "")
            if not rm or int(rm.group(1)) <= 0:
                continue
            rtime = int(rm.group(1))
            moment = datetime.fromtimestamp(rtime)
            lo = datetime.fromtimestamp(rtime - max(5, window)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            hi = datetime.fromtimestamp(rtime + 3).strftime("%Y-%m-%d %H:%M:%S")
            candidates = [
                row
                for row in _meta_events(str(chat_id), lo, hi)
                if not _looks_like_revoke_row(row[3])
            ]
            pick = candidates[0] if candidates else None
            flags = [
                word
                for word in _FLAG_WORDS
                if pick is not None and word in str(pick[3] or "")
            ]
            hits.append(
                {
                    "account_db": db.parent.name,
                    "revoked_at": moment.strftime("%Y-%m-%d %H:%M:%S"),
                    "chat_id": str(chat_id),
                    "delay_seconds": (
                        int(
                            (
                                moment
                                - datetime.fromisoformat(str(pick[0]))
                            ).total_seconds()
                        )
                        if pick
                        else None
                    ),
                    "content": str(pick[3]) if pick else "",
                    "event_id": int(pick[2]) if pick else None,
                    "event_type": str(pick[1]) if pick else "",
                    "flags": flags,
                }
            )
    hits.sort(key=lambda item: item["revoked_at"])
    return hits


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=1)
    parser.add_argument("--window", type=int, default=300, help="匹配窗口（秒）")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    hits = collect(args.days, args.window)
    if args.json:
        print(json.dumps({"revokes": hits}, ensure_ascii=False, indent=2))
        return 0
    print(f"[social_signal] 近 {args.days} 天 · 「她的消息被撤回」{len(hits)} 条")
    for hit in hits:
        delay = f"{hit['delay_seconds']}s" if hit["delay_seconds"] is not None else "?"
        if hit["delay_seconds"] is not None and hit["delay_seconds"] > 300:
            delay += "（远距，低置信）"
        flags = (" ⚑ " + "/".join(hit["flags"])) if hit["flags"] else ""
        print(f"  [{hit['revoked_at'][5:16]}] chat={hit['chat_id'][:18]} 延迟={delay}{flags}")
        text = " ".join(str(hit["content"]).split())
        print(f"      {text[:80] or '（未匹配到内容）'}")
    matched = sum(1 for hit in hits if hit["content"])
    print(f"  匹配到内容的：{matched}/{len(hits)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
