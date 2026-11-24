"""Resolver runtime override checks (P3).

Run directly: python tests/test_resolver_config.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.conversation import config as conversation_config
from core.conversation.resolver import AddresseeResolver

_ROOT = Path(__file__).resolve().parent.parent
_TUNING_FILE = _ROOT / "data" / "_test_resolver_tuning.json"

_ENV_KEYS = (
    conversation_config.WEIGHTS_ENV,
    conversation_config.TUNING_FILE_ENV,
    conversation_config.REPLY_THRESHOLD_ENV,
    conversation_config.OPEN_REPLY_THRESHOLD_ENV,
    conversation_config.OBSERVE_THRESHOLD_ENV,
)


def _clear_env() -> dict[str, str | None]:
    old = {key: os.environ.pop(key, None) for key in _ENV_KEYS}
    conversation_config.clear_cache()
    return old


def _restore_env(old: dict[str, str | None]) -> None:
    for key, value in old.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    conversation_config.clear_cache()


def test_defaults_and_env() -> bool:
    print("\n[resolver override precedence]")
    old = _clear_env()
    try:
        base = AddresseeResolver()
        ok = base.reply_threshold == 0.75
        ok &= base.open_reply_threshold == 0.55
        ok &= base.observe_threshold == 0.35
        ok &= base.weights["same_sender"] == 0.20

        os.environ[conversation_config.WEIGHTS_ENV] = json.dumps(
            {"same_sender": 0.5, "unknown_signal": 0.9}
        )
        os.environ[conversation_config.REPLY_THRESHOLD_ENV] = "0.62"
        os.environ[conversation_config.OPEN_REPLY_THRESHOLD_ENV] = "0.48"
        os.environ[conversation_config.OBSERVE_THRESHOLD_ENV] = "0.30"

        tuned = AddresseeResolver()
        ok &= tuned.weights["same_sender"] == 0.5
        ok &= "unknown_signal" not in tuned.weights
        ok &= tuned.reply_threshold == 0.62
        ok &= tuned.open_reply_threshold == 0.48
        ok &= tuned.observe_threshold == 0.30
        ok &= tuned.config_snapshot()["reply_threshold"] == 0.62

        # Explicit constructor arguments still win over environment tuning.
        explicit = AddresseeResolver(
            weights={"same_sender": 0.9}, reply_threshold=0.8,
        )
        ok &= explicit.weights["same_sender"] == 0.9
        ok &= explicit.reply_threshold == 0.8

        # Candidate search must be able to ignore production overrides.
        raw = AddresseeResolver(apply_overrides=False)
        ok &= raw.weights["same_sender"] == 0.20
        ok &= raw.reply_threshold == 0.75

        print(f"  tuned weights same_sender={tuned.weights['same_sender']}")
        print("  ok" if ok else "  FAIL")
        return bool(ok)
    finally:
        _restore_env(old)


def test_tuning_file() -> bool:
    print("\n[resolver tuning file]")
    _TUNING_FILE.unlink(missing_ok=True)
    old = _clear_env()
    try:
        _TUNING_FILE.write_text(json.dumps({
            "suggested_weights": {"same_sender": 0.35, "media": -0.05},
            "suggested_thresholds": {
                "reply_threshold": 0.70,
                "open_reply_threshold": 0.50,
            },
        }, ensure_ascii=False), encoding="utf-8")
        os.environ[conversation_config.TUNING_FILE_ENV] = str(_TUNING_FILE)
        resolver = AddresseeResolver()
        ok = resolver.weights["same_sender"] == 0.35
        ok &= resolver.weights["media"] == -0.05
        ok &= resolver.reply_threshold == 0.70
        ok &= resolver.open_reply_threshold == 0.50

        # Malformed values are ignored instead of breaking routing.
        _TUNING_FILE.write_text("{not json", encoding="utf-8")
        conversation_config.clear_cache()
        broken = AddresseeResolver()
        ok &= broken.weights["same_sender"] == 0.20

        print(f"  file thresholds={resolver.reply_threshold}/"
              f"{resolver.open_reply_threshold}")
        print("  ok" if ok else "  FAIL")
        return bool(ok)
    finally:
        _TUNING_FILE.unlink(missing_ok=True)
        _restore_env(old)


def main() -> bool:
    print("=" * 50)
    print("resolver config checks")
    print("=" * 50)
    ok = test_defaults_and_env()
    ok &= test_tuning_file()
    print("\n" + ("all passed" if ok else "FAILED"))
    return bool(ok)


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
