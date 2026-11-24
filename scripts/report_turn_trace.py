"""E0 读数：``turn_trace`` 报表（开口率 / 弃权率 / 沉默率 / 覆盖率 / 指回率）。

跑法：
    python -u scripts/report_turn_trace.py --days 1

口径（``docs/decisions/判投记_契约.md`` §9.5）：
- 开口率 = (respond + ask) / 全部判决行
- 弃权率 = abstain / 全部判决行
- 沉默率 = silent / 全部判决行
- 覆盖率 = 有 basis 的开口回合 / 开口回合
- 指回率 = 人工抽 50 条里 basis 能定位回原文行的比例（**本脚本只提示口径，不自动算**）

off 档：打印 ``TURN_TRACE=off, 0 rows`` 并 exit 0（验收脚本要能吃这个输出）。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import features  # noqa: E402
from core.terminal import turn_trace as TT  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=1.0)
    ap.add_argument("--chat", default="", help="只看某个会话（可选）")
    args = ap.parse_args(argv)

    mode = str(features.mode(features.TURN_TRACE, features.MODE_OFF) or features.MODE_OFF)
    rows = [] if mode == features.MODE_OFF else TT.iter_turn_trace(
        since=datetime.now() - timedelta(days=max(0.01, args.days))
    )
    if args.chat:
        rows = [r for r in rows if r.chat_id == args.chat]
    if mode == features.MODE_OFF:
        print("TURN_TRACE=off, 0 rows")
        return 0

    total = len(rows)
    if total == 0:
        print(f"TURN_TRACE={mode}, 0 rows (last {args.days:g}d)")
        return 0

    def pct(n: int) -> str:
        return f"{n / total * 100:5.1f}%"

    counts = {"respond": 0, "ask": 0, "abstain": 0, "silent": 0, "defer": 0}
    other = 0
    for r in rows:
        if r.decision in counts:
            counts[r.decision] += 1
        else:
            other += 1

    opened = counts["respond"] + counts["ask"]
    with_basis = sum(1 for r in rows if r.decision in ("respond", "ask") and r.basis)
    cover = f"{with_basis / opened * 100:5.1f}%" if opened else "  n/a"

    print(f"TURN_TRACE={mode}, rows={total}, window={args.days:g}d")
    print(f"  开口率 = (respond+ask)/全部 = ({counts['respond']}+{counts['ask']})/{total} = {pct(opened)}")
    print(f"  弃权率 = abstain/全部 = {counts['abstain']}/{total} = {pct(counts['abstain'])}")
    print(f"  沉默率 = silent/全部 = {counts['silent']}/{total} = {pct(counts['silent'])}")
    if counts["defer"] or other:
        print(f"  （另有 defer={counts['defer']} / 其他={other}）")
    print(f"  覆盖率 = 有 basis 的开口回合/开口回合 = {with_basis}/{opened} = {cover}")
    print("  指回率 = 需人工抽 50 条（E1 起算，本脚本不代算）")
    print(f"  mode 分布：{_mode_hist(rows)}")
    return 0


def _mode_hist(rows) -> str:
    hist: dict[str, int] = {}
    for r in rows:
        hist[r.mode] = hist.get(r.mode, 0) + 1
    return ", ".join(f"{k}={v}" for k, v in sorted(hist.items())) or "-"


if __name__ == "__main__":
    raise SystemExit(main())
