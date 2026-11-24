# -*- coding: utf-8 -*-
r"""回填引用内容：把库里被写成 [媒体消息] 的「文字引用」修回真实文本。

背景：``refermsg.content`` 是被引用消息的 XML 时，旧解析一律写成
``[媒体消息]``（见 core/conversation/quote_text.py 头注）。本脚本按
``quote_reply_id`` 找回被引用那条消息的真实文本回填 account.db 的
``quote_reply_content`` / ``content``，并同步修正 meta.db 的
``terminal_events.content``（按 external_msg_id 对齐）。

用法：
  .venv_it\Scripts\python.exe scripts\backfill_quote_content.py            # dry-run（默认）
  .venv_it\Scripts\python.exe scripts\backfill_quote_content.py --apply
"""
from __future__ import annotations

import argparse
import glob
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.conversation.quote_text import clean_quote_text  # noqa: E402

PLACEHOLDER = "[媒体消息]"
QUOTE_IN_CONTENT = re.compile(r"(\[引用 [^\]]*?: )\[媒体消息\](\])")


def _fix_content(content: str, new_quote: str) -> str:
    if not content:
        return content
    return QUOTE_IN_CONTENT.sub(lambda m: m.group(1) + new_quote + m.group(2), content, count=1)


def backfill_account(path: Path, *, apply: bool, limit: int) -> dict:
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, msg_id, content, quote_reply_id, quote_reply_content FROM messages "
        "WHERE content LIKE ? AND quote_reply_id IS NOT NULL AND quote_reply_id != '' "
        "LIMIT ?",
        ("%" + PLACEHOLDER + "%", limit),
    ).fetchall()
    updates: dict[str, str] = {}
    report = {"path": str(path), "candidates": len(rows), "resolved": 0, "unresolved": 0, "samples": []}
    for row in rows:
        ref = con.execute(
            "SELECT content FROM messages WHERE msg_id = ? LIMIT 1", (row["quote_reply_id"],)
        ).fetchone()
        if not ref or not str(ref["content"] or "").strip():
            report["unresolved"] += 1
            continue
        new_quote = clean_quote_text(str(ref["content"]))
        if not new_quote or new_quote == PLACEHOLDER:
            report["unresolved"] += 1
            continue
        new_content = _fix_content(str(row["content"] or ""), new_quote)
        old_quote = str(row["quote_reply_content"] or "")
        if old_quote and old_quote != PLACEHOLDER:
            new_quote = old_quote
        report["resolved"] += 1
        if len(report["samples"]) < 5:
            report["samples"].append({
                "msg_id": row["msg_id"],
                "before": str(row["content"] or "")[:70],
                "after": new_content[:90],
            })
        updates[str(row["msg_id"])] = new_content
        if apply:
            con.execute(
                "UPDATE messages SET quote_reply_content = ?, content = ? WHERE id = ?",
                (new_quote, new_content, row["id"]),
            )
    if apply:
        con.commit()
    con.close()
    report["updates"] = updates
    return report


def backfill_terminal(meta_path: Path, updates: dict[str, str], *, apply: bool) -> dict:
    report = {"path": str(meta_path), "matched": 0, "updated": 0}
    if not updates or not meta_path.exists():
        return report
    con = sqlite3.connect(str(meta_path))
    con.row_factory = sqlite3.Row
    for msg_id, content in updates.items():
        row = con.execute(
            "SELECT id, content FROM terminal_events WHERE external_msg_id = ? LIMIT 1", (msg_id,)
        ).fetchone()
        if not row:
            continue
        report["matched"] += 1
        if PLACEHOLDER in str(row["content"] or ""):
            report["updated"] += 1
            if apply:
                con.execute(
                    "UPDATE terminal_events SET content = ? WHERE id = ?", (content, row["id"])
                )
    if apply:
        con.commit()
    con.close()
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--accounts", default="data/platforms/wechat/*/account.db")
    ap.add_argument("--meta", default="data/yinyue_meta.db")
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    totals = {"candidates": 0, "resolved": 0, "unresolved": 0}
    all_updates: dict[str, str] = {}
    for path_str in sorted(glob.glob(args.accounts)):
        report = backfill_account(Path(path_str), apply=args.apply, limit=args.limit)
        totals["candidates"] += report["candidates"]
        totals["resolved"] += report["resolved"]
        totals["unresolved"] += report["unresolved"]
        all_updates.update(report.get("updates", {}))
        print(f"[account] {path_str} candidates={report['candidates']} resolved={report['resolved']} unresolved={report['unresolved']}")
        for sample in report["samples"]:
            print("   ", sample)
    term = backfill_terminal(Path(args.meta), all_updates, apply=args.apply)
    print(f"[terminal] matched={term['matched']} updated={term['updated']}")
    print(f"[totals] {totals} apply={args.apply}")
    if not args.apply:
        print("dry-run only; pass --apply to write")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
