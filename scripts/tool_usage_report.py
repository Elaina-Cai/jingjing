"""工具用量报告：谁在用、谁从没被调用过（合并与下线的依据，不是感觉）。

    python -X utf8 -m scripts.tool_usage_report
    python -X utf8 -m scripts.tool_usage_report --days 7
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "logs" / "yinyue.log"
CALL_RE = re.compile(r'"tool":\s*"([a-z_0-9]+)"')
STEP_RE = re.compile(r"job_step .*?action=(?:send:|)([a-z_0-9]+)")


def scan(path: Path, *, since: datetime) -> Counter:
    counts: Counter = Counter()
    if not path.exists():
        return counts
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if "agent_tool_call" in line:
                for name in CALL_RE.findall(line):
                    counts[name] += 1
            elif "job_step" in line:
                for name in STEP_RE.findall(line):
                    counts[name] += 1
    return counts


def declared() -> list:
    from core.tools.declared import declared_specs

    return [spec.name for spec in declared_specs()]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    args = ap.parse_args()
    since = datetime.now() - timedelta(days=args.days)
    counts = scan(LOG, since=since)
    names = sorted(set(declared()) | set(counts), key=lambda n: (-counts.get(n, 0), n))
    used = [n for n in names if counts.get(n, 0) > 0]
    never = [n for n in names if counts.get(n, 0) == 0]
    print("== 在用（近 %d 天） ==" % args.days)
    for name in used:
        print("  %-22s %d" % (name, counts[name]))
    print("== 从未调用 ==")
    for name in never:
        print("  %-22s 0" % name)
    print(json.dumps({"used": len(used), "never": len(never)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
