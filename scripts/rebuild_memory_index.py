#!/usr/bin/env python
"""Rebuild the derived memory vector index from memory_projections.

Phase 5 E7: vector stores are indexes over canonical projections, not facts.
This script is dry-run by default:

    python scripts/rebuild_memory_index.py
    python scripts/rebuild_memory_index.py --apply --limit 500

``--apply`` calls the configured embedding provider, so treat it as an
operator action.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.bootstrap import get_embedding, get_meta_repo, get_vector_store  # noqa: E402


async def _apply(limit: int, batch_size: int, clear_old: bool = False) -> dict:
    meta = get_meta_repo()
    projection_repo = meta.memory_projections
    vectors = get_vector_store()
    embedding = get_embedding()
    if clear_old:
        try:
            collection = vectors.client.get_collection("memory_embeddings")
            ids = list(collection.get(include=[]).get("ids", []))
            if ids:
                collection.delete(ids=ids)
        except Exception:
            pass
    items = projection_repo.all(limit=limit or None)
    written = 0
    failed = 0
    for item in items:
        content = str(item.content or "").strip()
        if not content:
            continue
        try:
            vector = await embedding.embed(content[:4000])
            vectors.memories.add(
                item.memory_id,
                content[:4000],
                vector,
                metadata={
                    "projection_id": item.memory_id,
                    "memory_type": item.memory_type,
                    "platform": item.platform,
                    "bot_account_id": item.bot_account_id,
                    "source_event_ids": list(item.source_event_ids),
                    "explicit_remember": bool(
                        item.metadata.get("explicit_remember")
                    ),
                    "kind": "personal",
                },
            )
            written += 1
        except Exception:
            failed += 1
        if batch_size and written and written % max(1, batch_size) == 0:
            print(f"indexed {written}/{len(items)}")
    return {
        "projections": len(items),
        "written": written,
        "failed": failed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="embed and upsert into the derived memory index",
    )
    parser.add_argument(
        "--clear-old",
        action="store_true",
        help="clear the derived memory index before rebuilding",
    )
    args = parser.parse_args()

    meta = get_meta_repo()
    count = meta.memory_projections.count()
    print(f"memory_projections={count}")
    if not args.apply:
        print("dry-run only; pass --apply to rebuild the derived index")
        return 0
    try:
        result = asyncio.run(
            _apply(max(0, args.limit), args.batch_size, args.clear_old)
        )
    except Exception as exc:  # noqa: BLE001
        print(f"index rebuild failed: {str(exc)[:240]}")
        return 1
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
