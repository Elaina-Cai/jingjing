"""Refresh scoped person profiles from existing observations.

This is a CLI adapter, not a core module.  It reads ``person_observations``,
maps them through the repository's domain mapper, and asks the core service to
refresh global + group + private snapshots.

Examples::

    python scripts/backfill_person_profiles.py --dry-run
    python scripts/backfill_person_profiles.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.contracts.person_memory import ProfileScopeType
from core.people.aggregator import RuleBasedProfileAggregator
from core.people.service import PersonMemoryService
from db.meta_models import init_meta_db
from db.repo_person_memory import PersonMemoryRepository


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--meta-db", default="data/yinyue_meta.db")
    parser.add_argument("--platform", default="")
    parser.add_argument("--account-id", default="")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    meta_db = Path(args.meta_db)
    if not meta_db.is_absolute():
        meta_db = ROOT / meta_db
    if not meta_db.exists():
        print(f"[error] meta database not found: {meta_db}")
        return 1

    engine = init_meta_db(str(meta_db))
    try:
        repo = PersonMemoryRepository(engine)
        observations = repo.list_all(
            platform=args.platform,
            account_id=args.account_id,
        )

        scopes = set()
        for observation in observations:
            base = (observation.person_id, observation.platform, observation.account_id)
            scopes.add(base + (ProfileScopeType.GLOBAL.value, ""))
            if observation.is_group_scope():
                scopes.add(base + (ProfileScopeType.GROUP.value, observation.occurred_scope_id))
            elif observation.is_private_scope():
                scopes.add(base + (ProfileScopeType.PRIVATE.value, observation.occurred_scope_id))

        if args.dry_run:
            print(json.dumps({
                "dry_run": True,
                "observations": len(observations),
                "scopes_to_refresh": len(scopes),
            }, ensure_ascii=False, indent=2))
            return 0

        service = PersonMemoryService(
            repo,
            repo,
            None,
            RuleBasedProfileAggregator(),
        )
        profiles = service.refresh_profiles_for_observations(observations)
        print(json.dumps({
            "dry_run": False,
            "observations": len(observations),
            "profiles_refreshed": len(profiles),
        }, ensure_ascii=False, indent=2))
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
