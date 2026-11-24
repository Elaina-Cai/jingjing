"""批C 止损单测：AI 腔套话的机械删除（只删不改、不删空、真人语料不误伤）。

Run directly:
    python tests/test_anti_ai_phrase.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.speech.guards import (  # noqa: E402
    detect_canned_phrases,
    strip_canned_phrases,
    watch_canned_phrases,
)


def test_strip_listen_posture():
    out, hits = strip_canned_phrases(
        "怎么啦，累成这样？是活儿太多还是没睡好呀，跟我说说，我听着呢。"
    )
    assert hits == ["跟我说说", "我听着呢"], hits
    assert out == "怎么啦，累成这样？是活儿太多还是没睡好呀", repr(out)
    return True


def test_strip_advice_phrase():
    # 2026-09-27 去凶：关心句（别硬撑/注意身体/照顾好自己）转观察名单只记不删；
    # 硬名单只保留倾听/卖惨套话。此处同步为「观察命中、不删句」。
    out, hits = strip_canned_phrases("P5，咋啦，忙一天了？先歇会儿，别硬撑。")
    assert hits == [] and "别硬撑" in out, (hits, out)
    assert watch_canned_phrases("别硬撑") == ["别硬撑"]
    return True


def test_no_hit_untouched():
    for text in (
        "诶我去，智械危机了",
        "太可恶了",
        "签个到卡三次，换谁都得累趴。",
        "P5这表情是冲谁来的？我这边没接上号，先声明没惹你。",
    ):
        out, hits = strip_canned_phrases(text)
        assert out == text and hits == [], (text, out, hits)
    return True


def test_never_returns_empty():
    # 整句就是套话时：宁可留着，也不能删成空（保证发送内容非空由调用方兜底）。
    out, hits = strip_canned_phrases("别硬撑")
    assert out == "别硬撑" and hits == [], (out, hits)
    return True


def test_watch_list_is_log_only():
    assert detect_canned_phrases("先歇会儿吧") == []
    assert watch_canned_phrases("先歇会儿吧") == ["先歇会儿"]
    return True


def test_real_group_replies_survive():
    # 真人语料抽查（2026-09-26 群聊采样）：这些必须原样保留。
    samples = (
        "诶我去，智械危机了",
        "希望我忘了删",
        "太可恶了",
        "这都触发了",
        "我去，我还看到还有更贵的",
        "sensitive",
    )
    for sample in samples:
        out, hits = strip_canned_phrases(sample)
        assert out == sample and not hits, sample
    return True


def main() -> bool:
    tests = [
        test_strip_listen_posture,
        test_strip_advice_phrase,
        test_no_hit_untouched,
        test_never_returns_empty,
        test_watch_list_is_log_only,
        test_real_group_replies_survive,
    ]
    print("=" * 50)
    print("AI 腔套话止损测试")
    print("=" * 50)
    failed: list[str] = []
    for test in tests:
        try:
            test()
            print(f"  ok   {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed.append(test.__name__)
            print(f"  FAIL {test.__name__}: {exc!r}")
    print(f"\nanti-ai-phrase checks: {len(tests) - len(failed)}/{len(tests)} passed")
    return not failed


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
