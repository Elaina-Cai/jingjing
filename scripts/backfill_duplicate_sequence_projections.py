#!/usr/bin/env python
"""ENG-23 定向回补：只重投影「重号漏行」，不动 53 条已失效记忆、不整表回放。

背景与纪律
----------
旧游标是 ``max(sequence)`` + 读侧 ``sequence > :cursor``，两类行读不到：

* **跨批**：批 A 以 seq=S 结尾 → 游标=S → 批 B 的 ``> S`` 跳过同号 S 的后继行；
* **迟到行**：游标推到 S 之后才落库的同号行（2026-09-25 落的那批 6083–6089，
  2026-09-29 又落了同号的 7 行，行 id 更大 → 旧谓词永久跳过）。

现网 8 对重号里，8 行「后到的那一行」没有投影行（每对恰一行）。另有 6 行同号
事件（``[图片]`` 占位等）本就该被噪声过滤，不在回补范围 —— 脚本会把两类分别
打印，绝不为了「把缺口清零」硬造投影。

为什么不 ``backfill()`` 整表回放
--------------------------------
``MemoryProjector.backfill()`` 从 sequence 0 扫全表 → 会把
``scripts/cleanup_test_memory_events.py`` 删掉测试事件的 ~106 行历史缺口重新
投影成记忆（主人已裁定：不整批回补，有待拍板）。所以这里只把**目标事件**交给
``project_events``：写入同样的确定性 id（``proj:<event_id>``），游标按批内最后
一条的 ``(sequence, id)`` 推进（``project_events`` 只往前走，不倒退）。

安全性
------
* 目标事件全部 ``sequence <= 游标号`` → 回补后不会被前向投影重复扫到；
* 既有行 ``valid_to`` 非空时 upsert **不清零** → 53 条失效记忆保持失效；
* ``--dry-run`` 只报告不写库；写完复核「应有投影的行都有投影」+ 失效数不下降。

用法（迁移 53 落地 = 重启一次之后）::

    .\\.venv\\Scripts\\python.exe scripts\\backfill_duplicate_sequence_projections.py --dry-run
    .\\.venv\\Scripts\\python.exe scripts\\backfill_duplicate_sequence_projections.py \\
        --only 2554:eb1aa339cb6b4687a7f0d526ad1dcdc5,6083:8e79a3c0715a40b8baf91a11e4d521f6

演练（在副本上跑，先 ``run_migrations(copy)`` 把 53 落地）::

    .\\.venv\\Scripts\\python.exe scripts\\backfill_duplicate_sequence_projections.py \\
        --db data\\_backfill_rehearsal\\yinyue_meta.db --only ...
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def duplicate_sequence_rows(engine) -> list[tuple[int, int, str]]:
    """重号序列的全部行 ``(sequence, row_id, event_id)``，按 ``(sequence, id)`` 升序。"""
    from sqlalchemy import text

    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT e.sequence, e.id, e.event_id FROM terminal_events e "
            "JOIN (SELECT sequence FROM terminal_events GROUP BY sequence "
            "      HAVING COUNT(*) > 1) d ON d.sequence = e.sequence "
            "ORDER BY e.sequence, e.id"
        )).fetchall()
    return [
        (int(sequence or 0), int(row_id or 0), str(event_id or ""))
        for sequence, row_id, event_id in rows
    ]


def has_projection(engine, event_id: str) -> bool:
    from sqlalchemy import text

    with engine.connect() as conn:
        return conn.execute(
            text("SELECT 1 FROM memory_projections WHERE memory_id = :mid"),
            {"mid": f"proj:{event_id}"},
        ).first() is not None


def projection_count(engine) -> int:
    from sqlalchemy import text

    with engine.connect() as conn:
        return int(conn.execute(text("SELECT COUNT(*) FROM memory_projections")).scalar() or 0)


def invalidated_count(engine) -> int:
    from sqlalchemy import text

    with engine.connect() as conn:
        return int(conn.execute(text(
            "SELECT COUNT(*) FROM memory_projections WHERE valid_to IS NOT NULL"
        )).scalar() or 0)


def parse_only(text_value: str) -> set[tuple[int, str]]:
    """``6083:<event_id>,2554:<event_id>`` → ``{(seq, event_id), ...}``。"""
    wanted: set[tuple[int, str]] = set()
    for chunk in str(text_value or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        sequence, _, event_id = chunk.partition(":")
        wanted.add((int(sequence), event_id.strip()))
    return wanted


def main() -> int:
    parser = argparse.ArgumentParser(description="ENG-23 重号漏行定向回补")
    parser.add_argument("--dry-run", action="store_true", help="只报告，不写库")
    parser.add_argument(
        "--db",
        default="",
        help="覆盖 meta 库路径（默认 config.paths.meta_db_path；演练请指向副本）",
    )
    parser.add_argument(
        "--only",
        default="",
        help="只回补这些 ``seq:event_id``（逗号分隔）；给了就不再自动挑全部缺投影行",
    )
    args = parser.parse_args()

    from sqlalchemy import create_engine, text

    from core.config.settings import config
    from core.terminal.event_store_db import TerminalEventStore
    from core.terminal.memory_projection import MemoryProjector
    from db.repo_memory_projections import MemoryProjectionRepository

    meta_db = str(args.db or config.paths.meta_db_path)
    # 直连引擎：**不**走 MetaRepositoryManager（构造期会 init_meta_db + run_migrations）。
    # 本脚本只做一件窄事（写目标投影），迁移交给重启时的 bootstrap。
    engine = create_engine(f"sqlite:///{meta_db}")
    with engine.connect() as conn:
        columns = {
            row[1]
            for row in conn.execute(
                text("PRAGMA table_info(memory_projection_cursors)")
            ).fetchall()
        }
        if "last_id" not in columns:
            print(
                f"[abort] {meta_db} 的 memory_projection_cursors 还没有 last_id："
                "迁移 53 未落地（需要重启一次让 bootstrap 跑到 53）。未写任何东西。"
            )
            engine.dispose()
            return 2

    store = TerminalEventStore(engine)
    repo = MemoryProjectionRepository(engine)
    projector = MemoryProjector(store, repo)

    rows = duplicate_sequence_rows(engine)
    wanted = parse_only(args.only)
    if wanted:
        rows = [row for row in rows if (row[0], row[2]) in wanted]
        if len(rows) != len(wanted):
            print(f"[abort] --only 里有 {len(wanted) - len(rows)} 个 (seq,event_id) 不是重号行")
            engine.dispose()
            return 2

    by_id = {str(event.event_id): event for event in store.all()}
    missing = [row for row in rows if not has_projection(engine, row[2])]
    # 预分类**不写库**：project_event() 是纯函数，item is None = 这条本来就不该投影
    # （噪声/排除类型）。回补只承诺「应有投影的行」，绝不为了清零缺口硬造记忆。
    expected = [row for row in missing if projector.project_event(by_id[row[2]]) is not None]
    noise = [row for row in missing if row not in expected]

    closed_before = invalidated_count(engine)
    total_before = projection_count(engine)
    cursor_before = projector.projection_cursor_pair()
    print(
        f"db={meta_db}\n"
        f"duplicate_rows={len(rows)} missing={len(missing)} "
        f"expected_projection={len(expected)} expected_noise={len(noise)} "
        f"projections_before={total_before} invalidated_before={closed_before} "
        f"cursor_before={cursor_before}"
    )
    for sequence, row_id, event_id in expected:
        print(f"  BACKFILL seq={sequence} id={row_id} event_id={event_id}")
    for sequence, row_id, event_id in noise:
        print(f"  NOISE    seq={sequence} id={row_id} event_id={event_id}（本就无投影，跳过）")

    if not expected:
        print("nothing to do")
        engine.dispose()
        return 0
    if args.dry_run:
        print("dry-run：未写库。")
        engine.dispose()
        return 0

    wanted_ids = {event_id for _s, _i, event_id in expected}
    events = [by_id[event_id] for event_id in wanted_ids]
    events.sort(key=lambda event: int(event.sequence or 0))
    stats = projector.project_events(events, advance_cursor=True)

    still_missing = [
        row for row in expected if not has_projection(engine, row[2])
    ]
    closed_after = invalidated_count(engine)
    total_after = projection_count(engine)
    print(
        f"scanned={stats['scanned']} projected={stats['projected']} "
        f"projections_after={total_after} new_rows={total_after - total_before} "
        f"cursor_after={projector.projection_cursor_pair()} "
        f"invalidated_after={closed_after} still_missing={len(still_missing)}"
    )
    for sequence, row_id, event_id in still_missing:
        print(f"  STILL-MISS seq={sequence} id={row_id} event_id={event_id}")
    engine.dispose()
    ok = not still_missing and closed_after >= closed_before
    print("OK" if ok else "CHECK-FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
