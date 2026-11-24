#!/usr/bin/env python
"""Offline tests for scripts/check_live_readings.py (history log parsing only)."""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for extra in (str(ROOT), str(ROOT / "scripts"), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from _tempdirs import temp_base  # noqa: E402
import check_live_readings as chk  # noqa: E402


def main() -> bool:
    ok = True
    tmp = temp_base() / f"check_live_{os.getpid()}"
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        log = tmp / "yinyue.log"
        log.write_text(
            "\n".join([
                "not json at all",
                json.dumps({"event": "feature_flags", "flag_turn_relevance": "shadow"}),
                json.dumps({"event": "other", "flag_turn_relevance": "off"}),
                json.dumps({"event": "feature_flags", "flag_turn_relevance": "off"}),
            ]) + "\n",
            encoding="utf-8",
        )
        got = chk.latest_feature_flags(log)
        if got.get("flag_turn_relevance") != "off":
            print(f"FAIL latest_feature_flags: {got}")
            ok = False
        empty = chk.latest_feature_flags(tmp / "missing.log")
        if empty:
            print(f"FAIL missing log should be empty: {empty}")
            ok = False

        history = tmp / "history.log"
        history.write_text(
            "\n".join([
                json.dumps({
                    "event": "turn_relevance_shadow", "chat_id": "c1",
                    "origin": "trigger", "trigger": "ambient",
                    "legacy_skip": True, "legacy_reason": "low_signal_ambient",
                    "new_action": "skip", "new_reason": "low_signal_ambient",
                    "new_chain": ["meta:low_signal_ambient"], "diff": False,
                    "timestamp": "2026-10-06T15:01:33Z",
                }, ensure_ascii=False),
                json.dumps({
                    "event": "turn_decision_shadow", "chat_id": "c1",
                    "origin": "trigger", "old_trigger": "ambient",
                    "new_action": "reply_now", "new_reason": "default",
                    "diff": False, "timestamp": "2026-10-06T15:01:33Z",
                }, ensure_ascii=False),
            ]) + "\n",
            encoding="utf-8",
        )
        original = (
            chk.port_listening,
            chk.read_status,
            chk._default_logs,
            chk.latest_feature_flags,
        )
        chk.port_listening = lambda: True
        chk.read_status = lambda: {"started_at": "2026-10-06T19:31:23"}
        chk._default_logs = lambda: [history]
        chk.latest_feature_flags = lambda path=None: {}
        try:
            data = chk.collect()
        finally:
            (
                chk.port_listening,
                chk.read_status,
                chk._default_logs,
                chk.latest_feature_flags,
            ) = original
        if set(data["history"]) != {"turn_relevance_shadow", "turn_decision_shadow"}:
            print(f"FAIL history events: {sorted(data['history'])}")
            ok = False
        relevance = data["history"].get("turn_relevance_shadow", {})
        decision = data["history"].get("turn_decision_shadow", {})
        if relevance.get("rows") != 1 or relevance.get("diff_true") != 0:
            print(f"FAIL relevance history: {relevance}")
            ok = False
        if decision.get("rows") != 1 or decision.get("diff_true") != 0:
            print(f"FAIL decision history: {decision}")
            ok = False
        for summary in (relevance, decision):
            if "ready" in summary or "missing_days" in summary:
                print(f"FAIL history exposes live-window gate: {sorted(summary)}")
                ok = False
        line = chk.history_line("turn_relevance_shadow", relevance)
        if (not line.startswith("history: turn_relevance_shadow rows=1")
                or "ready" in line or "missing_days" in line):
            print(f"FAIL history_line wording: {line}")
            ok = False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if ok:
        print("check live readings: ok")
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
