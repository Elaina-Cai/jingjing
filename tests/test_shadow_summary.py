#!/usr/bin/env python
"""Tests for scripts/summarize_turn_relevance_shadow.py (WP-04 part 3 gate)."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for extra in (str(ROOT), str(ROOT / "scripts"), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from _tempdirs import temp_base  # noqa: E402
import summarize_turn_relevance_shadow as summary  # noqa: E402


def _line(ts: str, diff: str, origin: str = "trigger", chat: str = "c1") -> str:
    return (
        f"{ts} [info     ] turn_relevance_shadow chat_id={chat} origin={origin} "
        f"trigger=ambient legacy_skip=False legacy_reason=default new_action=take "
        f"new_reason=ownership_addressee_her new_chain=['ownership:take_by_addressee'] "
        f"diff={diff}"
    )


def main() -> bool:
    ok = True
    tmp = temp_base() / f"shadow_summary_{os.getpid()}"
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        log = tmp / "yinyue.log"
        log.write_text(
            "\n".join([
                _line("2026-10-01 10:00:00", "False"),
                _line("2026-10-04 10:00:00", "False", origin="direct"),
                _line("2026-10-08 10:00:00", "True"),
                "not a shadow line",
            ]) + "\n",
            encoding="utf-8",
        )
        got = summary.summarize([log])
        if got["rows"] != 3 or got["diff_true"] != 1 or got["diff_false"] != 2:
            print(f"FAIL summarize: {got}")
            ok = False
        if got["span_days"] < 7 or got["by_origin"].get("direct") != 1:
            print(f"FAIL span/origin: {got}")
            ok = False
        clean = tmp / "clean.log"
        clean.write_text(
            "\n".join([
                _line("2026-10-01 10:00:00", "False"),
                _line("2026-10-08 10:00:00", "False"),
            ]) + "\n",
            encoding="utf-8",
        )
        script = str(ROOT / "scripts" / "summarize_turn_relevance_shadow.py")
        bad = subprocess.run(
            [sys.executable, script, "--log", str(log), "--require-clean-days", "7"],
            capture_output=True, text=True,
        )
        if bad.returncode == 0:
            print("FAIL: 有 diff=True 时门禁应失败")
            ok = False
        good = subprocess.run(
            [sys.executable, script, "--log", str(clean), "--require-clean-days", "7"],
            capture_output=True, text=True,
        )
        if good.returncode != 0:
            print(f"FAIL: 干净且跨 7 天应通过：{good.stdout} {good.stderr}")
            ok = False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    import json as _json

    json_line = _json.dumps({
        "event": "turn_relevance_shadow",
        "chat_id": "c1",
        "origin": "trigger",
        "trigger": "ambient",
        "legacy_skip": True,
        "legacy_reason": "ownership_clarify_not_taken",
        "new_action": "skip",
        "new_reason": "ownership_clarify_not_taken",
        "diff": False,
        "timestamp": "2026-10-06T15:01:33.033359Z",
    }, ensure_ascii=False)
    parsed = summary.parse_line(json_line)
    if not parsed or parsed.get("diff") is not False or parsed.get(
            "legacy_reason") != "ownership_clarify_not_taken":
        print(f"FAIL JSONL parse: {parsed}")
        ok = False
    console = (
        "2026-10-06T15:01:33 [info     ] turn_relevance_shadow chat_id=c1 "
        "origin=trigger trigger=ambient legacy_skip=True "
        "legacy_reason=ownership_clarify_not_taken new_action=skip "
        "new_reason=ownership_clarify_not_taken "
        "new_chain=['ownership:ownership_clarify_not_taken'] diff=False"
    )
    ansi_console = console.replace(
        "chat_id", "\x1b[36mchat_id\x1b[0m").replace("diff", "\x1b[33mdiff\x1b[0m")
    parsed_ansi = summary.parse_line(ansi_console)
    if not parsed_ansi or parsed_ansi.get("chat_id") != "c1" or parsed_ansi.get("diff") is not False:
        print(f"FAIL ANSI console parse: {parsed_ansi}")
        ok = False
    dup_dir = Path(tmp)
    dup_dir.mkdir(parents=True, exist_ok=True)
    dup = dup_dir / "dup.log"
    dup.write_text(json_line + "\n" + ansi_console + "\n", encoding="utf-8")
    got_dup = summary.summarize([dup])
    if got_dup["rows"] != 1 or got_dup["diff_true"] != 0:
        print(f"FAIL dedup across JSON/console: {got_dup}")
        ok = False
    shutil.rmtree(dup_dir, ignore_errors=True)
    if ok:
        print("shadow summary: ok")
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
