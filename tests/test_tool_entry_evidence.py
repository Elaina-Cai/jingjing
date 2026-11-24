# -*- coding: utf-8 -*-
"""工具入口证据化检查（2026-10-15）：无任务证据不给工具、不进工具循环。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.conversational import _tools_evidence  # noqa: E402


def test_banter_has_no_evidence():
    assert _tools_evidence({}, "这波元素叔叔先退让者幸存") is False
    assert _tools_evidence({}, "你是笨蛋吗") is False
    assert _tools_evidence({}, "哈哈哈哈") is False


def test_explicit_asks_have_evidence():
    assert _tools_evidence({}, "帮我查一下今天的新闻") is True
    assert _tools_evidence({}, "搜一下 元素周期表") is True
    assert _tools_evidence({}, "提醒我八点开会") is True
    assert _tools_evidence({}, "帮我整理一份名单") is True


def test_task_session_evidence():
    assert _tools_evidence({"task_session": True}, "随便") is True
    assert _tools_evidence({"reply_intent": {"intent": "闲聊"}}, "随便") is False


def test_static_wiring():
    import inspect

    from agents.conversational import ConversationalAgent

    loop_src = inspect.getsource(ConversationalAgent._run_persona_brain_turn_stream)
    # 2026-10-22：关键词闸退役后 `_tools_on` 恒为 True，已整块删掉（含不可达的
    # `tool_loop_skipped` 与循环里的 break）。现在守的是它真正想守的那件事：
    # 没有可用 schema（或模型不支持 tools）时**立刻**走单轮 loop，不进工具循环。
    assert "if not tool_schemas or not hasattr(llm" in loop_src, \
        "空 schema 时没在工具循环入口处把关"
    assert "_tools_on" not in loop_src, "恒真的工具开关又回潮了"


TESTS = [
    test_banter_has_no_evidence,
    test_explicit_asks_have_evidence,
    test_task_session_evidence,
    test_static_wiring,
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
        print(f"tool entry evidence checks: {len(TESTS)}/{len(TESTS)} passed")
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
