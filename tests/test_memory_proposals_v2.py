"""批 3c 提案改版测试：v2 槽位收敛 + habit 重复门槛 + 证据原文 + 术语分行。

Run directly:
    python tests/test_memory_proposals_v2.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.quality.memory_proposals import (  # noqa: E402
    _lexicon_line,
    _merge_lexicon,
    _validate_item,
    proposals_v2_enabled,
)

FLAG = "YINYUE_FEATURE_MEMORY_PROPOSALS_V2"
IDS = {"ev1", "ev2", "ev3"}


def _item(slot: str, *, evidence=None, text: str = "原话片段", content: str = "一句能读懂的话") -> dict:
    return {
        "slot": slot,
        "subject": "P5",
        "content": content,
        "evidence": evidence if evidence is not None else ["ev1"],
        "reason": "llm",
        "evidence_text": text,
    }


def test_flag_defaults_off() -> None:
    os.environ.pop(FLAG, None)
    assert proposals_v2_enabled() is False
    os.environ[FLAG] = "1"
    assert proposals_v2_enabled() is True
    os.environ.pop(FLAG, None)


def test_v2_only_open_loop_and_habit() -> None:
    os.environ[FLAG] = "1"
    try:
        for slot in ("identity", "preference", "appointment"):
            assert _validate_item(_item(slot), IDS) is None, slot
        ok = _validate_item(_item("open_loop"), IDS)
        assert ok is not None and ok["slot"] == "open_loop"
        assert ok["evidence_text"] == "原话片段"
    finally:
        os.environ.pop(FLAG, None)


def test_v2_habit_needs_three_occurrences() -> None:
    os.environ[FLAG] = "1"
    try:
        two = _item("habit", evidence=["ev1", "ev2"])
        assert _validate_item(two, IDS) is None, "habit 证据 <3 必须拒绝"
        three = _item("habit", evidence=["ev1", "ev2", "ev3"])
        ok = _validate_item(three, IDS)
        assert ok is not None and ok["slot"] == "habit"
    finally:
        os.environ.pop(FLAG, None)


def test_v2_requires_evidence_text() -> None:
    os.environ[FLAG] = "1"
    try:
        no_text = _item("open_loop", text="")
        assert _validate_item(no_text, IDS) is None, "v2 必须带原话证据"
    finally:
        os.environ.pop(FLAG, None)


def test_v1_behavior_unchanged() -> None:
    os.environ.pop(FLAG, None)
    ok = _validate_item(_item("identity", text=""), IDS)
    assert ok is not None and ok["slot"] == "identity", "v1 仍收身份类"


def test_lexicon_line_format() -> None:
    assert _lexicon_line("群内术语「fkxqs」被反复检索（3 次）", "复数缩写") == "fkxqs=复数缩写"
    assert _lexicon_line("群内术语「fkxqs」被反复检索（3 次）", "") == "fkxqs=待补释义"
    # 提取不到词 → 退化为原样文本（不编词条）
    assert _lexicon_line("没有书名号的内容", "释义") == "释义"


def test_merge_lexicon_separates_from_roster() -> None:
    roster = "作业群成员（唯一准版）：P5=计算机25-4班；Elaina=计算机25-4班"
    merged = _merge_lexicon(roster, "群内术语「fkxqs」被反复检索（3 次）", "复数缩写")
    assert "|" in merged and merged.count("fkxqs=复数缩写") == 1
    assert roster in merged or "P5=" in merged  # 成员清单不被破坏
    # 同名多义（硬条件 A，2026-09-26）：不同释义并存；完全相同的行去重
    again = _merge_lexicon(merged, "群内术语「fkxqs」被反复检索（3 次）", "新的释义")
    assert again.count("fkxqs=") == 2 and "复数缩写" in again and "新的释义" in again
    dup = _merge_lexicon(again, "群内术语「fkxqs」被反复检索（3 次）", "新的释义")
    assert dup.count("fkxqs=") == 2
    # 新术语追加
    more = _merge_lexicon(again, "群内术语「yyds」被反复检索（3 次）", "永远的神")
    assert "yyds=永远的神" in more and more.count("|") >= 2


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {test.__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
