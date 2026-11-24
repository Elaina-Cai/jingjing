"""短句种子 loader / 选择器单测（只读，不接运行时）。

Run directly:
    python tests/test_short_reply_selector.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.speech.short_reply_seed import (  # noqa: E402
    MVP_SIGNALS,
    SIGNAL_TO_KEY,
    ShortReplyKey,
    classify_short_reply,
    count_chars,
    derive_signal,
    load_short_reply_seed,
    select_lines,
)


def test_loader_reads_seed():
    seed = load_short_reply_seed()
    assert seed.key == "yinyue_short_reply_seed"
    assert seed.version
    assert len(seed.states) == 6
    assert seed.total_lines() == 111
    assert len(seed.keys()) == 35
    assert seed.has("平常", "被夸")
    assert seed.lines("平常", "被夸")[0] == "少来"
    assert seed.lines("特殊", "打错字_对方打错") == ("噢", "噢噢")
    return True


def test_loader_missing_or_broken_is_empty():
    missing = load_short_reply_seed("no_such_file_short_reply_seed.json")
    assert missing.states == {}
    assert missing.total_lines() == 0
    broken = load_short_reply_seed(__file__)
    assert broken.states == {}
    assert broken.total_lines() == 0
    return True


def test_count_chars_matches_seed_rules():
    assert count_chars("查到了，等我发你。") == 8
    assert count_chars("记上了，明早九点。") == 8
    assert count_chars("这事我不接，你们定。") == 9
    assert count_chars("嘿嘿，没有") == 4
    assert count_chars("……嗯") == 2
    assert count_chars("嗯？") == 2
    assert count_chars("？！") == 2
    assert count_chars("OK") == 2
    return True


def test_signal_mapping_points_to_existing_scenes():
    seed = load_short_reply_seed()
    assert SIGNAL_TO_KEY, "映射表不能为空"
    for signal, key in SIGNAL_TO_KEY.items():
        assert seed.has(key.state, key.scene), (signal, key)
    assert MVP_SIGNALS <= set(SIGNAL_TO_KEY), MVP_SIGNALS - set(SIGNAL_TO_KEY)
    return True


def test_classify_scope():
    key = ShortReplyKey("平常", "被夸")
    assert classify_short_reply("praised", is_owner=True) == key
    assert classify_short_reply("praised", is_owner=False) == key
    assert classify_short_reply("praised", is_group=True, is_owner=True) == key
    assert classify_short_reply("praised", is_group=True, is_owner=False) == key
    assert classify_short_reply("", is_owner=True) is None
    assert classify_short_reply("no_such_signal", is_owner=True) is None
    return True


def test_mvp_is_conservative():
    assert classify_short_reply("pestered", is_owner=True, mvp_only=True) is None
    assert classify_short_reply("pestered", is_owner=True, mvp_only=False) == ShortReplyKey("平常", "被麻烦")
    assert "pestered" not in MVP_SIGNALS
    return True


def test_mvp_high_confidence_mappings():
    cases = {
        "called_name": ShortReplyKey("平常", "叫名字"),
        "presence_check": ShortReplyKey("平常", "在吗"),
        "praised": ShortReplyKey("平常", "被夸"),
        "thanked": ShortReplyKey("平常", "被谢"),
        "asked_favor": ShortReplyKey("平常", "求她办事"),
        "caught_waiting": ShortReplyKey("别扭", "被戳穿_你在等我吧"),
        "caught_caring": ShortReplyKey("别扭", "被戳穿_你是不是在意"),
        "low_battery_called": ShortReplyKey("低电量", "被叫"),
        "low_battery_presence_check": ShortReplyKey("低电量", "被问在吗"),
        "serious_result": ShortReplyKey("正事", "查到了"),
        "reminder_recorded": ShortReplyKey("正事", "记提醒"),
        "task_done": ShortReplyKey("正事", "办成了"),
        "typo_other": ShortReplyKey("特殊", "打错字_对方打错"),
        "typo_self": ShortReplyKey("特殊", "打错字_自己手滑"),
    }
    for signal, key in cases.items():
        assert classify_short_reply(signal, is_owner=True) == key, signal
    return True


def test_select_lines():
    seed = load_short_reply_seed()
    assert select_lines(seed, "praised", is_owner=True, limit=2) == ("少来", "哼")
    assert select_lines(seed, "praised", is_owner=True, limit=3)[0] == "少来"
    assert select_lines(seed, "praised", is_owner=True, limit=100) == seed.lines("平常", "被夸")
    assert select_lines(seed, "praised", is_owner=True, limit=0) == ()
    assert select_lines(seed, "praised", is_owner=False, limit=2) == ("少来", "哼")
    assert select_lines(seed, "no_such_signal", is_owner=True) == ()
    assert select_lines(seed, "pestered", is_owner=True, mvp_only=True) == ()
    assert select_lines(seed, "pestered", is_owner=True, mvp_only=False, limit=1) == ("又是我？",)
    return True


def test_selected_lines_keep_forbidden_state_clean():
    seed = load_short_reply_seed()
    for signal in ("caught_waiting", "caught_caring"):
        lines = select_lines(seed, signal, is_owner=True, limit=99)
        assert lines
        assert all("嘿嘿" not in line for line in lines), (signal, lines)
    return True


def test_derive_signal_text_fallbacks():
    checks = [
        ("presence", derive_signal(inbound="在吗", is_owner=True), "presence_check"),
        ("presence+name", derive_signal(inbound="鲸鲸，在不在？", is_owner=True), "presence_check"),
        ("called", derive_signal(inbound="嘿，鲸鲸", is_owner=True), "called_name"),
        ("called-hi", derive_signal(inbound="hi，鲸鲸", is_owner=True), "called_name"),
        ("thanks", derive_signal(inbound="谢谢鲸鲸", is_owner=True), "thanked"),
        ("favor", derive_signal(inbound="帮我查一下明天天气", is_owner=True), "asked_favor"),
        ("waiting", derive_signal(inbound="你是不是在等我", is_owner=True), "caught_waiting"),
        ("caring", derive_signal(inbound="你是不是在意我", is_owner=True), "caught_caring"),
        ("typo-other", derive_signal(inbound="我刚刚打错字了", is_owner=True), "typo_other"),
        ("typo-self", derive_signal(inbound="你打错字了", is_owner=True), "typo_self"),
        ("praised", derive_signal(inbound="你真厉害", is_owner=True), "praised"),
    ]
    for name, got, want in checks:
        assert got == want, (name, got, want)
    return True


def test_derive_signal_scope_and_explicit():
    assert derive_signal(inbound="在吗", is_owner=False) == "presence_check"
    assert derive_signal(inbound="在吗", is_group=True, is_owner=True) == "presence_check"
    assert derive_signal(inbound="在吗", is_group=True, is_owner=False) == "presence_check"
    assert derive_signal(inbound="随便", is_owner=True, explicit="praised") == "praised"
    assert derive_signal(inbound="随便", is_owner=True, explicit="pestered") == ""
    assert derive_signal(inbound="", is_owner=True, trigger_reason="serious_result") == "serious_result"
    assert derive_signal(inbound="", is_owner=True, explicit="no_such") == ""
    return True


def main() -> bool:
    tests = [
        test_loader_reads_seed,
        test_loader_missing_or_broken_is_empty,
        test_count_chars_matches_seed_rules,
        test_signal_mapping_points_to_existing_scenes,
        test_classify_scope,
        test_mvp_is_conservative,
        test_mvp_high_confidence_mappings,
        test_select_lines,
        test_selected_lines_keep_forbidden_state_clean,
        test_derive_signal_text_fallbacks,
        test_derive_signal_scope_and_explicit,
    ]
    print("=" * 50)
    print("短句种子 loader / 选择器测试")
    print("=" * 50)
    failed: list[str] = []
    for test in tests:
        try:
            test()
            print(f"  ok   {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed.append(test.__name__)
            print(f"  FAIL {test.__name__}: {exc!r}")
    print(f"\nshort-reply selector checks: {len(tests) - len(failed)}/{len(tests)} passed")
    return not failed


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
