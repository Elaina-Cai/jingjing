"""One-off image description backfill (optional Phase 4).

The live pipeline describes images on demand and keeps an in-process cache
(``core/conversation/media.py``).  This script is for the slower path: describe
historical images once and optionally write ``[AI解读: ...]`` into the message
content so every later turn / memory read sees it without another vision call.

Safety: dry-run is the default.  ``--apply`` writes to the message DB, so back
it up first (the script refuses to run without ``--yes`` as a second gate).

Example:
    python scripts/enrich_image_descriptions.py --limit 20
    python scripts/enrich_image_descriptions.py --limit 20 --apply --yes
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.conversation.media import media_fingerprint, media_path_to_data_uri


def _connect(db_path: str, *, readonly: bool) -> sqlite3.Connection:
    if readonly:
        return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    return sqlite3.connect(db_path)


def _fetch_rows(con: sqlite3.Connection, limit: int) -> list[tuple]:
    return con.execute(
        """
        SELECT msg_id, content, media_path
        FROM messages
        WHERE msg_type = 3
          AND media_path IS NOT NULL
          AND media_path != ''
          AND (content IS NULL OR content NOT LIKE '%[AI解读:%')
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (max(1, int(limit)),),
    ).fetchall()


async def _describe(llm, path: str) -> str:
    uri = media_path_to_data_uri(path)
    if not uri:
        return ""
    return await llm.chat_vision(
        system=(
            "你是一个图片描述助手。用一句简短中文描述图片内容、文字和情绪，"
            "看不清就说看不清，不要编造。"
        ),
        user="描述这张图片。",
        images=[uri],
        agent_name="image_backfill",
        temperature=0.2,
        max_tokens=120,
    )


async def _run(args: argparse.Namespace) -> int:
    db_path = str(Path(args.db))
    if not Path(db_path).exists():
        print(f"[!] db not found: {db_path}")
        return 2

    con = _connect(db_path, readonly=not args.apply)
    rows = _fetch_rows(con, args.limit)
    if not rows:
        print("nothing to describe")
        con.close()
        return 0

    if args.apply and not args.yes:
        print("[!] --apply requires --yes (back up the DB first)")
        con.close()
        return 2

    from llm.client import LLMClient

    llm = LLMClient()
    seen: dict[str, str] = {}
    results: list[dict] = []
    for msg_id, content, media_path in rows:
        fingerprint = media_fingerprint(media_path)
        if fingerprint and fingerprint in seen:
            description = seen[fingerprint]
            cached = True
        else:
            description = await _describe(llm, media_path)
            cached = False
            if fingerprint and description:
                seen[fingerprint] = description
        preview = (content or "").replace("\n", " ")[:40]
        print(f"- {msg_id} {preview!r} -> {description!r}"
              f"{' (cache)' if cached else ''}")
        results.append({
            "msg_id": str(msg_id),
            "media_path": str(media_path),
            "description": description,
            "from_cache": cached,
        })
        if args.apply and description:
            new_content = f"{(content or '').rstrip()} [AI解读: {description}]"
            con.execute(
                "UPDATE messages SET content = ? WHERE msg_id = ?",
                (new_content, msg_id),
            )

    if args.apply:
        con.commit()
    con.close()

    out_path = Path(args.out) if args.out else Path(
        f"output/image_descriptions_{datetime.now():%Y%m%d_%H%M%S}.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(results, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\nwritten: {out_path}")
    print(f"mode: {'APPLIED' if args.apply else 'dry-run (use --apply --yes)'}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/yinyue.db")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--out", default="")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
