"""批 3b 上沉离线审计（只读，不写库、不调 LLM）。

列出「该上沉但还没上沉」的候选项，让人先看看有没有玩笑/一次性黑话混进来
——**红线是误上沉 = 0**。看过没问题，再开 `YINYUE_FEATURE_MEMORY_SINKING`
让提案真正落库（落库也只是 pending，仍要批准）。

用法::

    python scripts/audit_sinking.py                 # 近 14 天
    python scripts/audit_sinking.py --days 30 --min-occurrences 2
    python scripts/audit_sinking.py --json

只读 logs/memory_recall_trace.jsonl + memory_proposals（只查已存在的术语提案）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

if os.name == "nt":
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description="批 3b 上沉候选离线审计（只读）")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--min-occurrences", type=int, default=3)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    from core.quality.memory_sinking import (
        load_records,
        collect_candidates,
        load_tool_action_rows,
        collect_tool_candidates,
        merge_candidates,
        MIN_SCORE,
    )

    records = load_records(days=args.days)
    real = [r for r in records if not _is_fix(r)]
    recall = collect_candidates(
        real, min_occurrences=args.min_occurrences, min_score=MIN_SCORE
    )
    tool = collect_tool_candidates(
        load_tool_action_rows(days=args.days), min_occurrences=args.min_occurrences
    )
    candidates = merge_candidates(recall, tool)

    print(f"召回记录 {len(records)} 条（其中真实流量 {len(real)} 条，夹具 {len(records) - len(real)} 条）")
    print(f"命中分下限 {MIN_SCORE}，重复门槛 ≥{args.min_occurrences} 次\n")

    if not candidates:
        print("（没有达到门槛的候选——要么没有反复检索，要么都是夹具）")
    for c in candidates:
        print(
            f"- [{c['chat_id']}] 「{c['term']}」×{c['occurrences']}"
            f"  最高分 {c['best_score']:.2f}  依据 {c['memory_ids'][:3]}"
        )

    print(
        "\n人工核对：每个词条是不是**真的**反复被查、且值得沉淀？"
        "（玩笑/一次性黑话混进来 = 误上沉，红线，必须为 0）"
    )

    if args.json:
        out = os.path.join(os.path.dirname(__file__), "audit_sinking_out.json")
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(
                {"days": args.days, "records": len(records), "real": len(real), "candidates": candidates},
                fh,
                ensure_ascii=False,
                indent=2,
            )
        print(f"JSON → {out}")
    return 0


def _is_fix(record: dict) -> bool:
    from core.quality.memory_sinking import is_fixture

    return is_fixture(record)


if __name__ == "__main__":
    raise SystemExit(main())
