"""Read-only aggregator for the recall-chain shadow readouts (rollout gates, §15).

Parses logs/yinyue.log JSON lines emitted by core.memory.recall_chain:
  * recall_chain_shadow  - one line per needs_memory recall in shadow/on mode
  * query_style_diff     - prod / jieba-bag / raw-inbound top-3 Jaccard comparison

Usage:
  python -X utf8 scripts/report_recall_chain_shadow.py --log logs/yinyue.log --gate
  python -X utf8 scripts/report_recall_chain_shadow.py --json
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

GATES = {
    "inject_plan_nonempty_rate": 0.80,
    "suppressed_by_window_rate": 0.50,
    "elapsed_p90_ms": 300.0,
    "jaccard_prod_raw_median": 0.50,
}


def _percentile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    pos = (len(ordered) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    frac = pos - lo
    return float(ordered[lo] * (1 - frac) + ordered[hi] * frac)


def _load(path: Path):
    shadow, diff = [], []
    with path.open("r", encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            if '"recall_chain_shadow"' not in line and '"query_style_diff"' not in line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            event = str(row.get("event") or "")
            if event == "recall_chain_shadow":
                shadow.append(row)
            elif event == "query_style_diff":
                diff.append(row)
    return shadow, diff


def summarize(shadow, diff, *, by_chat=False):
    out = {"n_shadow": len(shadow), "n_diff": len(diff)}
    if shadow:
        elapsed = [int(r.get("elapsed_ms") or 0) for r in shadow]
        rows = [int(r.get("rows") or 0) for r in shadow]
        hits = [int(r.get("hits") or 0) for r in shadow]
        plan = [int(r.get("inject_plan_len") or 0) for r in shadow]
        vec = fts = kw = 0
        for r in shadow:
            try:
                sizes = json.loads(r.get("routes_json") or "{}").get("route_sizes") or {}
            except Exception:
                sizes = {}
            vec += 1 if int(sizes.get("vector") or 0) > 0 else 0
            fts += 1 if int(sizes.get("fts") or 0) > 0 else 0
            kw += 1 if int(sizes.get("keyword") or 0) > 0 else 0
        out.update({
            "chats": sorted({str(r.get("chat_id") or "") for r in shadow}),
            "first_ts": min(str(r.get("timestamp") or "") for r in shadow),
            "last_ts": max(str(r.get("timestamp") or "") for r in shadow),
            "route_nonempty": {
                "vector": round(vec / len(shadow), 4),
                "fts": round(fts / len(shadow), 4),
                "keyword": round(kw / len(shadow), 4),
            },
            "hits_nonzero_rate": round(sum(1 for h in hits if h > 0) / len(shadow), 4),
            "inject_plan_nonempty_rate": round(sum(1 for p in plan if p > 0) / len(shadow), 4),
            "suppressed_by_window_rate": round(
                sum(1 for r in shadow if r.get("suppressed_by_window")) / len(shadow), 4
            ),
            "elapsed_ms": {
                "p50": _percentile(elapsed, 0.5),
                "p90": _percentile(elapsed, 0.9),
                "p99": _percentile(elapsed, 0.99),
                "max": max(elapsed),
            },
            "rows_median": int(statistics.median(rows)) if rows else 0,
        })
        if by_chat:
            per = {}
            for r in shadow:
                chat = str(r.get("chat_id") or "")
                bucket = per.setdefault(chat, {"n": 0, "plan_nonempty": 0, "suppressed": 0, "elapsed": []})
                bucket["n"] += 1
                bucket["plan_nonempty"] += 1 if int(r.get("inject_plan_len") or 0) > 0 else 0
                bucket["suppressed"] += 1 if r.get("suppressed_by_window") else 0
                bucket["elapsed"].append(int(r.get("elapsed_ms") or 0))
            out["by_chat"] = {
                chat: {
                    "n": b["n"],
                    "inject_plan_nonempty_rate": round(b["plan_nonempty"] / b["n"], 4),
                    "suppressed_by_window_rate": round(b["suppressed"] / b["n"], 4),
                    "elapsed_p90_ms": _percentile(b["elapsed"], 0.9),
                }
                for chat, b in sorted(per.items())
            }
    if diff:
        jr = [float(r.get("jaccard_prod_raw") or 0.0) for r in diff]
        jb = [float(r.get("jaccard_prod_bag") or 0.0) for r in diff]
        br = [float(r.get("jaccard_bag_raw") or 0.0) for r in diff]
        out["jaccard"] = {
            "prod_raw": {"median": _percentile(jr, 0.5), "mean": round(statistics.mean(jr), 4)},
            "prod_bag": {"median": _percentile(jb, 0.5), "mean": round(statistics.mean(jb), 4)},
            "bag_raw": {"median": _percentile(br, 0.5), "mean": round(statistics.mean(br), 4)},
        }
    return out


def gate_report(summary):
    checks = []
    checks.append((
        "inject_plan_nonempty_rate >= %.2f" % GATES["inject_plan_nonempty_rate"],
        summary.get("inject_plan_nonempty_rate"),
        GATES["inject_plan_nonempty_rate"],
        (summary.get("inject_plan_nonempty_rate") or 0.0) >= GATES["inject_plan_nonempty_rate"],
    ))
    checks.append((
        "suppressed_by_window_rate < %.2f" % GATES["suppressed_by_window_rate"],
        summary.get("suppressed_by_window_rate"),
        GATES["suppressed_by_window_rate"],
        (summary.get("suppressed_by_window_rate") or 0.0) < GATES["suppressed_by_window_rate"],
    ))
    p90 = (summary.get("elapsed_ms") or {}).get("p90")
    checks.append(("elapsed_p90_ms < %.0f" % GATES["elapsed_p90_ms"], p90, GATES["elapsed_p90_ms"], (p90 or 1e9) < GATES["elapsed_p90_ms"]))
    med = (summary.get("jaccard") or {}).get("prod_raw", {}).get("median")
    checks.append((
        "jaccard_prod_raw_median < %.2f" % GATES["jaccard_prod_raw_median"],
        med,
        GATES["jaccard_prod_raw_median"],
        med is not None and med < GATES["jaccard_prod_raw_median"],
    ))
    return checks


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="recall-chain shadow readout aggregator")
    ap.add_argument("--log", default=str(ROOT / "logs" / "yinyue.log"))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--by-chat", action="store_true")
    ap.add_argument("--gate", action="store_true")
    args = ap.parse_args(argv)

    path = Path(args.log)
    if not path.exists():
        print("log not found: %s" % path)
        return 2
    shadow, diff = _load(path)
    summary = summarize(shadow, diff, by_chat=args.by_chat)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    print("shadow_calls=%d style_diff=%d" % (summary["n_shadow"], summary["n_diff"]))
    if shadow:
        print("chats=%s" % ",".join(summary["chats"]))
        print("window=%s .. %s" % (summary["first_ts"], summary["last_ts"]))
        print("route_nonempty=%s" % summary["route_nonempty"])
        print("hits_nonzero=%.3f inject_plan_nonempty=%.3f suppressed_by_window=%.3f"
              % (summary["hits_nonzero_rate"], summary["inject_plan_nonempty_rate"],
                 summary["suppressed_by_window_rate"]))
        print("elapsed_ms=%s" % summary["elapsed_ms"])
    if diff:
        print("jaccard=%s" % summary["jaccard"])
    if args.by_chat and shadow:
        for chat, bucket in summary["by_chat"].items():
            print("  %-26s n=%-4d plan_nonempty=%.3f suppressed=%.3f p90=%.0fms"
                  % (chat, bucket["n"], bucket["inject_plan_nonempty_rate"],
                     bucket["suppressed_by_window_rate"], bucket["elapsed_p90_ms"] or 0))
    if args.gate:
        print("--- gates (§15) ---")
        for name, value, threshold, ok in gate_report(summary):
            print("%-42s value=%-10s threshold=%-8s %s" % (name, value, threshold, "PASS" if ok else "FAIL"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
