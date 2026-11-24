# -*- coding: utf-8 -*-
"""文字型表情包 token 解码回归（2026-09-28 媒体可读）。

例：ChcKB2RlZmF1bHQSDOaDs+WQg+mlreWWtQ== → 想吃饭喵（此前整串 base64 进 prompt）。

Run directly:  python tests/test_sticker_text_decode.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.preprocess import emoji_recognition as er  # noqa: E402

CASES = (
    ("ChcKB2RlZmF1bHQSDOaDs+WQg+mlreWWtQ==", "想吃饭喵"),
    ("ChQKB2RlZmF1bHQSCeeVpeeVpeeVpQ==", "略略略"),
    ("Cg4KB2RlZmF1bHQSA+WTjgo=", "哎"),
)


def test_decode_text_tokens() -> bool:
    ok = True
    for token, expect in CASES:
        got = er.decode_sticker_token(token)
        if got != expect:
            ok = False
            print(f"  ✗ {token[:24]} → {got!r}（期望 {expect!r}）")
    print(f"  {'✓' if ok else '✗'} 解码文字型 token: {len(CASES)} 例")
    return ok


def test_decode_rejects_non_text() -> bool:
    cases = [
        ("f" * 32, ""),            # md5 → 走描述表
        ("捂脸笑", ""),             # 本来就是文字描述
        ("", ""),
        ("abc", ""),               # 太短
        ("!!!!!!!!!!!!!!!!", ""),  # 非 base64
    ]
    ok = all(er.decode_sticker_token(tok) == "" for tok, _ in cases)
    print(f"  {'✓' if ok else '✗'} 非文字型一律不解码（不制造假信息）")
    return ok


def test_replace_mixed_text() -> bool:
    text = (
        "[表情包:ChQKB2RlZmF1bHQSCeeVpeeVpeeVpQ==] 你好 "
        "[表情包:ChcKB2RlZmF1bHQSDOaDs+WQg+mlreWWtQ==]"
    )
    got = er.replace_emoji_descriptions(text)
    ok = got == "[表情包:略略略] 你好 [表情包:想吃饭喵]"
    print(f"  {'✓' if ok else '✗'} 混合文本替换: {got!r}")
    return ok


def test_md5_behavior_unchanged() -> bool:
    """md5 仍走描述表；未知 md5 原样保留（不回归）。"""
    unknown = "f" * 32
    ok1 = er.replace_emoji_descriptions(f"[表情包:{unknown}]") == f"[表情包:{unknown}]"
    cache = er._load_cache()
    ok2 = True
    if cache:
        md5 = next(iter(cache))
        er._reset_owner_index()
        expect = er.get_emoji_description(md5)  # 主人命名优先，取最终口径
        got = er.replace_emoji_descriptions(f"[表情包:{md5}]")
        ok2 = bool(expect) and got == f"[表情包:{expect}]"
    print(f"  {'✓' if ok1 and ok2 else '✗'} md5 行为不变: 未知保留={ok1} 有描述替换={ok2}")
    return bool(ok1 and ok2)


def test_no_placeholder_untouched() -> bool:
    text = "普通文本，没有表情包占位符"
    ok = er.replace_emoji_descriptions(text) == text
    print(f"  {'✓' if ok else '✗'} 无占位符原样返回")
    return ok


def main() -> int:
    tests = [
        test_decode_text_tokens,
        test_decode_rejects_non_text,
        test_replace_mixed_text,
        test_md5_behavior_unchanged,
        test_no_placeholder_untouched,
    ]
    results = []
    for test in tests:
        try:
            results.append((test.__name__, bool(test())))
        except Exception:  # pragma: no cover
            import traceback

            traceback.print_exc()
            results.append((test.__name__, False))
    print()
    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'} {name}")
    failed = [n for n, ok in results if not ok]
    print(f"sticker text decode checks: {len(results) - len(failed)}/{len(results)} passed")
    return 0 if not failed else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
