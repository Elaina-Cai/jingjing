# -*- coding: utf-8 -*-
"""转正闸门总表（批D 路线 7）：把各影子件的收数汇成一张表，按门槛给 ready。

用法：
  .venv_it\\Scripts\\python.exe scripts\\report_gates.py --log logs/yinyue.log --since 2026-10-14T12:00
"""
from __future__ import annotations

import argparse
import collections
import json
import pathlib

WATCH = (
    "human_tone_shadow",
    "llm_degradation",
    "self_continuity_shadow",
    "self_duplicate_draft",
    "addressee_repaired",
)


def _iter_events(log_path: str, since: str = ""):
    for line in pathlib.Path(log_path).read_text(encoding="utf-8", errors="replace").splitlines():
        if not any(name in line for name in WATCH):
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if since and str(obj.get("timestamp", "")) < since:
            continue
        yield obj


def collect(events) -> dict:
    tone_total = 0
    tone_model_like = 0
    deg = collections.Counter()
    cont = collections.Counter()
    addr = collections.Counter()
    for obj in events:
        ev = str(obj.get("event") or "")
        if ev == "human_tone_shadow":
            tone_total += 1
            if obj.get("model_like"):
                tone_model_like += 1
        elif ev == "llm_degradation":
            deg[f"{obj.get('stage') or '?'}/{obj.get('failure') or '?'}"] += 1
        elif ev in ("self_continuity_shadow", "self_duplicate_draft"):
            cont[ev] += 1
        elif ev.startswith("addressee_"):
            addr[ev] += 1
    tone_rate = round(tone_model_like / tone_total, 3) if tone_total else 0.0
    gates = [
        {
            "name": "human_tone",
            "metric": "样本数 / 模型腔率",
            "current": f"{tone_total}/{tone_rate}",
            "threshold": ">=30 / <0.10",
            "ready": tone_total >= 30 and tone_rate < 0.10,
        },
    ]
    info = {
        "degradation": dict(deg),
        "continuity": dict(cont),
        "addressee": dict(addr),
    }
    return {"gates": gates, "info": info}


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
