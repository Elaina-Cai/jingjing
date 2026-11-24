"""Project terminal_events into memory_projections.

Examples:
    python scripts/phase2_project_memory.py
    python scripts/phase2_project_memory.py --batches 20 --limit 500
    python scripts/phase2_project_memory.py --backfill
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Project terminal events into memory projections")
    parser.add_argument("--limit", type=int, default=500, help="events per batch")
    parser.add_argument("--batches", type=int, default=5, help="maximum batches to run")
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="reset the cursor and re-project from sequence 0 (idempotent)",
    )
    args = parser.parse_args(argv)

    from core.terminal.memory_runtime import get_memory_projector

    projector = get_memory_projector(force=True)
    if projector is None:
        from core.terminal.memory_runtime import memory_projector_error

        print(f"memory projector unavailable: {memory_projector_error()}", file=sys.stderr)
        return 2

    total_scanned = 0
    total_projected = 0
    cursor = 0
    stats = {}
    for batch_number in range(max(1, int(args.batches))):
        stats = projector.backfill(limit=max(1, int(args.limit))) if args.backfill and batch_number == 0 else projector.project_pending(limit=max(1, int(args.limit)))
        total_scanned += int(stats.get("scanned", 0) or 0)
        total_projected += int(stats.get("projected", 0) or 0)
        cursor = int(stats.get("cursor", 0) or 0)
        if not stats.get("scanned"):
            break

    print(json.dumps(
        {
            "scanned": total_scanned,
            "projected": total_projected,
            "cursor": cursor,
            "backfill": bool(args.backfill),
            **({"status": stats["status"], "reason": stats.get("reason", "")} if stats.get("status") else {}),
        },
        ensure_ascii=False,
    ))
    return 3 if stats.get("status") == "retired" else 0


if __name__ == "__main__":
    raise SystemExit(main())
