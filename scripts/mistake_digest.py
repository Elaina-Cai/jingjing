"""把 feedback_samples 聚成"她常犯的错"（P3-1，只读）。

用法：
    python scripts/mistake_digest.py                # 最近 30 天，出现 >=2 次的形状
    python scripts/mistake_digest.py --days 90 --min-count 3
    python scripts/mistake_digest.py --json

只开 `mode=ro` 的只读连接，**一个字节都不会写**。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.quality import mistake_digest as md  # noqa: E402

DEFAULT_DB = os.path.join(ROOT, "data", "yinyue_meta.db")


def _rows(db: str, days: int) -> list[dict]:
    uri = "file:%s?mode=ro" % db.replace("\\", "/")
    con = sqlite3.connect(uri, uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        sql = (
            "select id, source, chat_id, sample_text, previous_user_text, reply_preview, created_at "
            "from feedback_samples "
        )
        params: tuple = ()
        if days > 0:
            sql += "where created_at >= datetime('now', ?) "
            params = ("-%d days" % int(days),)
        sql += "order by id desc"
        return [dict(r) for r in con.execute(sql, params)]
    finally:
        con.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="她常犯的错（只读汇总）")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--min-count", type=int, default=2)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if not os.path.exists(args.db):
        print("没有库：%s" % args.db)
        return 1
    rows = _rows(args.db, args.days)
    digest = md.build_digest(rows, min_count=args.min_count)
    if args.json:
        print(json.dumps(digest, ensure_ascii=False, indent=2))
        return 0

    print("窗口：最近 %d 天（0=全部）  样本：%d 条  %s" % (args.days, digest["total"], digest["by_source"]))
    if not digest["clusters"]:
        print("没有出现 >= %d 次的形状。" % args.min_count)
        return 0
    print("")
    print("%-4s %-6s %-24s %-26s %s" % ("次数", "来源", "形状（归一化前 24 字）", "归类", "时间跨度"))
    for c in digest["clusters"]:
        print("%-4d %-6s %-24s %-26s %s → %s" % (
            c["count"], c["source"], c["shape"][:24], c["label"],
            (c["first_at"] or "")[:16], (c["last_at"] or "")[:16]))
    print("")
    print("按归类累计：%s" % digest["by_code"])
    print("")
    print("最该先修的三件：")
    for i, c in enumerate(digest["top"], 1):
        print("  %d. [%s] %d 次  %s" % (i, c["label"], c["count"], c["shape"][:40]))
        print("     主人原话：%s" % (c["example_text"] or "")[:80].replace("\n", " "))
        print("     她当时回：%s" % ((c["example_reply"] or "（没回）")[:80].replace("\n", " ")))
    print("")
    for n in digest["notes"]:
        print("· %s" % n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
