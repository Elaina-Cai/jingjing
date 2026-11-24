"""语气残留出口止损单测：**只删「赌气罢言」**，其余 10 类只检测不删（2026-10-10 主人裁定）。

检测依旧要钉住（新标签没进表、正则失效照样红），但不许再删——反将一军/算旧账/追问归因
这些是她的回嘴与人味，整句删掉就成了没脾气的客服（依据 scripts/expr_signal_report.py：
532 条真实回复命中 9 条，人读全部合格）。

Run directly:
    python tests/test_residual_tail_guard.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.speech.guards import (  # noqa: E402
    ContentGuard,
    detect_residual_tails,
    strip_leading_timestamp,
    strip_residual_tails,
)


def test_keep_backtalk_tail_but_detect_it():
    # 反将一军 + 反查对方：2026-10-10 起只检测不删（人味来源：会回嘴的人不能变成客服）。
    text = "没摸，我一直在呢。倒是你，拍我一下就为查岗？"
    out, hits = strip_residual_tails(text)
    assert out == text and hits == [], (out, hits)
    labels = detect_residual_tails(text)
    assert "反将一军" in labels and "反查对方" in labels, labels
    return True


def test_keep_old_grudge_tail_but_detect_it():
    # 旧账菜名：只检测不删。当年报红是因为整句被删了，不是因为没认出来。
    text = "嫌我话多了？行，那我少说两句。反正该报的菜名一个字没漏。"
    out, hits = strip_residual_tails(text)
    assert out == text and hits == [], (out, hits)
    labels = detect_residual_tails(text)
    assert labels == ["旧账菜名"], labels
    return True


def test_strip_leading_timestamp_cases():
    """S2 实验兜底：行首 [HH:MM]/[HH:MM:SS] 只删前缀，其余不动。"""
    cases = [
        ("[12:45] 你好", "你好", True),
        ("[11:50]@P6 官渡校区门口", "@P6 官渡校区门口", True),
        ("  [9:05:07] hi", "hi", True),
        ("没有前缀", "没有前缀", False),
        ("[12:45]", "", True),
        ("[引用 P5: 12:45 的事] 正文", "[引用 P5: 12:45 的事] 正文", False),
    ]
    for raw, expect, hit in cases:
        got, got_hit = strip_leading_timestamp(raw)
        assert (got, got_hit) == (expect, hit), (raw, got, got_hit)
    print("  OK  strip_leading_timestamp: 6 例全对")
    return True


def test_guard_rewrites_leading_timestamp():
    """ContentGuard 兜底：行首时间戳 → REWRITE 去前缀；无前缀 → ALLOW 原样。"""
    from types import SimpleNamespace

    policy = SimpleNamespace(
        output_policy=SimpleNamespace(
            max_chars=0, max_lines=0, forbidden_terms=[],
            required_phrases=[], replacement_text="",
        ),
        hard_constraints=[],
    )
    from core.speech.domain import PolicyAction

    guard = ContentGuard()
    hit = guard.check("[12:45] 你好", policy)
    assert hit.rewritten_content == "你好", hit
    # 必须是 REPLACE：router 的发送前检查只应用 REPLACE（REWRITE 会被忽略）
    assert hit.action == PolicyAction.REPLACE, hit
    clean = guard.check("你好", policy)
    assert clean.action == PolicyAction.ALLOW, clean
    assert clean.rewritten_content == "" and clean.allowed, clean
    print("  OK  ContentGuard: 前缀命中→REWRITE，干净稿→ALLOW")
    return True


def test_no_hit_untouched():
    for text in (
        "没摸，我一直在呢。",
        "新生要这么报菜名，学长学姐的避雷名单第一个就是你。",
        "这活儿我不接，闹钟得他自己定。",
        "好，你们聊。",
        "行，那我简着说，你问我就答。",
    ):
        out, hits = strip_residual_tails(text)
        assert out == text and hits == [], (text, out, hits)
    return True


def test_never_returns_empty():
    # 整句就是残留：宁可留着，也不能删成空（空内容由调用方按沉默协议处理）。
    out, hits = strip_residual_tails("倒是你，拍我一下就跑？")
    assert out == "倒是你，拍我一下就跑？" and hits == [], (out, hits)
    return True


def test_keep_old_grudge_variants_but_detect_them():
    # 模型把「菜名」省成「该报的」、把「没漏」拆到下一个子句：检测仍要认得，但一个字都不删。
    for text, labels in (
        (
            "嫌我话多啦？行，那我简着说，菜名报完了，没漏。",
            ["旧账报完", "旧账没漏"],
        ),
        (
            "嫌我话多啦？行，那我简着说，反正该报的一个字没漏。",
            ["旧账没漏"],
        ),
    ):
        out, hits = strip_residual_tails(text)
        assert out == text and hits == [], (text, out, hits)
        assert detect_residual_tails(text) == labels, (text, detect_residual_tails(text))
    return True


def test_keep_backtalk_half_sentence_but_detect_it():
    # 「倒是你，」后跟非疑问子句：半句也要被检测到，但整句原样出（不再切句）。
    text = "没摸，我一直在呢。倒是你，拍我一下就跑"
    out, hits = strip_residual_tails(text)
    assert out == text and hits == [], (out, hits)
    labels = detect_residual_tails(text)
    assert "反将一军" in labels, labels
    return True


def test_keep_old_grudge_more_but_detect_them():
    # 「被使唤报菜名」与「你先@我的」同样是记账/抬杠，是人味来源：认得出来，但不许删。
    for text, label in (
        ("嫌我话多啦？行，我简着说，刚才那几句是被使唤报菜名报出来的。", "旧账菜名"),
        ("刚才不是你先@我的嘛。行，我简着说。", "算旧账"),
    ):
        out, hits = strip_residual_tails(text)
        assert out == text and hits == [], (text, out, hits)
        assert label in detect_residual_tails(text), (text, detect_residual_tails(text))
    return True


def test_strip_betting_tail():
    # 赌气罢言（那我不说了）连同紧随子句一起删；整句只有赌气时回退原样。
    out, hits = strip_residual_tails("嫌我话多？那不说了，省点力气。")
    assert out == "嫌我话多？", repr(out)
    assert "赌气罢言" in hits, hits
    out2, hits2 = strip_residual_tails("那不说了，省点力气。")
    assert out2 == "那不说了，省点力气。" and hits2 == [], (out2, hits2)
    return True


def test_keep_blame_question_but_detect_it():
    # 追问归因（谁惹你了/谁惹的/谁气的）：只检测不删；整句只有它时也照原样出。
    text = "咋啦？谁惹你了？"
    out, hits = strip_residual_tails(text)
    assert out == text and hits == [], (out, hits)
    labels = detect_residual_tails(text)
    assert "追问归因" in labels, labels
    only = "谁惹你了？"
    out2, hits2 = strip_residual_tails(only)
    assert out2 == only and hits2 == [], (out2, hits2)
    assert detect_residual_tails(only) == ["追问归因"], detect_residual_tails(only)
    return True


def test_detect_only():
    assert detect_residual_tails("咱俩扯平。") == ["算账扯平"]
    assert detect_residual_tails("正常聊天。") == []
    return True


def main() -> bool:
    tests = [
        test_strip_leading_timestamp_cases,
        test_guard_rewrites_leading_timestamp,
        test_keep_backtalk_tail_but_detect_it,
        test_keep_old_grudge_tail_but_detect_it,
        test_no_hit_untouched,
        test_never_returns_empty,
        test_keep_backtalk_half_sentence_but_detect_it,
        test_keep_old_grudge_variants_but_detect_them,
        test_strip_betting_tail,
        test_keep_blame_question_but_detect_it,
        test_keep_old_grudge_more_but_detect_them,
        test_detect_only,
    ]
    print("=" * 50)
    print("语气残留出口止损测试")
    print("=" * 50)
    failed: list[str] = []
    for test in tests:
        try:
            test()
            print(f"  ok   {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed.append(test.__name__)
            print(f"  FAIL {test.__name__}: {exc!r}")
    print(f"\nresidual-tail checks: {len(tests) - len(failed)}/{len(tests)} passed")
    return not failed


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
