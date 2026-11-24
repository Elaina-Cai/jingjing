#!/usr/bin/env python
"""Tests for scripts/summarize_turn_decision_shadow.py (WP-05 part 3a gate)."""
from __future__ import annotations

import json
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
import summarize_turn_decision_shadow as summary  # noqa: E402


def _line(ts: str, diff: str, old: str = "ambient", action: str = "reply_now",
          origin: str = "trigger", chat: str = "c1") -> str:
    return (
        f"{ts} [info     ] turn_decision_shadow chat_id={chat} origin={origin} "
        f"old_trigger={old} new_action={action} new_reason=default "
        f"new_chain=['trigger:ambient'] diff={diff}"
    )


def main() -> bool:
    ok = True
    tmp = temp_base() / f"turn_decision_summary_{os.getpid()}"
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        log = tmp / "yinyue.log"
        log.write_text(
            "\n".join([
                _line("2026-10-01 10:00:00", "False"),
                _line("2026-10-04 10:00:00", "False", origin="direct"),
                _line("2026-10-08 10:00:00", "True", old="none", action="silent"),
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
        script = str(ROOT / "scripts" / "summarize_turn_decision_shadow.py")
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

    json_line = json.dumps({
        "event": "turn_decision_shadow",
        "chat_id": "c1",
        "origin": "trigger",
        "old_trigger": "deferred",
        "new_action": "defer",
        "new_reason": "group_deferred_to_state",
        "new_chain": ["trigger:deferred"],
        "diff": False,
        "timestamp": "2026-10-06T15:01:33.033359Z",
    }, ensure_ascii=False)
    parsed = summary.parse_line(json_line)
    if (not parsed or parsed.get("diff") is not False
            or parsed.get("old_trigger") != "deferred"
            or parsed.get("new_action") != "defer"):
        print(f"FAIL JSONL parse: {parsed}")
        ok = False

    console = _line("2026-10-06T15:01:33", "False", old="deferred", action="defer")
    ansi_console = console.replace(
        "chat_id", "\x1b[36mchat_id\x1b[0m").replace("diff", "\x1b[33mdiff\x1b[0m")
    parsed_ansi = summary.parse_line(ansi_console)
    if (not parsed_ansi or parsed_ansi.get("chat_id") != "c1"
            or parsed_ansi.get("old_trigger") != "deferred"):
        print(f"FAIL ANSI console parse: {parsed_ansi}")
        ok = False

    dup_dir = temp_base() / f"turn_decision_summary_dup_{os.getpid()}"
    dup_dir.mkdir(parents=True, exist_ok=True)
    try:
        dup = dup_dir / "dup.log"
        dup.write_text(json_line + "\n" + ansi_console + "\n", encoding="utf-8")
        got_dup = summary.summarize([dup])
        if got_dup["rows"] != 1 or got_dup["diff_true"] != 0:
            print(f"FAIL dedup across JSON/console: {got_dup}")
            ok = False
    finally:
        shutil.rmtree(dup_dir, ignore_errors=True)
    if ok:
        print("turn decision summary: ok")
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
