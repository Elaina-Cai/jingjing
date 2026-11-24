"""清理记忆投影里的确定性噪声（工具名 / 表情包占位 / 纯标点 / 单字碎片）。

只清理"100% 确定是噪声"的内容，不做语义/玩笑判断——玩笑该不该记，交给
提案制 + 主人审批（person_notes），不在这里用规则猜。

    python scripts/clean_noise_memories.py            # 只列出，不改
    python scripts/clean_noise_memories.py --apply    # 删除投影 + 对应向量（先备份）

删除的是派生投影（可从 terminal_events 重建），事实事件不动。
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import text as _sql  # noqa: E402


def _load_noise_rows(con):
    from core.terminal.memory_projection import _is_noise

    rows = con.execute(
        _sql("select memory_id, content from memory_projections")
    ).mappings().all()
    return [dict(r) for r in rows if _is_noise(str(r["content"] or ""))]


def _delete_vectors(memory_ids: list[str]) -> int:
    try:
        from core.bootstrap import get_vector_store

        collection = get_vector_store().client.get_collection("memory_embeddings")
        collection.delete(ids=memory_ids)
        return len(memory_ids)
    except Exception as exc:  # noqa: BLE001
        print(f"  [warn] 向量删除失败（可稍后 rebuild_memory_index.py --apply 重建）: {exc}")
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="真正删除（默认只列出）")
    args = ap.parse_args()

    from core.bootstrap import get_meta_repo

    repo = get_meta_repo()
    with repo.engine.connect() as con:
        noise = _load_noise_rows(con)

    print(f"噪声投影：{len(noise)} 条")
    for r in noise[:50]:
        print(f"  {r['memory_id']}  {str(r['content'])[:40]!r}")
    if len(noise) > 50:
        print(f"  ... 共 {len(noise)} 条")

    if not args.apply:
        print("\n[dry-run] 未改动。确认后用 --apply 执行。")
        return 0

    ids = [r["memory_id"] for r in noise]
    # 备份（内存库副本）
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = ROOT / "backups" / f"clean_noise_{ts}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        p = ROOT / "data" / f"yinyue_meta.db{suffix}"
        if p.exists():
            shutil.copy2(p, backup_dir / p.name)

    with repo.engine.begin() as con:
        for mid in ids:
            con.execute(
                _sql("delete from memory_projections where memory_id = :mid"),
                {"mid": mid},
            )
    n_vec = _delete_vectors(ids)
    print(f"\n已删除投影 {len(ids)} 条，向量 {n_vec} 条。")
    print(f"备份：{backup_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
