# -*- coding: utf-8 -*-
"""噪音闸门检查：只剩「同一人完全相同文本十分钟内不重复发起」这一条。

2026-10-23 删掉的六层启发式（模板前缀 / bigram 近重复 0.8 / 拍人 3 次豁免 /
15 条每分钟速率 / 被标 3 次转静默 / 疲劳预算）曾在这里各有一组用例；现在它们
不是「放宽了」而是「不存在了」，所以本文件同时锁反方向：这些名字一个不许长
回来。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.context import noise_gate as ng  # noqa: E402

ENV = "YINYUE_FEATURE_NOISE_GATE"


def _enable() -> None:
    os.environ[ENV] = "on"


def _disable() -> None:
    os.environ.pop(ENV, None)


def test_off_passes():
    _disable()
    ng.clear_noise_state()
    text = "这句话我刚刚说过了"
    assert ng.noise_verdict(chat_id="c1", sender_id="s1", content=text, now=1000.0).skip is False
    v = ng.noise_verdict(chat_id="c1", sender_id="s1", content=text, now=1030.0)
    assert v.skip is False and v.reason == "off"


def test_exact_repeat_skips_second():
    _enable()
    ng.clear_noise_state()
    text = "这句话我刚刚说过了"
    assert ng.noise_verdict(chat_id="c1", sender_id="s1", content=text, now=1000.0).skip is False
    v = ng.noise_verdict(chat_id="c1", sender_id="s1", content=text, now=1030.0)
    assert v.skip is True and v.reason == "repeat" and v.layer == "message"


def test_punctuation_and_case_do_not_hide_repeat():
    _enable()
    ng.clear_noise_state()
    ng.noise_verdict(chat_id="c1", sender_id="s1", content="Hello  World!!", now=1000.0)
    v = ng.noise_verdict(chat_id="c1", sender_id="s1", content="hello world", now=1005.0)
    assert v.skip is True and v.reason == "repeat"


def test_near_repeat_now_passes():
    """旧近重复闸门（bigram 0.8）已删：差一个字是新话，交模型判。"""
    _enable()
    ng.clear_noise_state()
    assert ng.noise_verdict(chat_id="c1", sender_id="s1", content="白白公主我要亲亲你", now=1000.0).skip is False
    v = ng.noise_verdict(chat_id="c1", sender_id="s1", content="白白公主我要亲亲你呀", now=1010.0)
    assert v.skip is False and v.reason == "fresh"


def test_template_prefix_now_passes():
    """旧模板前缀闸门已删：「[自动]」是字符串，不是刷屏判据。"""
    _enable()
    ng.clear_noise_state()
    v = ng.noise_verdict(chat_id="c1", sender_id="s1", content="【自动】我要亲亲你", now=1000.0)
    assert v.skip is False and v.reason == "fresh"


def test_rate_and_pat_counts_now_pass():
    """旧速率（15/分）与拍人频率（3 次豁免）已删：连发不许被代码拦。"""
    _enable()
    ng.clear_noise_state()
    for i in range(20):
        v = ng.noise_verdict(chat_id="c1", sender_id="pb", content=f"第{i}条不一样的消息", now=1000.0 + i)
        assert v.skip is False, (i, v)
    # 拍人也只剩「完全相同文本」这一条：一样的拍人第二次起算复读，不是「拍门频率」。
    for i in range(6):
        v = ng.noise_verdict(chat_id="c1", sender_id="pat", content=f'"pb" 第{i}次 拍了拍 "鲸鲸"', now=1100.0 + i)
        assert v.skip is False, (i, v)
    first_pat = ng.noise_verdict(chat_id="c1", sender_id="pat2", content='"pb" 拍了拍 "鲸鲸"', now=1200.0)
    assert first_pat.skip is False
    same_pat = ng.noise_verdict(chat_id="c1", sender_id="pat2", content='"pb" 拍了拍 "鲸鲸"', now=1201.0)
    assert same_pat.skip is True and same_pat.reason == "repeat"


def test_window_expiry_passes():
    _enable()
    ng.clear_noise_state()
    text = "十分钟之后再说一遍"
    ng.noise_verdict(chat_id="c1", sender_id="s1", content=text, now=1000.0)
    assert ng.noise_verdict(chat_id="c1", sender_id="s1", content=text, now=1590.0).skip is True
    # 窗口内被记过两次（1000 与 1590）⇒ 1601 仍在 1590 的窗口里；要验的是
    # 「全部记录都过期」这一条，所以清一次状态再验。
    ng.clear_noise_state()
    ng.noise_verdict(chat_id="c1", sender_id="s1", content=text, now=1000.0)
    assert ng.noise_verdict(chat_id="c1", sender_id="s1", content=text, now=1601.0).skip is False


def test_scope_is_per_sender_and_per_chat():
    _enable()
    ng.clear_noise_state()
    text = "同一个人同一句"
    ng.noise_verdict(chat_id="c1", sender_id="s1", content=text, now=1000.0)
    assert ng.noise_verdict(chat_id="c1", sender_id="s2", content=text, now=1001.0).skip is False
    assert ng.noise_verdict(chat_id="c2", sender_id="s1", content=text, now=1002.0).skip is False


def test_short_text_never_skipped():
    _enable()
    ng.clear_noise_state()
    for i in range(3):
        v = ng.noise_verdict(chat_id="c1", sender_id="s1", content="？", now=1000.0 + i)
        assert v.skip is False and v.reason == "short"


def test_owner_and_empty_sender_exempt():
    _enable()
    ng.clear_noise_state()
    text = "主人可以重复说同一句"
    assert ng.noise_verdict(chat_id="c1", sender_id="owner", content=text, is_owner=True, now=1000.0).reason == "owner"
    assert ng.noise_verdict(chat_id="c1", sender_id="owner", content=text, is_owner=True, now=1001.0).skip is False
    assert ng.noise_verdict(chat_id="c1", sender_id="", content=text, now=1002.0).reason == "no_sender"


def test_noise_scope_env():
    os.environ.pop("YINYUE_NOISE_GATE_CHATS", None)
    assert ng.noise_scope() == () and ng.in_noise_scope("any") is True
    os.environ["YINYUE_NOISE_GATE_CHATS"] = " c1 , c2 "
    try:
        assert ng.noise_scope() == ("c1", "c2")
        assert ng.in_noise_scope("c1") is True and ng.in_noise_scope("c3") is False
    finally:
        os.environ.pop("YINYUE_NOISE_GATE_CHATS", None)


def test_record_skipped_writes_message_observed_once():
    from core.context.situation import WorldModel, project

    ng._SKIPPED_LAST.clear()
    model = WorldModel()
    first = ng.record_skipped(
        model, chat_id="c1", sender_id="s1", sender_name="A",
        content="同一句话", reason="repeat",
    )
    second = ng.record_skipped(
        model, chat_id="c1", sender_id="s1", sender_name="A",
        content="同一句话", reason="repeat",
    )
    assert first is True and second is False
    msgs = project(model.events).messages
    assert len(msgs) == 1
    assert msgs[0].who == "A" and msgs[0].genre == "复读"
    third = ng.record_skipped(
        model, chat_id="c1", sender_id="s1", sender_name="A",
        content="不一样的话", reason="other",
    )
    assert third is True
    assert project(model.events).messages[1].genre == "其他"
    ng._SKIPPED_LAST.clear()


def test_static_wiring_and_retired_heuristics():
    from core import features

    assert features.NOISE_GATE == "noise_gate"
    assert features.NOISE_GATE in features.FLAG_REGISTRY
    wiring = (ROOT / "core/routing/turn_pipeline.py").read_text(encoding="utf-8")
    assert "noise_gate_skipped" in wiring and "noise_verdict(" in wiring
    mod_src = (ROOT / "core/context/noise_gate.py").read_text(encoding="utf-8")
    for banned in ("core.bootstrap", "sqlite3", "requests", "httpx"):
        assert banned not in mod_src, banned
    # 六层启发式一个都不许长回来。
    for retired in (
        "TEMPLATE_PREFIXES", "NEAR_DUP_RATIO", "_bigrams", "PAT_PASS_FREE",
        "MESSAGE_RATE_MAX", "FLAGGED_TO_QUIET", "QUIET_SECONDS",
        "FATIGUE_REPLY_MAX", "CHAT_FLAGGED_TO_HOLD", "note_reply",
    ):
        assert retired not in mod_src, retired
        assert retired not in wiring, retired


TESTS = [
    test_record_skipped_writes_message_observed_once,
    test_off_passes,
    test_exact_repeat_skips_second,
    test_punctuation_and_case_do_not_hide_repeat,
    test_near_repeat_now_passes,
    test_template_prefix_now_passes,
    test_rate_and_pat_counts_now_pass,
    test_window_expiry_passes,
    test_scope_is_per_sender_and_per_chat,
    test_short_text_never_skipped,
    test_owner_and_empty_sender_exempt,
    test_noise_scope_env,
    test_static_wiring_and_retired_heuristics,
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
        print(f"noise-gate checks: {len(TESTS)}/{len(TESTS)} passed")
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)