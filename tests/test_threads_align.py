# -*- coding: utf-8 -*-
"""语义对齐拆分（anchor_changed / takeover_allowed / about）本地检查。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.context.threads import FocusInput, calculate_focus  # noqa: E402


def _row(speaker, text, at, **kw):
    row = {"speaker": speaker, "sender_name": speaker, "content": text, "created_at": at}
    row.update(kw)
    return row


def test_about_defaults_to_speaker():
    result = calculate_focus(
        FocusInput(chat_id="c1", current=_row("P3", "哇呜", "2026-10-01 10:00:00"))
    )
    assert result.anchor is None
    assert result.about_is_speaker is True
    assert result.about == "P3"


def test_quote_takeover_allowed():
    current = _row("P3", "[引用 P1: 我们在讨论实验] 我这边接一下", "2026-10-01 10:00:10")
    result = calculate_focus(FocusInput(chat_id="c2", current=current))
    assert result.takeover_allowed is True
    assert any("引用" in item for item in result.takeover_evidence)


def test_anchor_changed_matches_switched():
    result = calculate_focus(
        FocusInput(chat_id="c3", current=_row("P2", "在吗", "2026-10-01 10:00:05"))
    )
    assert result.anchor_changed == result.switched


TESTS = [
    test_about_defaults_to_speaker,
    test_quote_takeover_allowed,
    test_anchor_changed_matches_switched,
]


def main() -> bool:
    ok = True
    for test in TESTS:
        try:
            test()
        except AssertionError as exc:
            print(f"FAIL {test.__name__}: {exc}")
            ok = False
        else:
            print(f"PASS {test.__name__}")
    if ok:
        print(f"threads-align checks: {len(TESTS)}/{len(TESTS)} passed")
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
