#!/usr/bin/env python
"""Read-only checker for retired shadow emitters + WP-07 counters.

    python scripts/check_live_readings.py
    python scripts/check_live_readings.py --json

Checks (all read-only):
  1. port 8972 + /api/status (is the scheduler actually up, and since when);
  2. newest ``feature_flags`` log line: the WP-04/WP-05 shadow flags must be
     gone (both emitters are retired, so no shadow flag remains);
  3. historical ``turn_relevance_shadow`` / ``turn_decision_shadow`` rows in
     logs: rows / diff_true / span_days only.  Both emitters are deleted, so
     these events can no longer grow and are not a live switch-on gate;
  4. ``legacy_merge_path_used`` counter lines (WP-07).
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import summarize_turn_decision_shadow as decision_history  # noqa: E402
import summarize_turn_relevance_shadow as relevance_history  # noqa: E402

LOG = ROOT / "logs" / "yinyue.log"
PORT = 8972


def port_listening() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=1.0):
            return True
    except OSError:
        return False


def read_status() -> dict:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{PORT}/api/status", timeout=3
        ) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:160]}


def latest_feature_flags(path: Path = LOG) -> dict:
    """Newest ``event=feature_flags`` row from the JSONL log (tail only)."""
    if not path.exists():
        return {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-4000:]
    except OSError:
        return {}
    found: dict = {}
    for line in lines:
        try:
            row = json.loads(line)
        except Exception:
            continue
        if row.get("event") == "feature_flags":
            found = row
    return found


def _default_logs() -> list[Path]:
    logs = ROOT / "logs"
    return sorted(logs.glob("*.log")) if logs.is_dir() else []


def counter_lines(needle: str) -> int:
    count = 0
    for path in _default_logs():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        count += text.count(needle)
    return count


def collect() -> dict:
    flags = latest_feature_flags()
    logs = _default_logs()
    return {
        "port_listening": port_listening(),
        "status": read_status(),
        "feature_flags": {
            "flag_turn_relevance": flags.get("flag_turn_relevance", ""),
            "flag_semantic_unit": flags.get("flag_semantic_unit", ""),
            "flag_unit_media": flags.get("flag_unit_media", ""),
            "flag_pat_grace": flags.get("flag_pat_grace", ""),
            "flag_turn_ownership": flags.get("flag_turn_ownership", ""),
            "flag_turn_decision": flags.get("flag_turn_decision", ""),
        },
        "history": {
            "turn_relevance_shadow": relevance_history.summarize(logs),
            "turn_decision_shadow": decision_history.summarize(logs),
        },
        "legacy_merge_path_used": counter_lines("legacy_merge_path_used"),
    }


def history_line(event: str, summary: dict) -> str:
    """One history-only readout line (no live-window gate after emitter retire)."""
    return (
        f"history: {event} rows={summary['rows']} "
        f"diff_true={summary['diff_true']} span_days={summary['span_days']} "
        f"window={summary['first_ts'] or '-'} -> {summary['last_ts'] or '-'}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    data = collect()
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
        return 0
    status = data["status"] if isinstance(data["status"], dict) else {}
    print(
        f"port8972={'LISTENING' if data['port_listening'] else 'down'} "
        f"started_at={status.get('started_at', '?')} "
        f"sync_count={status.get('sync_count', '?')} "
        f"last_sync={((status.get('last_sync') or {}).get('finished_at') or '?')}"
    )
    flags = data["feature_flags"]
    print(
        "feature_flags: "
        f"turn_relevance={flags['flag_turn_relevance'] or '(retired)'} "
        f"semantic_unit={flags['flag_semantic_unit'] or '?'} "
        f"unit_media={flags['flag_unit_media'] or '?'} "
        f"pat_grace={flags['flag_pat_grace'] or '?'} "
        f"turn_ownership={flags['flag_turn_ownership'] or '?'} "
        f"turn_decision={flags['flag_turn_decision'] or '(retired)'}"
    )
    history = data["history"]
    for event in ("turn_relevance_shadow", "turn_decision_shadow"):
        print(history_line(event, history[event]))
    print(f"legacy_merge_path_used={data['legacy_merge_path_used']}")
    relevance_retired = not flags.get("flag_turn_relevance")
    decision_retired = not flags.get("flag_turn_decision")
    print(
        f"retired: turn_relevance={'yes' if relevance_retired else 'NO (flag still logged)'} "
        f"| turn_decision={'yes' if decision_retired else 'NO (flag still logged)'}"
    )
    verdict = (
        "history_only"
        if relevance_retired and decision_retired
        else "flag_still_logged"
    )
    print(f"verdict: {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
