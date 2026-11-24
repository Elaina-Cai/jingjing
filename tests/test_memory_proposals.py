"""Memory proposals (P2): slot validation, dedupe and the propose-only contract.

归属：提案槽位白名单/证据校验/去重/拦截（玩笑命令、回声）。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.quality.memory_proposals import (  # noqa: E402
    MAX_PER_SUBJECT,
    REPEAT_MIN_OCCURRENCES,
    SLOTS,
    SLOTS_V2,
    _duplicate,
    _validate_item,
    proposals_v2_enabled,
)

VALID = {"ev1", "ev2", "ev3"}


def _v2(fn):
    """在 v2 开关打开的环境里跑断言。"""
    prev = os.environ.get("YINYUE_FEATURE_MEMORY_PROPOSALS_V2")
    os.environ["YINYUE_FEATURE_MEMORY_PROPOSALS_V2"] = "1"
    try:
        return fn()
    finally:
        if prev is None:
            os.environ.pop("YINYUE_FEATURE_MEMORY_PROPOSALS_V2", None)
        else:
            os.environ["YINYUE_FEATURE_MEMORY_PROPOSALS_V2"] = prev


def test_slots_are_the_five_contract_slots() -> None:
    """v1 的五槽 + 批 3b 上沉新增的 lexicon（术语）。"""
    assert set(SLOTS) == {
        "appointment",
        "open_loop",
        "identity",
        "preference",
        "habit",
        "lexicon",
    }


def test_v2_slots_are_the_two_narrow_ones() -> None:
    """批 3c：v2 只留 open_loop / habit，identity 不再自动提案。"""
    assert set(SLOTS_V2) == {"open_loop", "habit"}
    assert set(SLOTS_V2) <= set(SLOTS)


def test_valid_item_passes() -> None:
    item = {
        "slot": "identity",
        "subject": "P6",
        "content": "P6是物联网25-2班的",
        "evidence": ["ev1"],
        "reason": "repeat",
    }
    checked = _validate_item(item, VALID)
    assert checked is not None
    assert checked["slot"] == "identity"
    assert checked["evidence"] == ["ev1"]
    assert checked["reason"] == "repeat"


def test_unknown_slot_is_rejected() -> None:
    assert _validate_item({"slot": "gossip", "content": "abc", "evidence": ["ev1"]}, VALID) is None


def test_evidence_must_come_from_the_record() -> None:
    item = {"slot": "habit", "content": "Y-K 爱起哄", "evidence": ["ev999"]}
    assert _validate_item(item, VALID) is None  # 编造证据 → 拒


def test_short_or_empty_content_is_rejected() -> None:
    assert _validate_item({"slot": "habit", "content": "啊", "evidence": ["ev1"]}, VALID) is None
    assert _validate_item({"slot": "habit", "content": "   ", "evidence": ["ev1"]}, VALID) is None


def test_reason_defaults_and_clamps() -> None:
    checked = _validate_item(
        {"slot": "habit", "content": "Y-K 爱起哄", "evidence": ["ev1"], "reason": "瞎写"}, VALID
    )
    assert checked is not None and checked["reason"] == "llm"


def test_long_content_is_truncated() -> None:
    checked = _validate_item(
        {"slot": "preference", "content": "很长" * 100, "evidence": ["ev1"]}, VALID
    )
    assert checked is not None and checked["content"].endswith("…")


def test_duplicate_detection() -> None:
    existing = {"P6是物联网25-2班的"}
    assert _duplicate("P6是物联网25-2班的", existing) is True
    assert _duplicate("P6是物联网25-2班的女生", existing) is True  # containment
    assert _duplicate("Y-K 爱起哄", existing) is False


def test_habit_with_joke_instruction_is_rejected() -> None:
    """主人点名：玩笑里的"要求"不能沉淀成习惯（她会拿它当规则拒绝配合）。"""
    for content in (
        "Y-K 常自封牛子榜第一，并反复强调不准再改榜单",
        "他要求必须叫他第一",
        "群里约定禁止提某话题",
    ):
        assert _validate_item(
            {"slot": "habit", "content": content, "evidence": ["ev1"]}, VALID
        ) is None, content
    # 正常的脾气描述不受影响
    assert _validate_item(
        {"slot": "habit", "content": "Y-K 爱起哄，爱自封第一", "evidence": ["ev1"]}, VALID
    ) is not None


def test_echo_habit_about_the_bot_is_rejected() -> None:
    """说她自己的"习惯"是回声，不是对人的认识（主人点名驳回 #4）。"""
    for subject in ("鲸鲸", "她", "自己"):
        assert _validate_item(
            {"slot": "habit", "subject": subject, "content": "爱给榜单改版编号", "evidence": ["ev1"]},
            VALID,
        ) is None, subject
    assert _validate_item(
        {"slot": "habit", "subject": "Y-K", "content": "爱起哄，爱自封第一", "evidence": ["ev1"]},
        VALID,
    ) is not None


def test_subject_cap_is_small() -> None:
    assert MAX_PER_SUBJECT <= 5


# ------------------------------------------------------------------ 批 3c v2
def test_v2_only_accepts_two_slots() -> None:
    """v2 只提悬而未决 + 重复模式；身份类被挡在门外（名单已有）。"""
    assert set(SLOTS_V2) == {"open_loop", "habit"}
    v1_identity = {
        "slot": "identity",
        "subject": "P6",
        "content": "P6是物联网25-2班的",
        "evidence": ["ev1"],
        "evidence_text": "P6是物联网25-2班的",
    }
    # v1 放行，v2 拒绝——这就是改版的意义
    assert _validate_item(v1_identity, VALID) is not None
    assert _v2(lambda: _validate_item(v1_identity, VALID)) is None

    for slot in ("appointment", "preference"):
        item = {"slot": slot, "content": "下周三看成绩单", "evidence": ["ev1"], "evidence_text": "下周三"}
        assert _v2(lambda i=item: _validate_item(i, VALID)) is None, slot


def test_v2_habit_needs_three_occurrences() -> None:
    """「重复模式」必须有 ≥3 次证据；只出现一次的不是模式。"""
    one = {
        "slot": "habit",
        "subject": "Y-K",
        "content": "爱自封牛子榜第一",
        "evidence": ["ev1"],
        "evidence_text": "我是牛子榜第一",
    }
    three = dict(one, evidence=["ev1", "ev2", "ev3"])
    assert _v2(lambda: _validate_item(one, VALID)) is None
    assert _v2(lambda: _validate_item(three, VALID)) is not None
    assert REPEAT_MIN_OCCURRENCES == 3


def test_v2_requires_evidence_text() -> None:
    """每条必须带原话证据，否则不可审 → 丢弃。"""
    item = {
        "slot": "open_loop",
        "subject": "P8",
        "content": "P8 承诺转 token 尚未兑现",
        "evidence": ["ev1"],
    }
    assert _v2(lambda: _validate_item(item, VALID)) is None
    assert _v2(lambda: _validate_item(dict(item, evidence_text="给你转1亿token"), VALID)) is not None


def test_v2_keeps_the_joke_instruction_guard() -> None:
    """v2 不是放松，是把已有的两道护栏继续保留。"""
    joke = {
        "slot": "habit",
        "subject": "Y-K",
        "content": "反复强调不准再改榜单",
        "evidence": ["ev1", "ev2", "ev3"],
        "evidence_text": "不准再改榜单",
    }
    assert _v2(lambda: _validate_item(joke, VALID)) is None
    echo = {
        "slot": "habit",
        "subject": "鲸鲸",
        "content": "爱给榜单改版编号",
        "evidence": ["ev1", "ev2", "ev3"],
        "evidence_text": "又改版了",
    }
    assert _v2(lambda: _validate_item(echo, VALID)) is None


def test_v2_flag_defaults_off() -> None:
    """默认关 = 旧五槽位行为（可回滚）。"""
    os.environ.pop("YINYUE_FEATURE_MEMORY_PROPOSALS_V2", None)
    assert proposals_v2_enabled() is False
    os.environ["YINYUE_FEATURE_MEMORY_PROPOSALS_V2"] = "1"
    assert proposals_v2_enabled() is True
    os.environ.pop("YINYUE_FEATURE_MEMORY_PROPOSALS_V2", None)


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
