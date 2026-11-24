"""Calibration board (B5b) checks: aggregate the judgment signals correctly.

Run directly: python tests/test_calibration_board.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location(
    "calibration_board", ROOT / "scripts" / "calibration_board.py"
)
board = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(board)


def test_aggregates_skip_and_render_signals() -> bool:
    print("\n[汇总：否决 / 出稿 / 自指改写]")
    records = [
        {"event": "group_turn_skipped", "reason": "low_signal_ambient",
         "content": "没艾特"},
        {"event": "group_turn_skipped", "reason": "low_signal_ambient",
         "content": "又有问题，唉"},
        {"event": "group_turn_skipped", "reason": "meta_marker",
         "content": "🤔，这LLM看到啥了"},
        {"event": "speak_contract_rendered", "draft_chars": 100,
         "speak_chars": 40, "silent": False},
        {"event": "speak_contract_rendered", "draft_chars": 30,
         "speak_chars": 0, "silent": True},
        {"event": "speak_render_failed"},
        {"event": "self_voice_rewritten", "chars": 26},
        {"event": "self_voice_rewrite_failed"},
        {"event": "something_else"},
    ]
    got = board.summarize(records, days=None)
    checks = [
        ("low_signal 计 2", got["skips"].get("low_signal_ambient") == 2),
        ("meta_marker 计 1", got["skips"].get("meta_marker") == 1),
        ("样本只留原文", got["skip_samples"]["low_signal_ambient"]
         == ["没艾特", "又有问题，唉"]),
        ("出稿计 2 次", got["render"]["count"] == 2),
        ("草稿/出口字数", got["render"]["draft_chars"] == 130
         and got["render"]["speak_chars"] == 40),
        ("沉默计 1", got["render"]["silent"] == 1),
        ("出稿失败计 1", got["render_failed"] == 1),
        ("自指改写", got["self_rewrite"] == 1),
        ("自指失败", got["self_rewrite_failed"] == 1),
    ]
    return _report(checks)


def test_ignores_other_skip_reasons_and_bad_records() -> bool:
    print("\n[只统计本批信号；坏行不炸]")
    got = board.summarize([
        {"event": "group_turn_skipped", "reason": "mentioned_in_group",
         "content": "不该统计"},
        {"event": "group_turn_skipped", "reason": "other_pair_qa_not_about_her",
         "content": "该统计"},
        None,
        "not-a-dict",
        {"event": "speak_contract_rendered"},
    ], days=None)
    checks = [
        ("非本批 reason 不计", got["skips"].get("mentioned_in_group") is None),
        ("2c 判代理由计 1", got["skips"].get("other_pair_qa_not_about_her") == 1),
        ("缺字段不炸", got["render"]["count"] == 1),
    ]
    return _report(checks)


def test_format_and_json_are_human_and_machine_readable() -> bool:
    print("\n[输出：人看的短报表 + 机器可读 JSON]")
    summary = board.summarize([
        {"event": "group_turn_skipped", "reason": "meta_pronoun",
         "content": "他为什么要回复我"},
        {"event": "speak_contract_rendered", "draft_chars": 50,
         "speak_chars": 50, "silent": False},
    ], days=None)
    text = board._format(summary, 7)
    dumped = json.dumps(summary, ensure_ascii=False)
    checks = [
        ("报表含标题", "判断校准看板" in text),
        ("报表含样本", "他为什么要回复我" in text),
        ("报表含留存", "留存 100%" in text),
        ("JSON 可解析", json.loads(dumped)["skips"]["meta_pronoun"] == 1),
    ]
    return _report(checks)


def _report(checks: list[tuple[str, bool]]) -> bool:
    ok = True
    for name, good in checks:
        ok &= bool(good)
        print(f"  {'✓' if good else '✗'} {name}")
    return bool(ok)


def main() -> bool:
    print("=" * 60)
    print("calibration board checks")
    print("=" * 60)
    tests = [
        test_aggregates_skip_and_render_signals,
        test_ignores_other_skip_reasons_and_bad_records,
        test_format_and_json_are_human_and_machine_readable,
    ]
    results = []
    for test in tests:
        try:
            results.append((test.__name__, bool(test())))
        except Exception:  # pragma: no cover - diagnostics only
            import traceback

            traceback.print_exc()
            results.append((test.__name__, False))
    print("\n" + "=" * 60)
    passed = sum(1 for _, ok in results if ok)
    for name, ok in results:
        print(f"  {'ok  ' if ok else 'FAIL'} {name}")
    print(f"{passed}/{len(results)} passed")
    return passed == len(results)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
