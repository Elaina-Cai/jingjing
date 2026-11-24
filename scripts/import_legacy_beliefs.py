"""审核清单驱动的 Belief 导入；默认 dry。不给 --binding 只认隔离沙箱目标。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.memory.legacy_import import import_reviewed, import_reviewed_production


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--review", required=True, help="v2 审核 JSON：由 audit_legacy_memory.py --review-template 生成；不接受预检状态代替人工确认")
    parser.add_argument("--target", required=True, help=".test_tmp 或系统临时目录 yinyue-legacy-import 下的专用目标库")
    parser.add_argument("--binding", default="", help="生产绑定 JSON：给出时走 import_reviewed_production（目标必须是 belief 读取器的真实库）")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        review = json.loads(Path(args.review).read_text(encoding="utf-8"))
        if not isinstance(review, dict):
            raise ValueError("invalid_review_manifest")
        if args.binding:
            result = import_reviewed_production(args.source, source_id=args.source_id, review=review,
                                                target=args.target, binding=args.binding, dry_run=not args.apply)
        else:
            result = import_reviewed(args.source, source_id=args.source_id, review=review, target=args.target, dry_run=not args.apply)
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({"error": str(exc), "applied": None, "retry_with_same_review": True}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
