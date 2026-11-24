#!/usr/bin/env python
"""L2 准入看板：读 logs/admission_trace.jsonl（shadow / on 均可）。

用法：
  python scripts/admission_board.py
  python scripts/admission_board.py --days 2 --json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRACE = ROOT / "logs" / "decision_trace.jsonl"


def load_records(days: int) -> list[dict]:
    if not TRACE.exists():
        return []
    since = datetime.now() - timedelta(days=max(1, days))
    out: list[dict] = []
    for line in TRACE.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except Exception:
            continue
        if str(record.get("kind") or "") != "admission_detail":
            continue
        at = str(record.get("at") or "")
        try:
            if datetime.fromisoformat(at) < since:
                continue
        except Exception:
            pass
        out.append(record)
    return out


def percentile(values: list[int], ratio: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(ratio * (len(ordered) - 1)))))
    return int(ordered[index])


def _field(record: dict, section: str, key: str, default=None):
    """读三段结构（B5c）里的字段，回退到旧的扁平键。"""
    block = record.get(section)
    if isinstance(block, dict) and key in block:
        return block[key]
    return record.get(key, default)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=1)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    records = load_records(args.days)

    summary = {
        "days": args.days,
        "total": len(records),
        "modes": dict(Counter(str(r.get("mode")) for r in records)),
        "fallbacks": dict(Counter(str(_field(r, "judgment", "fallback")) for r in records)),
        "chats": dict(Counter(str(r.get("chat_id")) for r in records).most_common(5)),
        "labels": {"A": 0, "B": 0, "C": 0},
        "applied": sum(1 for r in records if _field(r, "result", "applied")),
        "judge_ms_p50": percentile(
            [int(_field(r, "judgment", "judge_ms") or 0)
             for r in records if _field(r, "judgment", "fallback") == "judge"], 0.5
        ),
        "judge_ms_p95": percentile(
            [int(_field(r, "judgment", "judge_ms") or 0)
             for r in records if _field(r, "judgment", "fallback") == "judge"], 0.95
        ),
    }
    for record in records:
        for key, value in (_field(record, "judgment", "labels") or {}).items():
            if key in summary["labels"]:
                summary["labels"][key] += int(value or 0)

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    print(f"L2 准入看板（近 {args.days} 天）")
    print(f"  记录数: {summary['total']}｜模式: {summary['modes']}｜兜底: {summary['fallbacks']}")
    print(f"  标签分布: {summary['labels']}｜实际替换全窗: {summary['applied']}｜"
          f"judge 延迟 p50/p95: {summary['judge_ms_p50']}/{summary['judge_ms_p95']} ms")
    print(f"  会话 top: {summary['chats']}")
    if records:
        print("  最近 5 条:")
        for record in records[-5:]:
            print(
                f"    {str(record.get('at'))[:19]} {record.get('chat_id')} "
                f"[{_field(record, 'judgment', 'fallback')}] "
                f"labels={_field(record, 'judgment', 'labels')} "
                f"scene={str(_field(record, 'judgment', 'scene'))[:30]!r}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
