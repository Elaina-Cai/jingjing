# -*- coding: utf-8 -*-
"""说话质感读数收数（批D 路线 6）。

用法：
  .venv_it\\Scripts\\python.exe scripts\\report_human_tone.py --log logs/yinyue.log --since 2026-10-14T12:00
"""
from __future__ import annotations

import argparse
import collections
import json
import pathlib


def _iter_events(log_path: str, since: str = ""):
    for line in pathlib.Path(log_path).read_text(encoding="utf-8", errors="replace").splitlines():
        if "human_tone_shadow" not in line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if since and str(obj.get("timestamp", "")) < since:
            continue
        yield obj


def collect(events) -> dict:
    total = 0
    model_like = 0
    flags = collections.Counter()
    colloquial = collections.Counter()
    short_ratios = []
    emoji = 0
    for obj in events:
        total += 1
        if obj.get("model_like"):
            model_like += 1
        for flag in obj.get("flags") or ():
            flags[str(flag)] += 1
        if int(obj.get("colloquial_n") or 0) > 0:
            colloquial["has"] += 1
        try:
            short_ratios.append(float(obj.get("short_ratio") or 0.0))
        except (TypeError, ValueError):
            pass
        if obj.get("emoji"):
            emoji += 1
    avg_short = round(sum(short_ratios) / len(short_ratios), 3) if short_ratios else 0.0
    return {
        "total": total,
        "model_like": model_like,
        "model_like_rate": round(model_like / total, 3) if total else 0.0,
        "flags": dict(flags),
        "colloquial": dict(colloquial),
        "avg_short_ratio": avg_short,
        "emoji": emoji,
        "gates": {
            "model_like_rate": f"{round(model_like / total, 3) if total else 0.0}/0.10",
            "samples": f"{total}/30",
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default="logs/yinyue.log")
    ap.add_argument("--since", default="")
    ap.add_argument("--json", default="")
    args = ap.parse_args()
    report = collect(_iter_events(args.log, args.since))
    if args.json:
        pathlib.Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
