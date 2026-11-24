#!/usr/bin/env python
"""P1 存量清理：旧成员名单版本 / 玩笑榜投影归并失效 + 引用回声观察下线。

Dry-run by default:

    python scripts/cleanup_memory_projections.py            # 只看计划
    python scripts/cleanup_memory_projections.py --apply    # 执行（先 SQLite 备份 meta DB）

规则：
* 成员名单类投影：保留 1 条（优先含"最终"的最新版），其余 valid_to 失效；
* 玩笑榜投影（萌榜/牛子/榜中榜）：全部失效（群聊玩笑不再进长期召回）；
* 引用回声观察：content/raw_quote 含"[引用 鲸鲸"的 active 观察 → status=superseded。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

META_DB = ROOT / "data" / "yinyue_meta.db"
BACKUP_DIR = ROOT / "backups"

ROSTER_SQL = (
    "select memory_id, content, visibility_scope_json, created_at "
    "from memory_projections where valid_to is null and ("
    "content like '%成员清单%' or content like '%成员档案%' "
    "or (content like '%群成员%' and content like '%班%'))"
)
JOKE_SQL = (
    "select memory_id, content, created_at from memory_projections "
    "where valid_to is null and (content like '%萌榜%' "
    "or content like '%牛子%' or content like '%榜中榜%')"
)
ECHO_SQL = (
    "select observation_id, substr(content,1,60) from person_observations "
    "where status = 'active' and (content like '%引用 鲸鲸%' "
    "or raw_quote like '%引用 鲸鲸%')"
)


def _backup() -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = BACKUP_DIR / f"meta_before_p1_cleanup_{stamp}.db"
    dst.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(str(META_DB))
    out = sqlite3.connect(str(dst))
    try:
        src.backup(out)
    finally:
        out.close()
        src.close()
    return dst


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="执行清理（默认 dry-run）")
    args = parser.parse_args()

    if not META_DB.exists():
        print(f"[err] meta db not found: {META_DB}")
        return 2

    con = sqlite3.connect(f"file:{META_DB.as_posix()}?mode=ro", uri=True)
    try:
        roster = con.execute(ROSTER_SQL).fetchall()
        jokes = con.execute(JOKE_SQL).fetchall()
        echoes = con.execute(ECHO_SQL).fetchall()
    finally:
        con.close()

    keep_id = ""
    if roster:
        finals = [row for row in roster if "最终" in (row[1] or "")]
        pool = finals or roster
        keep_id = sorted(pool, key=lambda row: row[3] or "")[-1][0]
    roster_drop = [row for row in roster if row[0] != keep_id]

    print("== 成员名单投影 ==")
    kept = next((row for row in roster if row[0] == keep_id), None)
    if kept is not None:
        print(f"  保留: {kept[0]} | {kept[3][:16]} | {(kept[1] or '')[:52]}")
    for row in roster_drop:
        print(f"  失效: {row[0]} | {row[3][:16]} | {(row[1] or '')[:52]}")

    print("== 玩笑榜投影 ==")
    for row in jokes:
        print(f"  失效: {row[0]} | {row[2][:16]} | {(row[1] or '')[:52]}")

    print("== 引用回声观察 ==")
    for row in echoes:
        print(f"  下线: {row[0]} | {(row[1] or '')[:60]}")

    total = len(roster_drop) + len(jokes)
    print(f"计划：投影失效 {total} 条（保留 {1 if kept else 0}），观察下线 {len(echoes)} 条")

    if not args.apply:
        print("（dry-run；加 --apply 执行，执行前自动备份 meta DB）")
        return 0

    backup = _backup()
    print(f"[backup] {backup}")

    from db.meta_repo import MetaRepositoryManager

    meta = MetaRepositoryManager(str(META_DB))
    closed = meta.memory_projections.invalidate(
        [row[0] for row in roster_drop] + [row[0] for row in jokes]
    )
    print(f"[apply] 投影失效 {closed} 条")

    now = datetime.now(timezone.utc).isoformat()
    con = sqlite3.connect(str(META_DB))
    try:
        cur = con.executemany(
            "update person_observations set status='superseded', updated_at=? "
            "where observation_id=?",
            [(now, row[0]) for row in echoes],
        )
        con.commit()
    finally:
        con.close()
    print(f"[apply] 观察下线 {cur.rowcount} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
