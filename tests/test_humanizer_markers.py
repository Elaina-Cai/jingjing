# -*- coding: utf-8 -*-
"""humanizer 映射并入检查（2026-10-15）。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.speech import human_tone as ht  # noqa: E402


def test_humanizer_markers_hit():
    text = "归根结底，这是一个至关重要的里程碑，赋能了全方位的一站式体验。"
    flags = ht.tone_flags(text)
    assert "归根结底" in flags and "至关重要" in flags
    assert "赋能" in flags and "全方位" in flags


def test_dash_stack_flagged():
    assert "破折号堆叠" in ht.tone_flags("行吧——也不是不行——但是——你先说到底怎么回事")


def test_single_dash_not_flagged():
    assert "破折号堆叠" not in ht.tone_flags("行——也不是不行")


def test_her_style_short_line_clean():
    for text in ("哈？", "行。", "锅边那碗我认，谁端我急。", "没漏。刚才那条就是答他的。"):
        flags = ht.tone_flags(text)
        assert not flags, (text, flags)


def test_humanize_new_fillers():
    assert ht.humanize("说白了，这题我不会。") == "这题我不会"
    assert ht.humanize("讲真，我不知道。") == "我不知道"
    assert ht.humanize("让我先说明一下，这事不赖我。") == "这事不赖我"


TESTS = [
    test_humanizer_markers_hit,
    test_dash_stack_flagged,
    test_single_dash_not_flagged,
    test_her_style_short_line_clean,
    test_humanize_new_fillers,
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
        print(f"humanizer-marker checks: {len(TESTS)}/{len(TESTS)} passed")
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
