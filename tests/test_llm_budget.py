"""LLM token budget checks (local testing safety).

Run directly: python tests/test_llm_budget.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from llm.client import LLMClient


def _stub(**kwargs):
    client = object.__new__(LLMClient)
    client._budget_used = kwargs.get("used", 0)
    client._daily_budget = kwargs.get("daily", 0)
    client._per_agent_budget = kwargs.get("per_agent", 0)
    client._agent_usage = kwargs.get("agent_usage", {})
    return client


def main() -> bool:
    print("=" * 50)
    print("LLM budget checks")
    print("=" * 50)

    unlimited = _stub(
        used=10**12, daily=0, per_agent=0,
        agent_usage={"conversational": 10**12},
    )
    ok = unlimited._budget_exceeded("conversational") is False

    negative = _stub(
        used=10**12, daily=-1, per_agent=-1,
        agent_usage={"conversational": 10**12},
    )
    ok &= negative._budget_exceeded("conversational") is False

    daily_hit = _stub(used=100, daily=100, per_agent=0)
    ok &= daily_hit._budget_exceeded("conversational") is True

    agent_hit = _stub(
        used=10, daily=1000, per_agent=100,
        agent_usage={"conversational": 100},
    )
    ok &= agent_hit._budget_exceeded("conversational") is True

    agent_other = _stub(
        used=10, daily=1000, per_agent=100,
        agent_usage={"memory": 100},
    )
    ok &= agent_other._budget_exceeded("conversational") is False

    print(f"  unlimited_exceeded={unlimited._budget_exceeded('conversational')}")
    print("  ok" if ok else "  FAIL")
    return bool(ok)


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
