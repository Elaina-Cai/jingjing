# -*- coding: utf-8 -*-
"""噪音闸门读数：noise_gate_shadow / noise_gate_skipped 的 reason/layer/发送人。

用法：
  .venv_it\\Scripts\\python.exe scripts\\report_noise_gate.py --log logs/yinyue.log --since 2026-10-15T00:50
"""
from __future__ import annotations

import argparse
import collections
import json
import pathlib


def _iter(log_path: str, since: str = ""):
    for line in pathlib.Path(log_path).read_text(encoding="utf-8", errors="replace").splitlines():
        if "noise_gate_" not in line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if str(obj.get("event") or "") not in ("noise_gate_shadow", "noise_gate_skipped"):
            continue
        if since and str(obj.get("timestamp", "")) < since:
            continue
        yield obj


def collect(events) -> dict:
    total = 0
    by_event = collections.Counter()
    reasons = collections.Counter()
    layers = collections.Counter()
    senders = collections.Counter()
    chats = collections.Counter()
    for obj in events:
        total += 1
        by_event[str(obj.get("event"))] += 1
        reasons[str(obj.get("reason") or "?")] += 1
        layers[str(obj.get("layer") or "?")] += 1
        senders[str(obj.get("sender") or "?")] += 1
        chats[str(obj.get("chat_id") or "?")] += 1
    return {
        "total": total,
        "by_event": dict(by_event),
        "reasons": dict(reasons),
        "layers": dict(layers),
        "top_senders": dict(senders.most_common(8)),
        "top_chats": dict(chats.most_common(5)),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default="logs/yinyue.log")
    ap.add_argument("--since", default="")
    ap.add_argument("--json", default="")
    args = ap.parse_args()
    report = collect(_iter(args.log, args.since))
    if args.json:
        pathlib.Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
