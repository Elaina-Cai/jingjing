# -*- coding: utf-8 -*-
"""人话重写档 v0（确定性减法，shadow 先行）检查：standalone + pytest 兼容。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.speech.human_tone import rewrite_v0  # noqa: E402

DASH = chr(0x2014)


def test_dash_to_comma():
    out, rules = rewrite_v0("跑崩了还能重跑" + DASH * 2 + "数据不对就得回头查参数")
    assert DASH not in out and "，" in out
    assert "dash" in rules


def test_opening_filler_removed():
    out, rules = rewrite_v0("其实，我觉得可以。")
    assert out == "我觉得可以。"
    assert "filler" in rules


def test_long_tail_clause_dropped():
    text = "这个事儿我先记着，等他们那边有动静我再喊你，其实我这边也不好催太紧"
    out, rules = rewrite_v0(text)
    assert out.endswith("我再喊你")
    assert "tail" in rules


def test_long_split_when_no_tail_marker():
    text = "先把这一版完整跑起来看看结果再说，剩下的事情等这一轮跑完我们再一起慢慢商量"
    out, rules = rewrite_v0(text)
    assert "\n" in out
    assert "split" in rules


def test_short_text_untouched():
    out, rules = rewrite_v0("好。")
    assert out == "好。" and rules == ()




TESTS = [test_dash_to_comma, test_opening_filler_removed, test_long_tail_clause_dropped,
         test_long_split_when_no_tail_marker, test_short_text_untouched]


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
        print(f"human-rewrite checks: {len(TESTS)}/{len(TESTS)} passed")
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
