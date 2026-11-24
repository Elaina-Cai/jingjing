"""幂等补历史入站消息的平台号、发送者和引用元数据。

只处理 world.db 中 platform_msg_id 为空的行，不覆盖已有平台号。
默认每账号每轮最多 5000 条，重复到没有新的更新为止。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from db import world_db


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit-per-account", type=int, default=5000)
    parser.add_argument("--max-rounds", type=int, default=100)
    args = parser.parse_args()
    if args.limit_per_account <= 0 or args.max_rounds <= 0:
        parser.error("limit 与 max-rounds 必须大于 0")

    totals = {"scanned": 0, "updated": 0, "rounds": 0}
    for _ in range(args.max_rounds):
        result = world_db.backfill_missing_message_metadata(args.limit_per_account)
        totals["rounds"] += 1
        totals["scanned"] += int(result.get("scanned") or 0)
        totals["updated"] += int(result.get("updated") or 0)
        print(json.dumps(result, ensure_ascii=False))
        if not result.get("scanned") or not result.get("updated"):
            break
    print("TOTAL " + json.dumps(totals, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
