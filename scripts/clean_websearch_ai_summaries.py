# -*- coding: utf-8 -*-
"""清理知识库：① DB 里 title='AI Summary' 的行；② 孤儿向量（DB 已删、向量残留）。

    python scripts/clean_websearch_ai_summaries.py            # 只列出，不改
    python scripts/clean_websearch_ai_summaries.py --apply    # 删除（先备份 meta 库）

- ① 覆盖"AI 摘要（二手合成/重复/含标题党幻觉，如『贴身秘书』）入库"的历史；
- ② 覆盖"DB 删了、向量没删"的残留（如 2026-09-26 首次清理时 delete 接口用错的那批）。
- 向量 id 为 kd_{doc_id}（见 agents/tools/web.py 写入路径）。可重复跑（幂等）。
"""
from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="真正删除（默认只列出）")
    args = ap.parse_args()

    from sqlalchemy import text as _sql

    from core.bootstrap import get_meta_repo, get_vector_store

    repo = get_meta_repo()
    with repo.engine.connect() as con:
        rows = con.execute(_sql("select id, title from knowledge_docs")).mappings().all()
    db_ids = {int(r["id"]) for r in rows}
    ai_ids = [int(r["id"]) for r in rows if str(r["title"]) == "AI Summary"]

    vs = None
    vec_ids: list = []
    try:
        vs = get_vector_store()
        vec_ids = list(vs.client.get_collection("persona_knowledge").get().get("ids", []))
    except Exception as exc:  # noqa: BLE001
        print("[warn] 向量库不可用:", exc)

    orphans = []
    for vid in vec_ids:
        s = str(vid)
        if not s.startswith("kd_"):
            continue
        try:
            if int(s.split("_")[-1]) not in db_ids:
                orphans.append(s)
        except ValueError:
            continue

    print(f"① DB 里 AI Summary 行：{len(ai_ids)} 条")
    shown = orphans[:12]
    print(f"② 孤儿向量（DB 已无对应行）：{len(orphans)} 个 -> {shown}{'...' if len(orphans) > 12 else ''}")

    if not args.apply:
        print("\n[dry-run] 未改动。确认后用 --apply 执行。")
        return 0

    try:
        src = repo.engine.url.database
        if src and Path(src).exists():
            bak = Path(str(src) + ".bak_aisummary_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
            shutil.copy(src, bak)
            print("备份:", bak)
    except Exception as exc:  # noqa: BLE001
        print("  [warn] 备份失败:", exc)

    deleted_db = 0
    deleted_vec = 0
    for doc_id in ai_ids:
        try:
            repo.knowledge_docs.delete(doc_id)
            deleted_db += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  [warn] DB 删除失败 {doc_id}: {str(exc)[:80]}")
    if vs is not None and orphans:
        for k in range(0, len(orphans), 50):
            batch = orphans[k:k + 50]
            try:
                vs.persona_knowledge.delete_batch(ids=batch)
                deleted_vec += len(batch)
            except Exception as exc:  # noqa: BLE001
                print(f"  [warn] 向量删除失败: {str(exc)[:100]}")
    print(f"完成：DB 删除 {deleted_db} 条，向量删除 {deleted_vec} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
