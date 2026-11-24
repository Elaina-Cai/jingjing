"""Golden-set loading and validation for conversation decisions (P1).

The golden set is deliberately data-only: it describes the conversation
situation, the incoming message and the expected decision.  It does not import
the router or any resolver, so it can be written before the new engine exists
and used later for shadow comparison and regression tests.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping

ALLOWED_ACTIONS = frozenset({"reply", "observe", "drop", "close", "defer"})
ALLOWED_ADDRESSEES = frozenset({"bot", "person", "group", "multiple", "unknown"})
ALLOWED_CHAT_TYPES = frozenset({"private", "group"})
ALLOWED_DIALOGUE_ACTS = frozenset({
    "greeting", "question_to_bot", "question_to_group", "answer_to_bot",
    "ack", "closing", "command", "banter", "broadcast", "other_addressed",
    "media", "unknown",
})


def load_cases(path: str | Path) -> list[dict[str, Any]]:
    """Load a golden-set JSON file and return its case list."""
    target = Path(path)
    data = json.loads(target.read_text(encoding="utf-8"))
    cases = data.get("cases", []) if isinstance(data, Mapping) else []
    return [dict(case) for case in cases if isinstance(case, Mapping)]


def validate_case(case: Mapping[str, Any]) -> list[str]:
    """Return a list of human-readable validation problems (empty = valid)."""
    problems: list[str] = []

    for key in ("id", "scenario", "chat_type", "message", "expected"):
        if not case.get(key):
            problems.append(f"missing field: {key}")

    if case.get("chat_type") not in ALLOWED_CHAT_TYPES:
        problems.append(f"bad chat_type: {case.get('chat_type')!r}")

    expected = case.get("expected")
    if not isinstance(expected, Mapping):
        return problems + ["expected must be an object"]

    if expected.get("action") not in ALLOWED_ACTIONS:
        problems.append(f"bad expected.action: {expected.get('action')!r}")
    if expected.get("addressee") not in ALLOWED_ADDRESSEES:
        problems.append(f"bad expected.addressee: {expected.get('addressee')!r}")
    if expected.get("dialogue_act") not in ALLOWED_DIALOGUE_ACTS:
        problems.append(f"bad expected.dialogue_act: {expected.get('dialogue_act')!r}")

    message = case.get("message")
    if isinstance(message, Mapping):
        if not str(message.get("content") or "").strip():
            problems.append("message.content must not be empty")
    elif not str(message or "").strip():
        problems.append("message must not be empty")

    history = case.get("history", [])
    if history and not isinstance(history, list):
        problems.append("history must be a list")

    return problems


def validate_cases(cases: Iterable[Mapping[str, Any]]) -> list[str]:
    """Validate the whole set, including unique ids."""
    problems: list[str] = []
    seen: set[str] = set()
    for index, case in enumerate(cases):
        case_id = str(case.get("id") or f"case[{index}]")
        if case_id in seen:
            problems.append(f"duplicate id: {case_id}")
        seen.add(case_id)
        for problem in validate_case(case):
            problems.append(f"{case_id}: {problem}")
    return problems


def summarize(cases: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Small summary useful in test output and refactor dashboards."""
    actions: dict[str, int] = {}
    chat_types: dict[str, int] = {}
    scenarios: dict[str, int] = {}
    total = 0
    for case in cases:
        total += 1
        expected = case.get("expected") or {}
        action = str(expected.get("action") or "unknown")
        actions[action] = actions.get(action, 0) + 1
        chat_type = str(case.get("chat_type") or "unknown")
        chat_types[chat_type] = chat_types.get(chat_type, 0) + 1
        scenario = str(case.get("scenario") or "unknown")
        scenarios[scenario] = scenarios.get(scenario, 0) + 1
    return {
        "total": total,
        "actions": actions,
        "chat_types": chat_types,
        "scenarios": scenarios,
    }
