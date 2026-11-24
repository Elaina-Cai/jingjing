"""WP-08 8.2 typed decision 读口单测（步骤 2）。

Run directly:
    python tests/test_typed_decision_reads.py
"""
from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agents.base import AgentContext  # noqa: E402
from agents.conversational import _decision_field  # noqa: E402
from agents.planner import content_plan_hint  # noqa: E402


@contextmanager
def _env(**values):
    saved = {key: os.environ.get(key) for key in values}
    for key, value in values.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_decision_value_without_carrier() -> bool:
    ctx = AgentContext(extra={"chat_id": "c1"})
    assert ctx.decision_value("action", "fallback") == "fallback"
    ctx.turn_decision = SimpleNamespace(action="reply_now")
    assert ctx.decision_value("action", "fallback") == "reply_now"
    assert ctx.decision_value("missing", "fallback") == "fallback"
    return True


def test_decision_field_reads_only_typed() -> bool:
    decision = SimpleNamespace(active_intent="decision-value", active_unknown_answer=True)

    ctx = AgentContext(extra={})
    ctx.turn_decision = decision
    assert _decision_field(ctx, "active_intent", "") == "decision-value"
    assert _decision_field(ctx, "active_unknown_answer", False) is True

    # 旧 extra 投影已退役：有没有 extra 都不再被当结论读
    legacy = SimpleNamespace(extra={"turn_intent": "extra-only", "unknown_answer": False})
    assert _decision_field(legacy, "active_intent", "") == ""
    assert _decision_field(legacy, "active_unknown_answer", False) is False

    # 没有 decision 的入口 -> default
    assert _decision_field(SimpleNamespace(extra={}), "active_unknown_answer", False) is False
    return True

def test_defer_sticker_reads_typed_then_extra() -> bool:
    from agents.tools.send_sticker import _should_defer_sticker

    typed = SimpleNamespace(
        turn_decision=SimpleNamespace(defer_sticker_until_after_reply=True),
        extra={"defer_sticker_until_after_reply": False},
    )
    typed.decision_value = lambda name, default=None: getattr(
        typed.turn_decision, name, default,
    )
    assert _should_defer_sticker(typed) is True, "typed 必须优先"
    duck = SimpleNamespace(extra={"defer_sticker_until_after_reply": True})
    assert _should_defer_sticker(duck) is True, "无 carrier 回落 extra"
    assert _should_defer_sticker(SimpleNamespace(extra={})) is False
    assert _should_defer_sticker(None) is False
    return True


def test_take_policy_decision_consumes_once() -> bool:
    legacy = SimpleNamespace(extra={"_policy_decision": "legacy"}, policy_decision=None)
    assert AgentContext.take_policy_decision(legacy) == "legacy"
    assert AgentContext.take_policy_decision(legacy) is None

    typed = SimpleNamespace(extra={"_policy_decision": "legacy"}, policy_decision="typed")
    assert AgentContext.take_policy_decision(typed) == "typed"
    assert typed.policy_decision is None
    assert typed.extra.get("_policy_decision") is None
    assert AgentContext.take_policy_decision(typed) is None
    return True


def test_content_plan_hint_typed_matches_projection() -> bool:
    payload = {
        "steps": [{"id": 1, "description": "查天气"}],
        "replans": 0,
        "over_budget": False,
        "report": "",
        "asked_user": False,
    }

    class _Plan:
        def to_dict(self):
            return dict(payload)

    with _env(YINYUE_FEATURE_PLAN_LOOP="on"):
        projected = content_plan_hint({"content_plan": dict(payload)})
        typed = content_plan_hint({}, _Plan())
    assert projected != "", projected
    assert projected == typed, (projected, typed)
    return True


def main() -> bool:
    tests = [
        test_decision_value_without_carrier,
        test_decision_field_reads_only_typed,
        test_defer_sticker_reads_typed_then_extra,
        test_take_policy_decision_consumes_once,
        test_content_plan_hint_typed_matches_projection,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  ok   {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {fn.__name__}: {exc!r}")
    print(f"\ntyped decision reads: {len(tests) - failed}/{len(tests)} passed")
    return failed == 0


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
