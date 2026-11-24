#!/usr/bin/env python
"""Export Layer-2 feedback samples as eval fixtures (read-only).

    python scripts/export_feedback_samples.py
    python scripts/export_feedback_samples.py --status new --mark-exported
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--status", default="new")
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--mark-exported", action="store_true")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    from core.bootstrap import get_meta_repo

    repo = get_meta_repo().feedback_samples
    rows = list(
        repo.list_recent(limit=args.limit, status=args.status or None)
    )
    payload = {
        "exported_at": datetime.now().isoformat(),
        "status_filter": args.status,
        "count": len(rows),
        "samples": [row.as_dict() for row in rows],
    }
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = Path(args.out) if args.out else ROOT / "output" / f"feedback_samples_{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"exported {len(rows)} sample(s) -> {out}")

    if args.mark_exported:
        for row in rows:
            repo.mark_status(row.id, "exported")
        print(f"marked {len(rows)} sample(s) as exported")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
