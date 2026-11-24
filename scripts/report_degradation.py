# -*- coding: utf-8 -*-
"""模型输出降级收数（批D 路线 5）。

用法：
  .venv_it\\Scripts\\python.exe scripts\\report_degradation.py --log logs/yinyue.log --since 2026-10-14T12:00
"""
from __future__ import annotations

import argparse
import collections
import json
import pathlib


def _iter_events(log_path: str, since: str = ""):
    for line in pathlib.Path(log_path).read_text(encoding="utf-8", errors="replace").splitlines():
        if "llm_degradation" not in line:
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
    stage = collections.Counter()
    failure = collections.Counter()
    fallback = collections.Counter()
    pairs = collections.Counter()
    chats = collections.Counter()
    for obj in events:
        total += 1
        stage[str(obj.get("stage") or "?")] += 1
        failure[str(obj.get("failure") or "?")] += 1
        fallback[str(obj.get("fallback") or "?")] += 1
        pairs[f"{obj.get('stage') or '?'}/{obj.get('failure') or '?'}"] += 1
        if str(obj.get("chat_id") or ""):
            chats[str(obj.get("chat_id"))] += 1
    return {
        "total": total,
        "stage": dict(stage),
        "failure": dict(failure),
        "fallback": dict(fallback),
        "pairs": dict(pairs),
        "chats": dict(chats),
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
