"""Phase 5: correction confirmation rule reaches the persona prompt.

Run directly:
    python tests/test_correction_confirm.py
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.terminal.persona_brain import build_persona_system_prompt  # noqa: E402


def _snapshot():
    persona = SimpleNamespace(
        name="鲸鲸",
        identity={"self": "你是鲸鲸"},
        traits={"语速": "轻快"},
        voice_config="",
    )
    policy = SimpleNamespace(prompt_brief="")
    turn = SimpleNamespace()
    return SimpleNamespace(persona=persona, policy=policy, turn=turn)


def test_rule_is_injected_and_absent_by_default() -> None:
    rule = "[纠错确认] 先用一句话确认更正后的说法。"
    plain = build_persona_system_prompt(_snapshot())
    assert "纠错确认" not in plain, plain[:200]
    with_rule = build_persona_system_prompt(_snapshot(), correction_rule=rule)
    assert "纠错确认" in with_rule, with_rule[:400]
    print("  ok   correction rule only appears when passed")


def test_rule_keeps_her_from_defending() -> None:
    with_rule = build_persona_system_prompt(
        _snapshot(), correction_rule="[纠错确认] 不要辩解、不要重复旧说法。"
    )
    assert "不要辩解" in with_rule and "不要重复旧说法" in with_rule
    print("  ok   rule tells her to confirm instead of defending")


def main() -> bool:
    tests = [test_rule_is_injected_and_absent_by_default, test_rule_keeps_her_from_defending]
    failed: list[str] = []
    for test in tests:
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            failed.append(test.__name__)
            print(f"  FAIL {test.__name__}: {exc!r}")
    print(f"correction confirm checks: {len(tests) - len(failed)}/{len(tests)} passed")
    return not failed


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
