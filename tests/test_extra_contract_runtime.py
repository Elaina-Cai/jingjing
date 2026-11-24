"""WP-08 8.2：AgentContext.extra 运行期契约守卫。

Run directly:
    python tests/test_extra_contract_runtime.py
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agents.base import (  # noqa: E402
    AgentContext,
    ExtraContractViolation,
    ExtraDict,
    _EXTRA_CONTRACT_VIOLATIONS,
)

MODE = "YINYUE_EXTRA_CONTRACT_MODE"


@contextmanager
def _mode(value: str | None):
    saved = os.environ.get(MODE)
    try:
        if value is None:
            os.environ.pop(MODE, None)
        else:
            os.environ[MODE] = value
        _EXTRA_CONTRACT_VIOLATIONS.clear()
        yield
    finally:
        if saved is None:
            os.environ.pop(MODE, None)
        else:
            os.environ[MODE] = saved


def test_known_key_never_violates() -> bool:
    with _mode(None):
        d = ExtraDict({"chat_id": "c1", "is_group": False})
        d["sender_name"] = "Elaina"
        d.update({"platform": "wechat"})
        d.setdefault("session_id", 1)
        assert not _EXTRA_CONTRACT_VIOLATIONS, _EXTRA_CONTRACT_VIOLATIONS
    return True


def test_warn_mode_records_unknown_keys() -> bool:
    with _mode("warn"):
        d = ExtraDict({"chat_id": "c1"})
        d["rogue_key"] = 1
        d.update({"rogue_two": 2})
        d.setdefault("rogue_three", 3)
    assert _EXTRA_CONTRACT_VIOLATIONS == {"rogue_key", "rogue_two", "rogue_three"}, (
        _EXTRA_CONTRACT_VIOLATIONS
    )
    return True


def test_strict_mode_raises_and_known_still_works() -> bool:
    with _mode("strict"):
        d = ExtraDict({"chat_id": "c1"})
        d["is_group"] = False
        try:
            d["rogue_key"] = 1
        except ExtraContractViolation:
            pass
        else:
            raise AssertionError("strict 模式没有拦截未知键")
    assert "rogue_key" in _EXTRA_CONTRACT_VIOLATIONS
    return True


def test_default_mode_blocks_unknown() -> bool:
    with _mode(None):
        d = ExtraDict({"chat_id": "c1"})
        try:
            d["rogue_default"] = 1
        except ExtraContractViolation:
            pass
        else:
            raise AssertionError("默认模式必须拦截未知键")
    assert "rogue_default" in _EXTRA_CONTRACT_VIOLATIONS
    return True


def test_invalid_mode_falls_back_to_strict() -> bool:
    with _mode("nonsense"):
        d = ExtraDict({"chat_id": "c1"})
        try:
            d["rogue_invalid"] = 1
        except ExtraContractViolation:
            pass
        else:
            raise AssertionError("非法 mode 必须 fail-safe 到 strict")
    assert "rogue_invalid" in _EXTRA_CONTRACT_VIOLATIONS
    return True


def test_off_mode_ignores_unknown() -> bool:
    with _mode("off"):
        d = ExtraDict({"chat_id": "c1"})
        d["rogue_key"] = 1
    assert not _EXTRA_CONTRACT_VIOLATIONS
    return True


def test_agent_context_wraps_extra_and_assignment() -> bool:
    with _mode("warn"):
        ctx = AgentContext(extra={"chat_id": "c1"})
        assert isinstance(ctx.extra, ExtraDict), type(ctx.extra)
        ctx.extra["is_group"] = False
        assert not _EXTRA_CONTRACT_VIOLATIONS

        ctx.extra = {"sender_name": "Elaina"}
        assert isinstance(ctx.extra, ExtraDict), type(ctx.extra)
        ctx.extra["rogue_ctx"] = 1
    assert "rogue_ctx" in _EXTRA_CONTRACT_VIOLATIONS
    return True


def test_setdefault_existing_unknown_does_not_warn_again() -> bool:
    with _mode("off"):
        d = ExtraDict({"chat_id": "c1"})
        d["rogue_existing"] = 1
    with _mode("warn"):
        assert d.setdefault("rogue_existing", 2) == 1
    assert not _EXTRA_CONTRACT_VIOLATIONS, _EXTRA_CONTRACT_VIOLATIONS
    return True


def test_ior_checks_unknown_key() -> bool:
    with _mode("warn"):
        d = ExtraDict({"chat_id": "c1"})
        d |= {"rogue_ior": 1}
    assert _EXTRA_CONTRACT_VIOLATIONS == {"rogue_ior"}, _EXTRA_CONTRACT_VIOLATIONS
    return True


def main() -> bool:
    tests = [
        test_known_key_never_violates,
        test_warn_mode_records_unknown_keys,
        test_strict_mode_raises_and_known_still_works,
        test_default_mode_blocks_unknown,
        test_invalid_mode_falls_back_to_strict,
        test_off_mode_ignores_unknown,
        test_setdefault_existing_unknown_does_not_warn_again,
        test_ior_checks_unknown_key,
        test_agent_context_wraps_extra_and_assignment,
    ]
    failed: list[str] = []
    for test in tests:
        try:
            test()
            print(f"  ok   {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed.append(test.__name__)
            print(f"  FAIL {test.__name__}: {exc!r}")
    print(f"\nextra contract runtime: {len(tests) - len(failed)}/{len(tests)} passed")
    return not failed


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
