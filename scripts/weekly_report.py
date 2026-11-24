"""周报入口：`python -X utf8 -m scripts.weekly_report [--days 7] [--json] [--out 路径]`。

只读：查 turn_trace / delivery_receipts / job_events，不写任何表、不改开关。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.observability import weekly  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    days = weekly.DEFAULT_DAYS
    if "--days" in args:
        try:
            days = int(args[args.index("--days") + 1])
        except (IndexError, ValueError):
            print("用法：--days 7")
            return 2
    report = weekly.build_weekly(days=days)
    text = json.dumps(report.as_dict(), ensure_ascii=False, indent=2) if "--json" in args else report.markdown()
    if "--out" in args:
        try:
            out = Path(args[args.index("--out") + 1])
        except IndexError:
            print("用法：--out 路径")
            return 2
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"已写入 {out}")
        return 0
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
