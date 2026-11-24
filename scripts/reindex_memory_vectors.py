#!/usr/bin/env python
"""Rebuild the memory_embeddings collection from its stored vectors (no API).

The 2026-09-21 E2 audit found the big `--clear-old` rebuild left the HNSW index
inconsistent: querying with an item's own stored vector did not return that item
(distances 0.58-0.76 instead of 0).  Later incremental adds were fine.

This script fixes the index *without* re-embedding: it dumps ids/embeddings/
documents/metadatas, deletes the collection, recreates it and re-adds the rows
in batches, then verifies self-query hits.

    python scripts/reindex_memory_vectors.py            # dry-run report
    python scripts/reindex_memory_vectors.py --apply
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

COLLECTION = "memory_embeddings"
BATCH = 200


def _collect(client) -> tuple[list[str], list, list, list]:
    col = client.get_collection(COLLECTION)
    data = col.get(include=["embeddings", "documents", "metadatas"])
    ids = list(data.get("ids", []))
    embeddings = list(data.get("embeddings", []))
    documents = list(data.get("documents", []))
    metadatas = list(data.get("metadatas", []))
    return ids, embeddings, documents, metadatas


def _self_hit_rate(client, sample: int = 8) -> float:
    col = client.get_collection(COLLECTION)
    ids = col.get(include=[])["ids"]
    if not ids:
        return 0.0
    step = max(1, len(ids) // sample)
    picks = ids[::step][:sample]
    hits = 0
    for target in picks:
        vec = col.get(ids=[target], include=["embeddings"])["embeddings"][0]
        res = col.query(query_embeddings=[vec], n_results=1, include=["distances"])
        dist = res["distances"][0][0] if res["distances"][0] else 9.9
        # Duplicate memories share one embedding, so "top1 is my id" is too
        # strict: an identical vector (distance ~0) proves the index is sound.
        if dist <= 1e-6:
            hits += 1
    return hits / max(1, len(picks))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", default="")
    args = parser.parse_args()

    from core.bootstrap import get_vector_store

    vs = get_vector_store()
    client = vs.client
    try:
        before = _self_hit_rate(client)
    except Exception as exc:  # noqa: BLE001
        print(f"[err] cannot read collection: {exc}")
        return 2
    ids, embeddings, documents, metadatas = _collect(client)
    print(f"collection={COLLECTION} rows={len(ids)} self_hit_rate={before:.0%}")

    if not args.apply:
        print("[dry-run] nothing changed. Re-run with --apply to rebuild the index.")
        return 0

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = Path(args.backup_dir) if args.backup_dir else (
        ROOT / "backups" / f"chroma_before_reindex_{stamp}"
    )
    backup_dir.mkdir(parents=True, exist_ok=True)
    persist = Path(vs.persist_dir)
    if persist.exists():
        shutil.copytree(persist, backup_dir / persist.name, dirs_exist_ok=True)
        print(f"chroma backup -> {backup_dir}")

    vs._collections.pop(COLLECTION, None)
    client.delete_collection(COLLECTION)
    collection = vs.client.get_or_create_collection(
        name=COLLECTION,
        metadata={
            "embedding_model": vs._embedding_model,
            "embedding_dim": vs._embedding_dim,
            "spec_name": COLLECTION,
            "version": 1,
            "description": "记忆向量，角色回忆相关经历",
        },
    )
    written = 0
    for start in range(0, len(ids), BATCH):
        end = start + BATCH
        collection.add(
            ids=ids[start:end],
            embeddings=embeddings[start:end],
            documents=documents[start:end],
            metadatas=metadatas[start:end],
        )
        written += len(ids[start:end])
        print(f"re-added {written}/{len(ids)}")

    after = _self_hit_rate(client)
    print(f"done: rows={written} self_hit_rate={after:.0%}")
    return 0 if after >= 0.99 else 1


if __name__ == "__main__":
    raise SystemExit(main())
