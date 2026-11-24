#!/usr/bin/env python
"""向量索引体检：覆盖率 / 停更时间 / 嵌入可用性（P3）。

背景：2026-09-22 实测索引停在 09-21 22:10，覆盖 1008/1797（56%），
且注入路径 join 0.35s 而 embed ~1.2s —— 实际贡献≈0，还会静默停更。
P3 的决定是**停用**（YINYUE_FEATURE_MEMORY_VECTOR_RECALL=0），并用这个脚本
把"停更"变成看得见的事实；将来要恢复，必须先补上"游标 + 增量 + lag 告警"。

    python scripts/memory_index_status.py
    python scripts/memory_index_status.py --json

归属：向量索引/投影体检（覆盖 56% 时告警）；P3 决定向量召回停用；设计 docs/记忆的定义.md §9。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def collect() -> dict:
    from sqlalchemy import create_engine, text

    meta = ROOT / "data" / "yinyue_meta.db"
    engine = create_engine(f"sqlite:///{meta.as_posix()}")
    with engine.connect() as conn:
        total = conn.execute(text("select count(*) from memory_projections")).scalar() or 0
        newest = conn.execute(
            text("select max(created_at) from memory_projections")
        ).scalar()
    engine.dispose()

    vector_ids: set[str] = set()
    vector_error = ""
    try:
        from core.bootstrap import get_vector_store

        collection = get_vector_store().client.get_collection("memory_embeddings")
        data = collection.get(include=["metadatas"])
        vector_ids = {str(i) for i in data.get("ids", [])}
    except Exception as exc:  # noqa: BLE001
        vector_error = str(exc)[:160]

    indexed = len(vector_ids)
    covered = 0
    if vector_ids:
        engine = create_engine(f"sqlite:///{meta.as_posix()}")
        with engine.connect() as conn:
            covered = sum(
                1
                for (mid,) in conn.execute(text("select memory_id from memory_projections"))
                if str(mid) in vector_ids
            )
        engine.dispose()

    embedding_ok, embedding_error = False, ""
    try:
        import asyncio

        from core.bootstrap import get_embedding

        client = get_embedding()
        if client is not None:
            asyncio.run(client.embed("体检"))
            embedding_ok = True
    except Exception as exc:  # noqa: BLE001
        embedding_error = str(exc)[:160]

    cursor, newest = _projection_lag()
    skewed = _timezone_skewed_rows()

    return {
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "projection_cursor": cursor,
        "newest_event_sequence": newest,
        "projection_lag": max(0, newest - cursor),
        "timezone_skewed": skewed,
        "projections": total,
        "indexed_ids": indexed,
        "covered": covered,
        "coverage": round(covered / total, 4) if total else 0.0,
        "newest_projection": str(newest or ""),
        "embedding_available": embedding_ok,
        "vector_error": vector_error,
        "embedding_error": embedding_error,
        "recall_enabled": _flag("YINYUE_FEATURE_MEMORY_VECTOR_RECALL", True),
    }


def _timezone_skewed_rows() -> int:
    """统计"occurred_at 比入库时间早整 8 小时"的行（应为 0）。

    2026-09-22 修过一个坑：她自己的 thought/reply/action 默认写 UTC，比入站消息
    早 8 小时，窗口按天/小时分桶全乱（459 条已回填）。这个数字 >0 就说明又混了。
    """
    from sqlalchemy import create_engine, text

    meta = ROOT / "data" / "yinyue_meta.db"
    engine = create_engine(f"sqlite:///{meta.as_posix()}")
    try:
        with engine.connect() as conn:
            return int(
                conn.execute(
                    text(
                        "select count(*) from terminal_events "
                        "where created_at is not null and occurred_at is not null "
                        "and abs((julianday(created_at) - julianday(occurred_at)) * 24 - 8) * 60 <= 10"
                    )
                ).scalar()
                or 0
            )
    except Exception:
        return -1
    finally:
        engine.dispose()


def _projection_lag() -> tuple[int, int]:
    """投影落后多少条事件（cursor vs 最新 sequence）。"""
    from sqlalchemy import create_engine, text

    meta = ROOT / "data" / "yinyue_meta.db"
    engine = create_engine(f"sqlite:///{meta.as_posix()}")
    try:
        with engine.connect() as conn:
            cursor = conn.execute(
                text(
                    "select last_sequence from memory_projection_cursors "
                    "where key = 'terminal_event'"
                )
            ).scalar()
            newest = conn.execute(text("select max(sequence) from terminal_events")).scalar()
        return int(cursor or 0), int(newest or 0)
    except Exception:
        return 0, 0
    finally:
        engine.dispose()


def _flag(name: str, default: bool) -> bool:
    import os

    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    info = collect()
    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0
    print(f"检查时间：{info['checked_at']}")
    print(
        f"投影 {info['projections']} 条 · 已索引 {info['covered']} 条 "
        f"（覆盖 {info['coverage']:.0%}）· 索引集合 {info['indexed_ids']} 条"
    )
    print(f"最新投影：{info['newest_projection']}")
    print(
        f"投影滞后：{info['projection_lag']} 条"
        f"（cursor={info['projection_cursor']} / 最新事件={info['newest_event_sequence']}）"
    )
    print(f"向量召回旗标：{'开' if info['recall_enabled'] else '关（P3 决定：停用）'}")
    print(f"嵌入服务：{'可用' if info['embedding_available'] else '不可用'}")
    skew = info["timezone_skewed"]
    print(f"时间戳一致性：{'OK（0 条差 8 小时）' if skew == 0 else f'⚠️ {skew} 条差 8 小时，需要修（event_model/回溯）'}")
    if info["vector_error"]:
        print(f"  索引读取错误：{info['vector_error']}")
    if info["embedding_error"]:
        print(f"  嵌入错误：{info['embedding_error']}")
    if info["coverage"] < 1.0 and info["recall_enabled"]:
        print("⚠️ 索引不完整但召回是开的：要么补索引，要么把旗标关掉（不许静默停更）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
