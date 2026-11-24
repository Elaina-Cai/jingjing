#!/usr/bin/env python
"""批0 基线报告：群聊理解相关指标（只读，不改数据、不调模型）。

对应《群聊理解修复方案》（已归档 backups/docs_pruned_*）的「批 0 · 立标尺」：
  1) feedback_samples：correction / repeat 计数（全量 + 近 N 天）
  2) addressee_shadow：影子收件人判定 vs 现状路由（含误接画像 / 该接没接）
  3) terminal_events：近 N 天回复量 + 不确定表达 / 短句反问观测

用法：
  python scripts/understanding_baseline.py
  python scripts/understanding_baseline.py --days 7 --json
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

META_DB = ROOT / "data" / "yinyue_meta.db"
DECISION_TRACE = ROOT / "logs" / "decision_trace.jsonl"

# 宽口径：先观测，批 1 前再收紧成正式行为指标
UNCERTAIN_RE = re.compile(
    r"不确定|不知道|不清楚|没对上|记不清|记不得|说不准|拿不准|不敢保证|"
    r"没印象|不记得|想不起来|啥意思|什么意思|没懂|没看懂|看不懂|懵"
)
SHORT_ASK_RE = re.compile(r"[?？]")


def _connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def feedback_stats(days: int) -> dict:
    con = _connect(META_DB)
    try:
        rows = con.execute(
            "select source, created_at, status from feedback_samples"
        ).fetchall()
    finally:
        con.close()
    cutoff = (datetime.now() - timedelta(days=days)).isoformat(sep=" ", timespec="seconds")

    def _count(items: list) -> dict:
        counter = Counter(str(item[0] or "unknown") for item in items)
        return {
            "total": len(items),
            "correction": counter.get("correction", 0),
            "repeat": counter.get("repeat", 0),
            "by_source": dict(counter),
        }

    recent = [row for row in rows if row[1] and str(row[1])[:19] >= cutoff]
    return {"all": _count(rows), "recent": _count(recent)}


def shadow_stats(path: Path) -> dict:
    from core.conversation.report import load_shadow_rows, summarize_shadow

    rows = load_shadow_rows(path)
    report = summarize_shadow(rows)
    data = report.to_dict()

    legacy_rows = [r for r in rows if (r.get("legacy") or {}).get("should_reply")]
    addressee_dist = Counter(
        str((r.get("shadow") or {}).get("addressee") or "unknown") for r in legacy_rows
    )
    action_dist = Counter(
        str((r.get("shadow") or {}).get("action") or "unknown") for r in legacy_rows
    )
    bot_rows = [
        r
        for r in rows
        if str((r.get("shadow") or {}).get("addressee") or "") == "bot"
    ]
    bot_replied = sum(
        1 for r in bot_rows if (r.get("legacy") or {}).get("should_reply")
    )
    timestamps = [str(r.get("ts")) for r in rows if r.get("ts")]

    return {
        "total": data["total"],
        "window": [min(timestamps), max(timestamps)] if timestamps else [],
        "legacy_reply": data["legacy_reply"],
        "shadow_reply": data["shadow_reply"],
        "agreement_rate": data["agreement_rate"],
        "miss_rate": data["miss_rate"],
        "interjection_rate": data["interjection_rate"],
        "action_matrix": data["action_matrix"],
        "reason_counts": data["reason_counts"],
        "legacy_reply_addressee": dict(addressee_dist),
        "legacy_reply_shadow_action": dict(action_dist),
        "bot_addressed_total": len(bot_rows),
        "bot_addressed_legacy_reply": bot_replied,
        "bot_addressed_legacy_reply_rate": (
            bot_replied / len(bot_rows) if bot_rows else 0.0
        ),
    }


def reply_stats(days: int) -> dict:
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    con = _connect(META_DB)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "select occurred_at, chat_id, content from terminal_events "
            "where event_type='reply' and occurred_at >= ? order by occurred_at",
            (cutoff,),
        ).fetchall()
    finally:
        con.close()

    group = [r for r in rows if "@chatroom" in str(r["chat_id"])]
    private = [r for r in rows if "@chatroom" not in str(r["chat_id"])]

    def _uncertain(items: list) -> dict:
        hits = [r for r in items if UNCERTAIN_RE.search(str(r["content"] or ""))]
        samples = [
            f"{r['occurred_at']} {str(r['content'])[:60]!r}" for r in hits[:8]
        ]
        return {
            "count": len(hits),
            "rate": (len(hits) / len(items)) if items else 0.0,
            "samples": samples,
        }

    def _short_ask(items: list) -> dict:
        hits = [
            r
            for r in items
            if len(str(r["content"] or "")) <= 15
            and SHORT_ASK_RE.search(str(r["content"] or ""))
        ]
        return {"count": len(hits), "rate": (len(hits) / len(items)) if items else 0.0}

    return {
        "days": days,
        "total": len(rows),
        "group": len(group),
        "private": len(private),
        "uncertain": _uncertain(rows),
        "uncertain_group": _uncertain(group),
        "short_ask": _short_ask(rows),
        "short_ask_group": _short_ask(group),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="批0 基线（只读）")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--trace", default=str(DECISION_TRACE))
    args = parser.parse_args()

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "days": args.days,
        "feedback": feedback_stats(args.days),
        "shadow": shadow_stats(Path(args.trace)),
        "replies": reply_stats(args.days),
    }

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    fb, sh, rp = payload["feedback"], payload["shadow"], payload["replies"]
    print(f"[批0 基线] {payload['generated_at']}（近 {args.days} 天，只读）")
    print()
    print("一、反馈样本 feedback_samples")
    print(
        f"  全量: total={fb['all']['total']} "
        f"correction={fb['all']['correction']} repeat={fb['all']['repeat']}"
    )
    print(
        f"  近{args.days}天: total={fb['recent']['total']} "
        f"correction={fb['recent']['correction']} repeat={fb['recent']['repeat']}"
        "（样本少，先作观察项）"
    )
    print()
    print("二、影子收件人判定 addressee_shadow")
    print(f"  样本: {sh['total']} 条  {sh['window']}")
    print(
        f"  legacy 回复 {sh['legacy_reply']} / shadow 回复 {sh['shadow_reply']}  "
        f"agreement={sh['agreement_rate']:.1%} miss={sh['miss_rate']:.1%} "
        f"interjection={sh['interjection_rate']:.1%}"
    )
    print(f"  legacy 回复中的 addressee 分布: {sh['legacy_reply_addressee']}")
    print(f"  legacy 回复中的 shadow 动作分布: {sh['legacy_reply_shadow_action']}")
    print(
        f"  addressee=bot 时 legacy 回复率: "
        f"{sh['bot_addressed_legacy_reply']}/{sh['bot_addressed_total']} = "
        f"{sh['bot_addressed_legacy_reply_rate']:.1%}"
    )
    print()
    print("三、她的回复（terminal_events reply 事件）")
    print(
        f"  近{args.days}天: {rp['total']} 条（群 {rp['group']} / 私聊 {rp['private']}）"
    )
    print(
        f"  不确定表达: 全量 {rp['uncertain']['count']}（{rp['uncertain']['rate']:.1%}）"
        f" / 群 {rp['uncertain_group']['count']}（{rp['uncertain_group']['rate']:.1%}）"
    )
    print(
        f"  短句反问(≤15字含?): 全量 {rp['short_ask']['count']}（{rp['short_ask']['rate']:.1%}）"
        f" / 群 {rp['short_ask_group']['count']}（{rp['short_ask_group']['rate']:.1%}）"
    )
    if rp["uncertain"]["samples"]:
        print("  不确定表达样例:")
        for sample in rp["uncertain"]["samples"]:
            print("   -", sample)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
