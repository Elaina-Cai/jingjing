"""Phase 5 D2 hardening: repository resolution semantics.

Run directly:
    python tests/test_repo_resolution.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import core.bootstrap as bs  # noqa: E402


def _without_context():
    """Patch the resolver inputs so no AccountContext / default account exists."""
    original_ctx = bs.current_account_context
    original_init = bs.init_singletons
    meta_sentinel = object()
    bs.current_account_context = lambda: None
    bs.init_singletons = lambda **kwargs: {"runtime": None, "repo": meta_sentinel}
    return original_ctx, original_init, meta_sentinel


def test_account_repo_fails_closed() -> None:
    original_ctx, original_init, _ = _without_context()
    try:
        raised = False
        try:
            bs.get_account_repo()
        except RuntimeError as exc:
            raised = "no account context" in str(exc)
        assert raised, "get_account_repo() must raise without an account context"
    finally:
        bs.current_account_context = original_ctx
        bs.init_singletons = original_init
    print("  ok   get_account_repo() fails closed without an account context")


def test_get_repo_falls_back_to_meta_with_warning() -> None:
    original_ctx, original_init, sentinel = _without_context()
    original_flag = bs._FALLBACK_WARNED
    bs._FALLBACK_WARNED = False
    try:
        repo = bs.get_repo()
        assert repo is sentinel, "get_repo() should fall back to the shared meta repo"
        assert bs._FALLBACK_WARNED is True, "fallback must be flagged (logged once)"
    finally:
        bs.current_account_context = original_ctx
        bs.init_singletons = original_init
        bs._FALLBACK_WARNED = original_flag
    print("  ok   get_repo() falls back to meta repo and flags it")


def test_legacy_db_path_returns_empty_when_missing() -> None:
    from pathlib import Path

    import core.bootstrap as bootstrap

    missing = Path(bootstrap.LEGACY_DB_PATH)
    if missing.exists():
        print("  skip legacy db still exists")
        return
    assert bootstrap.get_legacy_db_path() == "", "retired legacy DB must not be advertised"
    print("  ok   get_legacy_db_path() returns '' once the legacy DB is retired")


def main() -> bool:
    tests = [
        test_account_repo_fails_closed,
        test_get_repo_falls_back_to_meta_with_warning,
        test_legacy_db_path_returns_empty_when_missing,
    ]
    failed: list[str] = []
    for test in tests:
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            failed.append(test.__name__)
            print(f"  FAIL {test.__name__}: {exc!r}")
    print(f"repo resolution checks: {len(tests) - len(failed)}/{len(tests)} passed")
    return not failed


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
