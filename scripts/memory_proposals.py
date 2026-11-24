#!/usr/bin/env python
"""记忆沉淀提案 CLI（P2，docs/记忆的定义.md §3）。提案制：批准才写备注。

    python scripts/memory_proposals.py --propose --chat 10000000002@chatroom --hours 24
    python scripts/memory_proposals.py --list
    python scripts/memory_proposals.py --approve 3
    python scripts/memory_proposals.py --approve 3 --content "改过的说法"
    python scripts/memory_proposals.py --reject 3

只写 memory_proposals / person_notes 两张表；不写事件、不发消息。

归属：沉淀提案审阅入口（--propose/--list/--approve/--reject/--revoke）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.quality.memory_proposals import (  # noqa: E402
    SLOTS,
    approve,
    list_proposals,
    revoke,
    propose_from_chat,
    reject,
)


def _evidence_text(raw: str) -> str:
    """证据字段的展示：新版是 {"ids": [...], "text": "..."}，旧版是 ["ev1", ...]。"""
    import json

    try:
        data = json.loads(raw or "[]")
    except Exception:
        return ""
    if isinstance(data, dict):
        text = str(data.get("text") or "").strip()
        ids = data.get("ids") or []
        if text:
            return f'「{text}」（{len(ids)} 条证据）'
        return f"（{len(ids)} 条证据）" if ids else ""
    if isinstance(data, list):
        return f"（{len(data)} 条证据）" if data else ""
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--propose", action="store_true")
    ap.add_argument("--chat", default="")
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--limit", type=int, default=60)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--status", default="pending")
    ap.add_argument("--approve", type=int)
    ap.add_argument("--reject", type=int)
    ap.add_argument("--revoke", type=int)
    ap.add_argument("--content", default="")
    args = ap.parse_args()

    if args.propose:
        if not args.chat:
            print("--propose 需要 --chat")
            return 2
        from core.bootstrap import get_llm

        stats = asyncio.run(
            propose_from_chat(
                args.chat, hours=args.hours, limit=args.limit, llm=get_llm()
            )
        )
        print(f"事件 {stats['events']} 条 → 候选 {stats['raw']} 条 → 落库 {stats['staged']} 条")
        if stats["staged"]:
            print("用 --list 查看，用 --approve/--reject 处置。")
        return 0

    if args.approve is not None:
        ok = approve(args.approve, content=args.content)
        print("已批准并写入备注" if ok else "批准失败（不存在或已处置）")
        return 0 if ok else 1

    if args.reject is not None:
        ok = reject(args.reject)
        print("已驳回" if ok else "驳回失败（不存在或已处置）")
        return 0 if ok else 1

    if args.revoke is not None:
        ok = revoke(args.revoke)
        print("已撤回（备注里那段已删除）" if ok else "撤回失败（不存在或不是已批准状态）")
        return 0 if ok else 1

    rows = list_proposals(status=args.status, chat_id=args.chat)
    if not rows:
        print("(无提案)")
        return 0
    for row in rows:
        slot_label = SLOTS.get(str(row["slot"]), row["slot"])
        who = row["subject_name"] or "(整段对话)"
        print(
            f"#{row['id']:<4} [{row['status']:8}] [{slot_label}] {who}：{row['content']}"
            f"   ← {row['reason']} | {row['chat_id']} | {row['created_at']}"
        )
        ev = _evidence_text(row.get("evidence_json") or "")
        if ev:
            print(f"        证据 {ev}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
