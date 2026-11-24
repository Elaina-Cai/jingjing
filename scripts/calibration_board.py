#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""B5 判断校准看板：把「系统否决 / 出稿 / 自指改写」的线上信号汇成可复核的表。

只读日志，不改任何东西。用途：每次事故后 / 每周复核——
* 系统否决（meta_gate / 2c 指代）否决了哪些回合：reason 分布 + 样本原文 → 抽查误杀；
* 出稿把草稿删了多少、多少次变成沉默 → 抽查过度删减；
* 自指改写触发几次、失败几次 → 抽查改写质量。

用法：
    python scripts/calibration_board.py                # 近 7 天 logs/yinyue.log
    python scripts/calibration_board.py --days 1
    python scripts/calibration_board.py --json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DEFAULT_LOG = ROOT / "logs" / "yinyue.log"

_SKIP_PREFIXES = ("meta_", "low_signal_ambient", "other_pair_qa_not_about_her")


def _parse_ts(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(
            tzinfo=None
        )
    except Exception:  # noqa: BLE001
        return None


def summarize(records, *, days: int | None = None, samples: int = 5) -> dict:
    """Aggregate the calibration-relevant events from parsed log records."""
    cutoff = None
    if days:
        cutoff = datetime.now() - timedelta(days=max(1, int(days)))
    out = {
        "skips": {},           # reason -> count
        "skip_samples": {},    # reason -> [text, ...]
        "render": {"count": 0, "draft_chars": 0, "speak_chars": 0, "silent": 0},
        "render_failed": 0,
        "self_rewrite": 0,
        "self_rewrite_failed": 0,
        "blind_vision_blocked": 0,
    }
    for record in records or ():
        if not isinstance(record, dict):
            continue
        if cutoff is not None:
            ts = _parse_ts(record.get("timestamp"))
            if ts is not None and ts < cutoff:
                continue
        event = str(record.get("event") or "")
        if event == "group_turn_skipped":
            reason = str(record.get("reason") or "")
            if not any(reason.startswith(p) for p in _SKIP_PREFIXES):
                continue
            out["skips"][reason] = out["skips"].get(reason, 0) + 1
            text = str(record.get("content") or "").strip()
            if text:
                bucket = out["skip_samples"].setdefault(reason, [])
                if text not in bucket and len(bucket) < max(1, int(samples)):
                    bucket.append(text)
        elif event == "speak_contract_rendered":
            out["render"]["count"] += 1
            out["render"]["draft_chars"] += int(record.get("draft_chars") or 0)
            out["render"]["speak_chars"] += int(record.get("speak_chars") or 0)
            if record.get("silent"):
                out["render"]["silent"] += 1
        elif event == "speak_render_failed":
            out["render_failed"] += 1
        elif event == "self_voice_rewritten":
            out["self_rewrite"] += 1
        elif event == "self_voice_rewrite_failed":
            out["self_rewrite_failed"] += 1
        elif event == "vision_blind_answer_blocked":
            out["blind_vision_blocked"] += 1
    return out


def _format(summary: dict, days: int | None) -> str:
    window = f"近 {days} 天" if days else "全部"
    lines = [f"== 判断校准看板（{window}）=="]
    skips = summary["skips"]
    total_skips = sum(skips.values())
    lines.append(f"系统否决：{total_skips} 次")
    for reason, count in sorted(skips.items(), key=lambda kv: -kv[1]):
        samples = summary["skip_samples"].get(reason) or []
        sample_text = " / ".join(samples) if samples else "(无样本)"
        lines.append(f"  {reason:<28} {count:>3} 例：{sample_text}")
    render = summary["render"]
    if render["count"]:
        draft = render["draft_chars"]
        speak = render["speak_chars"]
        ratio = f"{(speak / draft * 100):.0f}%" if draft else "n/a"
        lines.append(
            f"出稿：{render['count']} 次；草稿 {draft} 字 → 说出口 {speak} 字"
            f"（留存 {ratio}）；出稿后沉默 {render['silent']} 次"
        )
    else:
        lines.append("出稿：0 次")
    lines.append(f"出稿失败：{summary['render_failed']} 次")
    lines.append(
        f"自指改写：{summary['self_rewrite']} 次（失败 "
        f"{summary['self_rewrite_failed']} 次）"
    )
    if summary["blind_vision_blocked"]:
        lines.append(f"盲答图片被拦：{summary['blind_vision_blocked']} 次")
    lines.append("复核建议：抽查否决样本是否误杀；抽查出稿留存率是否过低。")
    return "\n".join(lines)


def load_records(path: Path) -> list[dict]:
    records: list[dict] = []
    try:
        for raw in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                item = json.loads(raw)
            except Exception:  # noqa: BLE001
                continue
            if isinstance(item, dict):
                records.append(item)
    except Exception:  # noqa: BLE001
        return []
    return records


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", default=str(DEFAULT_LOG))
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    summary = summarize(
        load_records(Path(args.log)), days=args.days, samples=args.samples
    )
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(_format(summary, args.days))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
