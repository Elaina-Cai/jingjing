"""自检入口：`python -X utf8 -m scripts.selfcheck [--json] [--network]`。

只读：默认不连 CDP、不模拟按键、不写账本。出口码 = 0 表示七节里没有 ❌。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.execution.gui.selfcheck import run_selfcheck  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    report = run_selfcheck(probe_network="--network" in args)
    if "--json" in args:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(report.text())
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
