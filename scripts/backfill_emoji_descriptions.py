"""Fill in emoji descriptions for rows that were never patched.

Why this exists
---------------
Live recognition is one-at-a-time by design: a sticker is described while a
turn is being answered.  That leaves history behind -- rows written before the
write path worked, and rows whose sticker simply never came round again -- so a
later search still shows her ``[表情包:<md5>]`` instead of a description.

This walks every database that holds message rows, finds the unresolved
placeholders, and resolves them through the same code path the live runtime
uses (cache first, vision only on a miss), then patches every database.  One
recognition fixes every occurrence in every database at once.

Usage::

    python scripts/backfill_emoji_descriptions.py --dry-run
    python scripts/backfill_emoji_descriptions.py --limit 20
    python scripts/backfill_emoji_descriptions.py
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.preprocess.emoji_recognition import (  # noqa: E402
    _MD5_PATTERN,
    _candidate_databases,
    _find_emoji_image,
    _patch_placeholders,
    ensure_emoji_description,
)

REPORT_PATH = Path("output/emoji_backfill_report.md")


def collect() -> list[tuple[str, int]]:
    """Return (md5, occurrence count) for still-unresolved stickers."""
    counts: dict[str, int] = {}
    for db_path in _candidate_databases():
        try:
            con = sqlite3.connect(db_path)
            try:
                # Materialise before closing: the cursor is lazy, and reading
                # it after close() raises ProgrammingError.
                rows = con.execute(
                    "select content from messages where content like ?",
                    ("%[表情包:%",),
                ).fetchall()
            finally:
                con.close()
        except sqlite3.Error as exc:
            print(f"  ! skipped {db_path}: {exc}")
            continue
        for (content,) in rows:
            for md5 in _MD5_PATTERN.findall(content or ""):
                counts[md5] = counts.get(md5, 0) + 1
    # Most frequent first: with --limit, the calls that fix the most rows run
    # first, so an interrupted run still leaves the biggest holes filled.
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


async def run(limit: int, dry_run: bool) -> int:
    from core.bootstrap import init_singletons
    from core.env import load_yinyue_env_defaults

    load_yinyue_env_defaults()
    pending = collect()
    total_rows = sum(n for _, n in pending)
    print(f"unresolved stickers: {len(pending)} distinct, {total_rows} rows")
    if not pending:
        print("nothing to do")
        return 0

    no_image = [md5 for md5, _ in pending if not _find_emoji_image(md5)]
    if no_image:
        print(f"  {len(no_image)} have no image on disk and cannot be described "
              f"(e.g. {no_image[0][:8]})")
    if dry_run:
        for md5, n in pending[:limit]:
            state = "ok " if _find_emoji_image(md5) else "no-image"
            print(f"  {md5[:8]} x{n:<4} {state}")
        print(f"\n(dry run: would describe {min(limit, len(pending))} stickers)")
        return 0

    init_singletons()

    done: list[tuple[str, str, int, dict[str, int]]] = []
    failed: list[tuple[str, str, int]] = []
    started = time.monotonic()
    for index, (md5, n) in enumerate(pending[:limit], start=1):
        try:
            desc = await ensure_emoji_description(md5)
        except Exception as exc:  # noqa: BLE001 - keep going through the batch
            desc = ""
            failed.append((md5, f"exception: {exc}"[:80], n))
        if not desc:
            if not any(m == md5 for m, _, _ in failed):
                failed.append((md5, "no description", n))
            print(f"  [{index}/{min(limit, len(pending))}] {md5[:8]} x{n} FAILED")
            continue
        per_db: dict[str, int] = {}
        for db_path in _candidate_databases():
            try:
                changed = _patch_placeholders(db_path, md5, desc)
            except Exception as exc:  # noqa: BLE001
                print(f"      ! {db_path}: {exc}")
                continue
            if changed:
                per_db[db_path] = changed
        done.append((md5, desc, n, per_db))
        elapsed = time.monotonic() - started
        print(f"  [{index}/{min(limit, len(pending))}] {md5[:8]} x{n} -> {desc} "
              f"{per_db or '(already patched)'} [{elapsed:.0f}s]")

    patched_rows = sum(sum(v.values()) for _, _, _, v in done)
    lines = [
        "# 表情包描述回填报告",
        "",
        f"- 运行时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
        f"- 待描述表情：{len(pending)} 个 / {total_rows} 行",
        f"- 本次尝试：{min(limit, len(pending))} 个",
        f"- 成功：{len(done)} 个，改写 {patched_rows} 行",
        f"- 失败：{len(failed)} 个",
        "",
    ]
    if failed:
        lines += ["## 失败", "", "| md5 | 行数 | 原因 |", "|---|---|---|"]
        lines += [f"| `{m[:8]}` | {n} | {why} |" for m, why, n in failed]
        lines.append("")
    lines += ["## 已完成", "", "| md5 | 描述 | 命中行数 | 写入 |", "|---|---|---|---|"]
    for md5, desc, n, per_db in done:
        where = "、".join(
            f"{Path(p).name}={c}" for p, c in sorted(per_db.items())
        ) or "已是最新"
        lines.append(f"| `{md5[:8]}` | {desc} | {n} | {where} |")
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")

    remaining = collect()
    print(f"\nresolved {len(done)} stickers, {patched_rows} rows rewritten")
    print(f"failed {len(failed)}")
    print(f"remaining: {len(remaining)} stickers / "
          f"{sum(n for _, n in remaining)} rows")
    print(f"report: {REPORT_PATH}")
    return 0 if not failed else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=10_000)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    return asyncio.run(run(args.limit, args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
