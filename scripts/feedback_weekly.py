#!/usr/bin/env python
"""Weekly correction/repeat report for the Phase 2 business acceptance.

Reads ``feedback_samples`` (read-only) and prints per-week counts plus the
delta against the previous week.  This is the metric that decides whether the
context-budget change (YINYUE_FEATURE_CONTEXT_BUDGET) stays or is rolled back.

    python scripts/feedback_weekly.py
    python scripts/feedback_weekly.py --weeks 8
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

META_DB = ROOT / "data" / "yinyue_meta.db"


def _week_key(moment: datetime) -> str:
    iso = moment.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--weeks", type=int, default=4)
    parser.add_argument("--meta-db", default=str(META_DB))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    path = Path(args.meta_db)
    if not path.exists():
        print(f"[err] meta db not found: {path}")
        return 2

    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "select source, created_at, status from feedback_samples"
        ).fetchall()
    except sqlite3.OperationalError as exc:
        print(f"[err] feedback_samples unavailable: {exc}")
        return 2
    finally:
        con.close()

    weeks: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for source, created_at, _status in rows:
        if not created_at:
            continue
        try:
            moment = datetime.fromisoformat(str(created_at))
        except ValueError:
            continue
        weeks[_week_key(moment)][str(source or "unknown")] += 1

    ordered = sorted(weeks)[-max(1, int(args.weeks or 4)):]
    payload = []
    previous_total = None
    for week in ordered:
        counts = dict(weeks[week])
        total = sum(counts.values())
        delta = "" if previous_total is None else f"{total - previous_total:+d}"
        payload.append(
            {"week": week, "correction": counts.get("correction", 0),
             "repeat": counts.get("repeat", 0), "total": total, "delta": delta}
        )
        previous_total = total

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"{'week':>10} | {'correction':>10} | {'repeat':>6} | {'total':>5} | delta")
    print("-" * 52)
    for row in payload:
        print(
            f"{row['week']:>10} | {row['correction']:>10} | {row['repeat']:>6} "
            f"| {row['total']:>5} | {row['delta']}"
        )
    if not payload:
        print("(还没有样本——先用起来，纠正/重复问会自动被采集)")
    else:
        print(
            "\n判断：correction/repeat 周总数下降 = 上下文预算有效；"
            "不降或上升 = 回滚 YINYUE_FEATURE_CONTEXT_BUDGET。"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
