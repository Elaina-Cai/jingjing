#!/usr/bin/env python
"""Summarize WP-04 shadow rows (`turn_relevance_shadow`) from production logs.

Only production log files are read by default (`logs/*.log`), so test and
mutation runs never count towards the switch-on gate.

    python scripts/summarize_turn_relevance_shadow.py
    python scripts/summarize_turn_relevance_shadow.py --require-clean-days 7
    python scripts/summarize_turn_relevance_shadow.py --log logs/yinyue.log --json

`--require-clean-days N` exits 0 only when: at least one row exists,
`diff=True` count is 0, and the observed timestamp span is >= N days.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_FIELDS = ("chat_id", "origin", "trigger", "legacy_skip", "legacy_reason",
           "new_action", "new_reason", "diff")
_DEDUPE_FIELDS = ("legacy_reason", "new_action")
_BOOL_FIELDS = frozenset({"legacy_skip", "diff"})
_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})")
_MARKER = "turn_relevance_shadow"


@lru_cache(maxsize=None)
def _field_patterns(fields: tuple[str, ...]) -> dict[str, re.Pattern[str]]:
    """Each field set compiles once; WP-04/WP-05 shadow rows share this parser."""
    return {key: re.compile(rf"\b{key}=([^\s]+)") for key in fields}


_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def parse_line(line: str, *, marker: str = _MARKER,
               fields: tuple[str, ...] = _FIELDS) -> dict | None:
    """Parse one shadow row from either JSONL or ANSI console output."""
    stripped = str(line or "").strip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except Exception:
            data = None
        if isinstance(data, dict) and data.get("event") == marker:
            row: dict = {}
            for key in fields:
                row[key] = (bool(data.get(key)) if key in _BOOL_FIELDS
                            else str(data.get(key) or ""))
            row["ts"] = str(data.get("timestamp") or "")
            return row if row.get("diff") is not None else None
    clean = _ANSI.sub("", str(line or ""))
    if marker not in clean:
        return None
    row = {}
    for key, pattern in _field_patterns(tuple(fields)).items():
        match = pattern.search(clean)
        if match is None:
            continue
        value: object = match.group(1)
        if key in _BOOL_FIELDS:
            value = str(value).strip().lower() in ("true", "1", "yes")
        row[key] = value
    ts_match = _TS.match(clean.strip())
    row["ts"] = ts_match.group(1) if ts_match else ""
    return row if row.get("diff") is not None else None


def _parse_stamp(value: object) -> datetime | None:
    """Parse a shadow timestamp; normalise aware values to naive UTC.

    JSONL rows carry ``...Z`` (timezone-aware) while older console rows are
    naive; comparing the two directly raises TypeError.
    """
    try:
        stamp = datetime.fromisoformat(str(value or "").replace(" ", "T"))
    except ValueError:
        return None
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone(timezone.utc).replace(tzinfo=None)
    return stamp


def summarize(paths: list[Path], *, marker: str = _MARKER,
              fields: tuple[str, ...] = _FIELDS,
              dedupe_fields: tuple[str, ...] = _DEDUPE_FIELDS) -> dict:
    rows: list[dict] = []
    seen: set[tuple] = set()
    files_read = 0
    for path in paths:
        if not path.is_file():
            continue
        files_read += 1
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            row = parse_line(line, marker=marker, fields=fields)
            if row is None:
                continue
            key = (
                str(row.get("ts") or "")[:19],
                str(row.get("chat_id") or ""),
                *[str(row.get(field) or "") for field in dedupe_fields],
                bool(row.get("diff")),
            )
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)
    diffs = [row for row in rows if row.get("diff")]
    stamps = []
    for row in rows:
        stamp = _parse_stamp(row.get("ts"))
        if stamp is not None:
            stamps.append(stamp)
    span_days = 0.0
    if len(stamps) >= 2:
        span_days = (max(stamps) - min(stamps)).total_seconds() / 86400.0
    by_origin: dict[str, int] = {}
    for row in rows:
        key = str(row.get("origin") or "?")
        by_origin[key] = by_origin.get(key, 0) + 1
    return {
        "files_read": files_read,
        "rows": len(rows),
        "diff_true": len(diffs),
        "diff_false": len(rows) - len(diffs),
        "first_ts": min(stamps).isoformat(sep=" ") if stamps else "",
        "last_ts": max(stamps).isoformat(sep=" ") if stamps else "",
        "span_days": round(span_days, 3),
        "by_origin": by_origin,
        "diff_examples": [
            {key: row.get(key) for key in ("ts", *fields)}
            for row in diffs[:10]
        ],
    }


def _default_logs() -> list[Path]:
    logs = ROOT / "logs"
    return sorted(logs.glob("*.log")) if logs.is_dir() else []


def main(argv: list[str] | None = None, *, marker: str = _MARKER,
         fields: tuple[str, ...] = _FIELDS,
         dedupe_fields: tuple[str, ...] = _DEDUPE_FIELDS,
         description: str | None = None) -> int:
    parser = argparse.ArgumentParser(description=description or __doc__)
    parser.add_argument("--log", action="append", default=[],
                        help="log file path; repeatable (default: logs/*.log)")
    parser.add_argument("--require-clean-days", type=float, default=None,
                        help="exit 0 only when diff=True is 0 and span >= N days")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    paths = [Path(p) for p in args.log] if args.log else _default_logs()
    summary = summarize(paths, marker=marker, fields=fields,
                        dedupe_fields=dedupe_fields)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(
            f"files={summary['files_read']} rows={summary['rows']} "
            f"diff_true={summary['diff_true']} diff_false={summary['diff_false']} "
            f"span_days={summary['span_days']} origins={summary['by_origin']}"
        )
        if summary["first_ts"]:
            print(f"window: {summary['first_ts']} -> {summary['last_ts']}")
        for row in summary["diff_examples"]:
            print(f"  diff: {row}")
    if args.require_clean_days is not None:
        need = float(args.require_clean_days)
        ok = (
            summary["rows"] > 0
            and summary["diff_true"] == 0
            and summary["span_days"] >= need
        )
        print(
            f"{'PASS' if ok else 'FAIL'}: require_clean_days={need} "
            f"(rows={summary['rows']}, diff_true={summary['diff_true']}, "
            f"span_days={summary['span_days']})"
        )
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
