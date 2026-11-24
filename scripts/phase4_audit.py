"""Phase 4 audit export / retention control.

Examples:
    python scripts/phase4_audit.py counts
    python scripts/phase4_audit.py export
    python scripts/phase4_audit.py plan --event-days 365
    python scripts/phase4_audit.py purge --event-days 365 --execute

``purge`` is dry-run unless ``--execute`` is passed.  Before deleting, a SYSTEM
tombstone event is appended to ``terminal_events`` so the purge itself is
auditable.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _engine(meta_db: str):
    from sqlalchemy import create_engine

    return create_engine(f"sqlite:///{meta_db}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Terminal audit export / retention")
    parser.add_argument("command", choices=["counts", "export", "plan", "purge"])
    parser.add_argument("--meta-db", default=str(ROOT / "data" / "yinyue_meta.db"))
    parser.add_argument("--out", default="")
    parser.add_argument("--event-days", type=int, default=365)
    parser.add_argument("--projection-days", type=int, default=365)
    parser.add_argument("--command-days", type=int, default=0)
    parser.add_argument("--authorization-days", type=int, default=0)
    parser.add_argument("--query-log-days", type=int, default=180)
    parser.add_argument("--link-edge-days", type=int, default=0)
    parser.add_argument("--membership-days", type=int, default=0)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="actually delete rows for purge; default is dry-run",
    )
    args = parser.parse_args()

    from core.terminal.audit_export import (
        RetentionPolicy,
        apply_retention,
        audit_counts,
        export_all,
        retention_preview,
    )

    policy = RetentionPolicy(
        event_days=max(0, args.event_days),
        projection_days=max(0, args.projection_days),
        command_days=max(0, args.command_days),
        authorization_days=max(0, args.authorization_days),
        query_log_days=max(0, args.query_log_days),
        link_edge_days=max(0, args.link_edge_days),
        membership_days=max(0, args.membership_days),
    )
    engine = _engine(args.meta_db)
    try:
        if args.command == "counts":
            result = audit_counts(engine)
        elif args.command == "export":
            out = args.out or str(
                ROOT / "backups"
                / f"audit_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            )
            result = export_all(engine, out)
        elif args.command == "plan":
            result = {"policy": policy.as_table_days(),
                      "would_delete": retention_preview(engine, policy)}
        else:
            result = apply_retention(
                engine, policy, dry_run=not args.execute
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
