"""Golden-set schema checks (P1).

Run directly: python tests/test_golden_set.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.observability.golden import load_cases, summarize, validate_cases

_CASES_PATH = Path(__file__).resolve().parent / "golden" / "group_conversation_cases.json"


def main() -> bool:
    print("=" * 50)
    print("golden set checks")
    print("=" * 50)

    cases = load_cases(_CASES_PATH)
    problems = validate_cases(cases)
    summary = summarize(cases)

    ok = len(cases) >= 10
    ok &= not problems
    ok &= summary["actions"].get("reply", 0) >= 1
    ok &= summary["actions"].get("drop", 0) >= 1
    ok &= summary["actions"].get("close", 0) >= 1
    ok &= summary["chat_types"].get("group", 0) >= 1
    ok &= summary["chat_types"].get("private", 0) >= 1

    print(f"  cases: {len(cases)}")
    print(f"  actions: {summary['actions']}")
    print(f"  chat_types: {summary['chat_types']}")
    if problems:
        for problem in problems:
            print(f"  FAIL {problem}")
    print("  ok" if ok else "  FAIL")
    return bool(ok)


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
