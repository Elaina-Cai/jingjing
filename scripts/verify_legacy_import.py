"""只读核对旧库与隔离导入结果；不执行生产切换。"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from core.memory.legacy_acceptance import verify_cutover, verify_import


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--target", required=True, help="已存在的隔离导入目标")
    parser.add_argument("--at", default="", help="本地事实有效时间；默认现在")
    parser.add_argument("--production", action="store_true", help="目标是生产 belief 库时显式声明")
    parser.add_argument("--binding", default="", help="读取权绑定 JSON；提供时才在完整性/停写/绑定三项都成立后计算 cutover_ready")
    parser.add_argument("--require-cutover", action="store_true", help="读取权切换未证明时返回非零；不会执行切换")
    args = parser.parse_args(argv)
    try:
        if args.binding:
            from sqlalchemy import create_engine
            from core.memory.legacy_read_binding import load_read_binding
            from db.repo_memory_projections import MemoryProjectionRepository
            binding = load_read_binding(args.binding)
            engine = create_engine("sqlite:///" + str(binding.source))
            try:
                result = verify_cutover(args.source, source_id=args.source_id, target=args.target,
                                        binding=binding, repository=MemoryProjectionRepository(engine), at=args.at,
                                        production=args.production)
            finally:
                engine.dispose()
        else:
            result = verify_import(args.source, source_id=args.source_id, target=args.target, at=args.at,
                               production=args.production)
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({"error": str(exc), "read_only": True}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["integrity_ok"]:
        return 1
    return 2 if args.require_cutover and not result["cutover_ready"] else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
