# -*- coding: utf-8 -*-
"""人化减法层（HUMAN_TONE on 档）检查。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.speech import human_tone as ht  # noqa: E402


def test_opening_filler_stripped():
    assert ht.humanize("好的，我帮你看看这个") == "我帮你看看这个"
    assert ht.humanize("当然，可以。") == "可以"
    assert ht.humanize("没问题，马上来") == "马上来"


def test_short_text_untouched():
    assert ht.humanize("好的") == "好的"
    assert ht.humanize("行") == "行"
    assert ht.humanize("哈？") == "哈？"


def test_smile_marker_stripped():
    assert ht.humanize("行吧（笑）就这样") == "行吧就这样"
    assert ht.humanize("收到(笑)") == "收到"  # 8 字内不动


def test_single_trailing_period_stripped():
    assert ht.humanize("那你先忙。") == "那你先忙"
    assert ht.humanize("第一。第二。") == "第一。第二。"


def test_untouched_when_no_marker():
    original = "锅边那碗我认，谁端我急"
    assert ht.humanize(original) == original


def test_empty_or_punct_falls_back():
    assert ht.humanize("。。。。。") == "。。。。。"




TESTS = [
    test_opening_filler_stripped,
    test_short_text_untouched,
    test_smile_marker_stripped,
    test_single_trailing_period_stripped,
    test_untouched_when_no_marker,
    test_empty_or_punct_falls_back,
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
        print(f"human-tone checks: {len(TESTS)}/{len(TESTS)} passed")
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
