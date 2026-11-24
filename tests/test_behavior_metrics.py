# -*- coding: utf-8 -*-
"""行为指标量具（scripts/report_behavior_metrics.py）检查：合成数据 + 词表同步锁。"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for candidate in (ROOT, ROOT / "scripts"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

import report_behavior_metrics as m  # noqa: E402

T0 = datetime(2026, 10, 15, 12, 0, 0)


def _pairs(*rows):
    return [
        {"ts": T0 + timedelta(seconds=offset), "text": text, "inbound": inbound}
        for offset, text, inbound in rows
    ]


def test_near_duplicate_pair_flagged():
    pairs = _pairs(
        (0, "又是这条。行吧，收下了，但你这词儿是真打算用到我退休是吧。", ""),
        (45, "还是这条，一个字没动。行，收下了，心意我记着，但你这循环是真打算用到我退休是吧。", ""),
    )
    got = m.similar_pairs(pairs, top=5)
    assert got["near_total"] == 1, got
    assert got["flip_total"] == 0, got


def test_unrelated_pair_not_flagged():
    pairs = _pairs(
        (0, "早上好，今天要开会。", ""),
        (30, "这个链接别点，上次 apk 的教训还热乎。", ""),
    )
    got = m.similar_pairs(pairs, top=5)
    assert got["near_total"] == 0 and got["flip_total"] == 0, got


def test_accusation_hit():
    pairs = _pairs(
        (0, "有啊，怎么啦。", ""),
        (60, "我哪来的手机。", "你自己说的有手机，现在又不认？"),
    )
    samples, total = m.accusation_hits(pairs, top=5)
    assert total == 1
    assert samples[0]["inbound"].startswith("你自己说的")
    assert samples[0]["prev_replies"] == ["有啊，怎么啦。"]


def _event(ts, event, **fields):
    payload = {"timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z", "event": event}
    payload.update(fields)
    return {"ts": ts, "event": event, "raw": payload}


def test_task_chain_break_and_alive():
    events = [
        _event(T0, "serious_signals", signals=["delegated"], content="帮我把文档整理一下", chat_id="c1"),
        _event(T0 + timedelta(minutes=5), "group_trigger_fired", chat_id="c1", question="那个弄好了吗"),
        _event(T0 + timedelta(minutes=5, seconds=2), "tool_loop_skipped", chat_id="c1", reason="no_task_evidence"),
        _event(T0 + timedelta(minutes=30), "serious_signals", signals=["promised"], content="我明天给你", chat_id="c1"),
        _event(T0 + timedelta(minutes=35), "group_trigger_fired", chat_id="c1", question="说好的呢"),
    ]
    got = m.task_chain(events, top=5)
    assert got["starts"] == 2, got
    assert got["broken"] == 1 and got["alive"] == 1, got


def test_tool_false_open_and_legit_ask():
    events = [
        _event(T0, "group_trigger_fired", chat_id="c1", question="这波我赢了哈哈哈"),
        _event(T0 + timedelta(seconds=3), "reply_intent_resolved", chat_id="c1", intent="玩梗"),
        _event(T0 + timedelta(seconds=5), "agent_tool_call", tool="send_message", input="{}"),
        _event(T0 + timedelta(minutes=10), "group_trigger_fired", chat_id="c1", question="帮我查一下明天的天气"),
        _event(T0 + timedelta(minutes=10, seconds=3), "agent_tool_call", tool="get_weather", input="{}"),
    ]
    got = m.tool_false_open(events, top=5)
    assert got["opened"] == 2, got
    assert got["suspected"] == 1, got
    assert got["samples"][0]["tool"] == "send_message"


def test_ask_words_synced_with_runtime():
    """指标侧的询问词表要和运行时那条正则对得上——直接比正则对象，不读源码文本。"""
    import agents.conversational as conv_module

    runtime = conv_module._TOOL_ASK_RE.pattern
    for word in m.TOOL_ASK_PATTERN.split("|"):
        assert word in runtime, f"指标词表里的 {word!r} 运行时正则不认"


TESTS = [
    test_near_duplicate_pair_flagged,
    test_unrelated_pair_not_flagged,
    test_accusation_hit,
    test_task_chain_break_and_alive,
    test_tool_false_open_and_legit_ask,
    test_ask_words_synced_with_runtime,
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
        print(f"behavior-metrics checks: {len(TESTS)}/{len(TESTS)} passed")
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
