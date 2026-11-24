# -*- coding: utf-8 -*-
"""人话 voice 注入块（VOICE_INJECT 三态）检查：standalone + pytest 兼容。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.speech import voice_style as vs  # noqa: E402

ENV = "YINYUE_FEATURE_VOICE_INJECT"


def test_mode_default_off_and_reads_tri():
    old = os.environ.get(ENV)
    try:
        os.environ.pop(ENV, None)
        assert vs.voice_inject_mode() == "off"
        os.environ[ENV] = "shadow"
        assert vs.voice_inject_mode() == "shadow"
        os.environ[ENV] = "on"
        assert vs.voice_inject_mode() == "on"
    finally:
        if old is None:
            os.environ.pop(ENV, None)
        else:
            os.environ[ENV] = old


def test_constants_cover_two_registers():
    assert "群聊" in vs.VOICE_GROUP and "短句" in vs.VOICE_GROUP and "不解释" in vs.VOICE_GROUP
    assert "私聊" in vs.VOICE_PRIVATE and "主人" in vs.VOICE_PRIVATE and "禁止" in vs.VOICE_PRIVATE


def test_build_voice_block_only_on():
    assert vs.build_voice_block(True, mode="off") == ""
    assert vs.build_voice_block(True, mode="shadow") == ""
    group = vs.build_voice_block(True, mode="on")
    private = vs.build_voice_block(False, mode="on")
    assert "短句" in group and "群聊" in group
    assert "主人" in private and "禁止" in private


def test_static_wiring():
    import inspect

    from agents.conversational import ConversationalAgent

    prompt_src = inspect.getsource(ConversationalAgent._build_system_prompt_from_snapshot)
    assert "return _voice_inject_block(prompt, is_group, snapshot)" in prompt_src, \
        "语气块没接在提示词的返回处"
    feat = (ROOT / "core/features.py").read_text(encoding="utf-8")
    assert 'VOICE_INJECT = "voice_inject"' in feat
    env = (ROOT / "tests/test_env_flag_isolation.py").read_text(encoding="utf-8")
    assert "YINYUE_FEATURE_VOICE_INJECT" in env


def test_pragmatic_rules_present():
    """人话问题样本 #1（m07677）：技术故障被说成"哪个先动的手？"= 打架域比喻翻车（要求规则含同域/拿不准）。"""
    assert "同域" in vs.VOICE_GROUP and "秒懂" in vs.VOICE_GROUP
    assert "拿不准" in vs.VOICE_GROUP and "直接说事" in vs.VOICE_GROUP


TESTS = [
    test_mode_default_off_and_reads_tri,
    test_constants_cover_two_registers,
    test_build_voice_block_only_on,
    test_static_wiring,
    test_pragmatic_rules_present,
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
        print(f"voice-style checks: {len(TESTS)}/{len(TESTS)} passed")
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
