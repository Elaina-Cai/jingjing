"""显式选择旧库的只读预检；stdout 输出不含正文的 JSON，无 apply 模式。"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.memory.legacy_audit import audit_database


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="显式指定只读来源库（优先使用离线副本）")
    parser.add_argument("--source-id", required=True, help="来源库稳定逻辑标识；重复预检保持一致，不用临时副本路径作身份")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--review-template", action="store_true", help="输出 v2 待审核模板，所有项暂缓，不含正文")
    mode.add_argument("--check-review", help="只读核对审核清单和当前源快照，报告行/证据漂移")
    args = parser.parse_args(argv)
    try:
        result = audit_database(args.db, source_id=args.source_id)
        if args.review_template:
            from core.memory.legacy_review import review_template
            result = review_template(result)
        elif args.check_review:
            from core.memory.legacy_review import check_review
            review = json.loads(Path(args.check_review).read_text(encoding="utf-8"))
            result = check_review(result, review)
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({"error": str(exc), "read_only": True}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not args.check_review or result["ok"] else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
