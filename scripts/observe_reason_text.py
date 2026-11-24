#!/usr/bin/env python
"""renderer 观察窗采样器（只读）：真实 prompt 里的 [触发原因] 块。

数据源（有哪个读哪个）：
  * logs/context_trace.jsonl    旧 context 子 trace（blocks[].name 即块正文）
  * logs/decision_trace.jsonl   8.1d 合并后的 context_detail 行（同样有 blocks[].name）

用法：
  python scripts/observe_reason_text.py                 # 最近 24h
  python scripts/observe_reason_text.py --hours 6
  python scripts/observe_reason_text.py --all --limit 20
  python scripts/observe_reason_text.py --since 2026-10-10T12:00:00
  python scripts/observe_reason_text.py --json

红项（退出码 1）：仍出现 raw reason token、旧 trigger 英文 label、旧触发模板。
服务重启后用 --since 传入重启时间，只看新代码产生的行。
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES = (
    ROOT / "logs" / "context_trace.jsonl",
    ROOT / "logs" / "decision_trace.jsonl",
)
EVENT_LOG = ROOT / "logs" / "yinyue.log"

RAW_TOKENS = (
    "mentioned_in_group", "patted_in_group", "quoted_in_group", "name_call",
    "patted_in_private", "owner_private_message", "non_owner_private_urgent",
    "web_chat", "conversation_active", "engagement_fallback",
    "group_deferred_to_state", "ignored_group", "content",
)
OLD_LABELS = ("immediate", "ambient", "deferred", "pat", "none")
TRIGGER_HUMAN = ("进了环境接话", "先挂起，等状态变化", "没有触发她", "@ 了她", "拍了拍她")
DIRECT_HUMAN = (
    "有人", "主人私聊了她", "非主人私聊", "对话仍在进行", "网页端",
    "这条还带了内容", "群里的", "延迟窗口", "消息过期", "她自己发的", "不是冲她来的",
)


def _parse_at(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _iter_rows(path):
    if not path.exists():
        return
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if "触发原因" not in line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            yield row


def _trigger_blocks(row):
    for block in row.get("blocks") or ():
        if not isinstance(block, dict):
            continue
        name = str(block.get("name") or "")
        if name.startswith("[触发原因"):
            yield name


def _classify(text):
    tags = []
    inner = text[len("[触发原因:"):].strip().rstrip("]").strip()
    if any("触发了" + label in text for label in OLD_LABELS):
        tags.append("old_label")
    if "触发了" in text:
        tags.append("old_template")
    if any(tok in inner for tok in RAW_TOKENS) or "+" in inner:
        tags.append("raw_token")
    if any(mark in text for mark in TRIGGER_HUMAN):
        tags.append("trigger_human")
    if any(mark in text for mark in DIRECT_HUMAN):
        tags.append("direct_human")
    if not tags:
        tags.append("unknown")
    return tags


def _recent_events(since):
    counts = Counter()
    if not EVENT_LOG.exists():
        return counts
    keys = ('"trigger_reason"', '"route_will_reply"', '"route_no_reply"', '"group_unit_sealed"')
    with EVENT_LOG.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not any(k in line for k in keys):
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            at = _parse_at(row.get("timestamp") or row.get("ts"))
            if since and at and at < since:
                continue
            reason = str(row.get("trigger_reason") or row.get("reason") or "-")
            counts[(str(row.get("event") or "-"), reason)] += 1
    return counts


def main():
    parser = argparse.ArgumentParser(description="renderer 观察窗采样器（只读）")
    parser.add_argument("--hours", type=float, default=24.0)
    parser.add_argument("--since", default="", help="ISO 起始时间，覆盖 --hours")
    parser.add_argument("--all", action="store_true", help="不按时间过滤")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.all:
        since = None
    elif args.since:
        since = _parse_at(args.since)
    else:
        since = datetime.now() - timedelta(hours=max(0.0, args.hours))

    samples, text_counts, tag_counts = [], Counter(), Counter()
    for path in SOURCES:
        for row in _iter_rows(path):
            at = _parse_at(row.get("at") or row.get("ts"))
            if since and at and at < since:
                continue
            for text in _trigger_blocks(row):
                tags = _classify(text)
                samples.append({
                    "at": str(row.get("at") or row.get("ts") or ""),
                    "chat_id": str(row.get("chat_id") or ""),
                    "source": path.name,
                    "text": text,
                    "tags": tags,
                })
                text_counts[text] += 1
                for tag in tags:
                    tag_counts[tag] += 1

    red = [s for s in samples if {"raw_token", "old_label", "old_template"} & set(s["tags"])]
    event_counts = _recent_events(since)

    if args.json:
        print(json.dumps({
            "since": since.isoformat() if since else None,
            "blocks": len(samples),
            "distinct_texts": len(text_counts),
            "tags": dict(tag_counts),
            "red_count": len(red),
            "top": text_counts.most_common(20),
            "events": [{"event": k[0], "reason": k[1], "n": v} for k, v in event_counts.most_common(20)],
            "samples": samples[-max(1, args.limit):],
        }, ensure_ascii=False, indent=2))
    else:
        print("窗口起点:", since.isoformat(sep=" ") if since else "(全部)")
        print("命中 [触发原因] 块: %d  | distinct: %d" % (len(samples), len(text_counts)))
        print("标签:", dict(tag_counts))
        print("红项(raw/old_label/old_template):", len(red))
        print()
        print("-- 出现最多的触发原因 --")
        for text, n in text_counts.most_common(15):
            print("%5d  %s" % (n, text))
        print()
        print("-- 最近样例（最多 %d 条）--" % args.limit)
        for s in samples[-max(1, args.limit):]:
            print("  %s  %-22s %s  [%s]" % (s["at"][:19], s["chat_id"][:22], s["text"], ",".join(s["tags"])))
        if event_counts:
            print()
            print("-- 决策事件（yinyue.log，供对量）--")
            for (ev, reason), n in event_counts.most_common(10):
                print("%5d  %-24s %s" % (n, ev, reason))
        if red:
            print()
            print("!! 红项样例：")
            for s in red[-10:]:
                print("  %s  %s  [%s]" % (s["at"][:19], s["text"], ",".join(s["tags"])))
    return 1 if red else 0


if __name__ == "__main__":
    raise SystemExit(main())
