#!/usr/bin/env python
"""Summarize WP-05 shadow rows (`turn_decision_shadow`) from production logs.

Only production log files are read by default (`logs/*.log`), so test and
mutation runs never count towards the switch-on gate.

    python scripts/summarize_turn_decision_shadow.py
    python scripts/summarize_turn_decision_shadow.py --require-clean-days 7
    python scripts/summarize_turn_decision_shadow.py --log logs/yinyue.log --json

`--require-clean-days N` exits 0 only when: at least one row exists,
`diff=True` count is 0, and the observed timestamp span is >= N days.

解析/去重/span 逻辑与 WP-04 的 ``summarize_turn_relevance_shadow`` 共用同一份实现
（这里只声明事件名与字段集），避免两套影子汇总漂移（当断则断：同题多实现合并）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = str(ROOT / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import summarize_turn_relevance_shadow as _base  # noqa: E402

MARKER = "turn_decision_shadow"
FIELDS = ("chat_id", "origin", "old_trigger", "new_action", "new_reason", "diff")
DEDUPE_FIELDS = ("old_trigger", "new_action")


def parse_line(line: str) -> dict | None:
    return _base.parse_line(line, marker=MARKER, fields=FIELDS)


def summarize(paths: list[Path]) -> dict:
    return _base.summarize(
        paths, marker=MARKER, fields=FIELDS, dedupe_fields=DEDUPE_FIELDS
    )


def main(argv: list[str] | None = None) -> int:
    return _base.main(
        argv, marker=MARKER, fields=FIELDS, dedupe_fields=DEDUPE_FIELDS,
        description=__doc__,
    )


if __name__ == "__main__":
    raise SystemExit(main())
