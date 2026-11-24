# -*- coding: utf-8 -*-
"""轮次通道（升级为对话轮次）回归：

* 同一份 rows 派生两种形态：``text_full``（现状）/``text_digest``（通道分离）；
* ``messages_from_window``：自己=assistant、别人=user+name、系统事件=name 系统；
* ``build_turn_world`` 在开关+白名单下产出 turns、并把窗口文本切成 digest；
* L2 准入 judge 的 A 行可复用于轮次（``AdmissionResult.rows`` / ``scene_text``）。

Run directly:  python tests/test_turns_channel.py
"""
from __future__ import annotations

import asyncio
import io
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.context.admission import evaluate  # noqa: E402
from core.context.chat_window import (  # noqa: E402
    WindowMessage,
    build_chat_window,
    build_window_messages,
    build_window_parts,
    messages_from_window,
    turns_enabled_for,
)
from core.context.world_state import build_turn_world  # noqa: E402

_BASE = datetime(2026, 9, 28, 14, 0, 0)  # 固定白天基时：跨零点跑时按天分桶会变，测试需要确定性

WINDOW_FLAG = "YINYUE_FEATURE_CHAT_WINDOW"
WINDOW_CHATS = "YINYUE_CHAT_WINDOW_CHATS"
TURNS = "YINYUE_TURNS_MODE"


class _env:
    def __init__(self, **values: str | None):
        self._values = values

    def __enter__(self):
        self._saved = {key: os.environ.get(key) for key in self._values}
        for key, value in self._values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def __exit__(self, *exc):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _rows(base: datetime, n: int = 60) -> list[WindowMessage]:
    return [
        WindowMessage(
            base - timedelta(minutes=60 - i),
            "鲸鲸" if i % 3 == 0 else "P4",
            f"消息{i}",
            is_self=(i % 3 == 0),
        )
        for i in range(n)
    ]


def test_two_forms_same_source() -> bool:
    base = _BASE
    rows = _rows(base)
    kw = dict(messages=rows, full=40, now=base, budget_chars=3600)
    parts = build_window_parts("c1", **kw)
    text = build_chat_window("c1", **kw)
    msgs = build_window_messages("c1", **kw)
    ok1 = parts.text_full == text
    ok2 = "【最近对话·原文】" not in parts.text_digest and "【今天更早·压缩】" in parts.text_digest
    ok3 = len(parts.recent) == len(msgs) == 40
    ok4 = all((m["role"] == "assistant") == r.is_self for m, r in zip(msgs, parts.recent))
    ok5 = {m["name"] for m in msgs} == {"鲸鲸", "P4"}
    ok6 = msgs[-1]["content"].endswith("消息59")
    print(f"  {'✓' if all((ok1, ok2, ok3, ok4, ok5, ok6)) else '✗'} 同源双形态: "
          f"text一致={ok1} digest去原文={ok2} 40条={ok3} role对齐={ok4} name={ok5} 末条={ok6}")
    return bool(ok1 and ok2 and ok3 and ok4 and ok5 and ok6)


def test_system_events_are_records() -> bool:
    base = _BASE
    rows = [
        WindowMessage(base - timedelta(minutes=3), "P5", "在吗", is_self=False),
        WindowMessage(base - timedelta(minutes=2), "P5", "“P5” 拍了拍 “鲸鲸”", is_self=False, is_pat=True),
        WindowMessage(base - timedelta(minutes=1), "P4",
                      '<sysmsg type="revokemsg"><revokemsg><content>"P4" 撤回了一条消息</content>'
                      "</revokemsg></sysmsg>", is_self=False, is_revoke=True),
    ]
    msgs = build_window_messages("c1", messages=rows, now=base)
    pat, revoke = msgs[1], msgs[2]
    ok = (
        pat["role"] == "user" and pat["name"] == "系统" and "拍了拍" in pat["content"]
        and revoke["name"] == "系统" and "撤回" in revoke["content"] and "[引用" not in revoke["content"]
    )
    print(f"  {'✓' if ok else '✗'} 拍一拍/撤回=系统记录: {pat['content']} | {revoke['content']}")
    return bool(ok)


def test_turns_switch_and_world() -> bool:
    base = _BASE
    rows = _rows(base)
    with _env(**{WINDOW_FLAG: "1", WINDOW_CHATS: "c1", TURNS: "off"}):
        off = build_turn_world("c1", now=base, loader=lambda: list(rows))
        off_flag = turns_enabled_for("c1")
    with _env(**{WINDOW_FLAG: "1", WINDOW_CHATS: "c1", TURNS: "on"}):
        on = build_turn_world("c1", now=base, loader=lambda: list(rows))
        on_flag = turns_enabled_for("c1")
        other = turns_enabled_for("c2")
        off2 = build_turn_world("c2", now=base, loader=lambda: list(rows))
    ok1 = (not off_flag) and off.turns == [] and "【最近对话·原文】" in off.window_text
    ok2 = on_flag and (not other) and len(on.turns) == 40 and on.turns_hash
    ok3 = "【最近对话·原文】" not in on.window_text and "【最近对话·原文】" in on.window_full_text
    ok4 = on.window_full_hash and on.window_hash and on.window_full_hash != on.window_hash
    ok5 = off2.turns == [] and off2.window_text == ""
    ok6 = on.as_dict().get("turns_n") == 40 and on.as_dict().get("turns_chars", 0) > 0
    ok = all((ok1, ok2, ok3, ok4, ok5, ok6))
    print(f"  {'✓' if ok else '✗'} 开关+白名单: off={ok1} on={ok2} 通道分离={ok3} 双hash={ok4} 白名单外={ok5} trace={ok6}")
    return bool(ok)


def test_admission_rows_are_rules_only() -> bool:
    """准入不再调模型（2026-10-23 删 judge）：只剩规则窗口那一段。

    旧契约是"LLM 判 A 行 + 场面备注"，judge 删掉后这些都不存在了；仍然成立的是：
    候选/窗口照算、文本里仍有原文块、**拿到 llm 也不调用**。
    """
    base = _BASE
    rows = [
        WindowMessage(base - timedelta(minutes=5), "P4", "表啊", is_self=False),
        WindowMessage(base - timedelta(minutes=4), "P7", "打印什么？", is_self=False),
        WindowMessage(base - timedelta(minutes=1), "P4", "手绘吗", is_self=False),
    ]

    class _FakeLLM:
        calls = 0

        async def chat(self, system, user, **kwargs):
            _FakeLLM.calls += 1
            return '{"labels": {"2": "A"}}'

    with _env(**{WINDOW_FLAG: "1", WINDOW_CHATS: "c@chatroom", "YINYUE_ADMISSION_MODE": "on",
                 "YINYUE_FEATURE_REPLY_INTENT": "shadow"}):
        result = asyncio.run(evaluate(
            chat_id="c@chatroom", reason="mentioned_in_group", current="新的消息",
            sender_name="P4", now=base, rows=rows, llm=_FakeLLM(),
        ))
    ok1 = result is not None and result.fallback in {"t2", "full"}
    ok2 = result is not None and result.scene_text == ""
    ok3 = result is not None and "【最近对话·原文·仅直接相关】" in result.text
    ok4 = _FakeLLM.calls == 0
    ok = bool(ok1 and ok2 and ok3 and ok4)
    print(f"  {'✓' if ok else '✗'} 准入只剩规则窗口: fallback={getattr(result, 'fallback', '')} "
          f"scene={getattr(result, 'scene_text', '')!r} llm_calls={_FakeLLM.calls}")
    return ok


def test_no_pending_default() -> bool:
    """无 window_turns 时主路径 helper 返回空（旧形态不变）。"""
    from agents.conversational import _messages_with_turns, _window_turns

    ok1 = _window_turns(None) == []
    ok2 = _window_turns({"window_turns": []}) == []
    ok3 = _window_turns({"window_turns": [{"role": "user", "content": "hi"}]}) == [
        {"role": "user", "name": "", "content": "hi"}
    ]
    ok4 = _window_turns({"window_turns": [{"role": "tool", "content": "x"}]}) == []
    turns = [{"role": "user", "name": "P5", "content": "[10:00] 在吗"},
             {"role": "assistant", "name": "鲸鲸", "content": "[10:01] 在"}]
    msgs = _messages_with_turns("SYS", turns, "USER")
    ok5 = [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
    ok6 = msgs[2]["name"] == "鲸鲸" and msgs[-1]["content"] == "USER"
    msgs_old = _messages_with_turns("SYS", [], "USER")
    ok7 = [m["role"] for m in msgs_old] == ["system", "user"]
    ok = bool(ok1 and ok2 and ok3 and ok4 and ok5 and ok6 and ok7)
    print(f"  {'✓' if ok else '✗'} _window_turns + _messages_with_turns: "
          f"过滤={ok4} 轮次顺序={ok5} 旧形态={ok7}")
    return ok


def test_turns_timestamp_switch() -> bool:
    """轮次时间戳开关：默认不带 [HH:MM]；YINYUE_TURNS_TIMESTAMP=on 恢复旧口径。"""
    base = _BASE
    rows = [WindowMessage(base, "P4", "在吗", is_self=False)]
    with _env(**{"YINYUE_TURNS_TIMESTAMP": None}):
        default = messages_from_window(rows)
    with _env(**{"YINYUE_TURNS_TIMESTAMP": "on"}):
        with_ts = messages_from_window(rows)
    ok1 = default[0]["content"] == "在吗"
    ok2 = with_ts[0]["content"].startswith("[") and "在吗" in with_ts[0]["content"]
    ok = bool(ok1 and ok2)
    print(f"  {'✓' if ok else '✗'} 时间戳开关: 默认={default[0]['content']!r} on={with_ts[0]['content']!r}")
    return ok


def main() -> int:
    tests = [
        test_two_forms_same_source,
        test_system_events_are_records,
        test_turns_switch_and_world,
        test_admission_rows_are_rules_only,
        test_no_pending_default,
        test_turns_timestamp_switch,
    ]
    results = []
    for test in tests:
        try:
            results.append((test.__name__, bool(test())))
        except Exception:  # pragma: no cover - diagnostics
            import traceback

            traceback.print_exc()
            results.append((test.__name__, False))
    print()
    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'} {name}")
    failed = [n for n, ok in results if not ok]
    print(f"turns channel checks: {len(results) - len(failed)}/{len(results)} passed")
    return 0 if not failed else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
