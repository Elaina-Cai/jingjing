"""Phase 1b: user-prompt shared blocks reproduce the legacy strings.

Run directly:
    python tests/test_context_user_prompt.py

Covers:
* the unit output of ``core.context.user_prompt`` against the frozen legacy
  literals (time/affect prefix, group/trigger blocks);
* the wiring: ``_synthesize_reply`` 仍用共享块产出一致的 user prompt。
  （``_lightweight_reply`` 已于 2026-10-23 退役：不再有第二条生成路径。）
"""

from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
# 临时目录的唯一入口（见 tests/_tempdirs.py）：``gettempdir()`` 在系统 TEMP 不可写
# 时会静默回落到仓库根，trace 文件就直接写进仓库。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _tempdirs import temp_base  # noqa: E402

os.environ["YINYUE_CONTEXT_TRACE_PATH"] = str(
    temp_base() / f"yinyue_test_user_prompt_trace_{os.getpid()}.jsonl"
)

from core.context import user_prompt  # noqa: E402

NOW_LINE = "当前时间: 2026年01月01日 星期四 12:00:00（中午）UTC+08:00"
MSG_LINE = "本条消息发送时间: 刚刚"


def _freeze_time():
    from core import temporal

    original_now_line, original_msg_line = temporal.now_line, temporal.message_time_line
    temporal.now_line = lambda *args, **kwargs: NOW_LINE
    temporal.message_time_line = lambda *args, **kwargs: MSG_LINE
    return original_now_line, original_msg_line


def _restore_time(originals) -> None:
    from core import temporal

    temporal.now_line, temporal.message_time_line = originals


class _RecordingLLM:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        return "ok"


def test_prefix_unit() -> None:
    originals = _freeze_time()
    try:
        plain = user_prompt.time_affect_prefix(message_time=None, affect_block="")
        assert plain == ["[时间感知]", NOW_LINE, MSG_LINE, ""], plain

        with_affect = user_prompt.time_affect_prefix(
            message_time=None, affect_block="[情绪] 开心"
        )
        assert with_affect == [
            "[时间感知]", NOW_LINE, MSG_LINE, "", "[情绪] 开心", ""
        ], with_affect
    finally:
        _restore_time(originals)
    print("  ok   time/affect prefix matches the legacy literal")


def test_group_blocks_unit() -> None:
    blocks = user_prompt.group_blocks(
        is_group=True, group_recent_context="小A: 在吗", trigger_reason="mentioned"
    )
    assert blocks == [
        "[群聊最近消息]\n小A: 在吗\n",
        "[触发原因: mentioned]\n",
    ], blocks
    assert user_prompt.group_blocks(is_group=False, group_recent_context="x") == []
    assert user_prompt.group_blocks(is_group=True, group_recent_context="") == []
    print("  ok   group/trigger blocks match the legacy literal")


def test_synthesize_reply_wiring() -> None:
    from agents.conversational import ConversationalAgent

    originals = _freeze_time()
    llm = _RecordingLLM()
    fake = SimpleNamespace(
        _is_group=True,
        _affect_block="",
        _group_recent_context="小A: 在吗",
        _reason_text="mentioned",
        _turn_decision=None,
        _message_time=None,
        _turn_intent=None,
        _unknown_answer=False,
        name="鲸鲸",
        _build_system_prompt=lambda is_group: "[SYS]",
        # 2026-10-16: _synthesize_reply 现在会调 _synthesize_band() 取温度/max_tokens
        # （无 volition 时 = 旧参数 0.8/500，与接线前逐字节一致）。
        _synthesize_band=lambda: (0.8, 500),
        llm=llm,
    )
    try:
        reply = asyncio.run(
            ConversationalAgent._synthesize_reply(
                fake, "你好", "最近对话文本", "更早的摘要", ["观察1"]
            )
        )
    finally:
        _restore_time(originals)

    expected_parts = [
        "[时间感知]",
        NOW_LINE,
        MSG_LINE,
        "",
        "[更早对话的摘要]\n更早的摘要\n",
        "最近的对话:\n最近对话文本\n",
        "[群聊最近消息]\n小A: 在吗\n",
        "[触发原因: mentioned]\n",
        '用户现在说: "你好"\n',
        "[你通过工具获得的信息]\n观察1\n",
        "基于以上所有信息，以你的角色身份直接回复用户。"
        "用口语。如果工具返回了具体数据，基于真实数据回答，不要编造。",
        "回复要简洁温暖。",
    ]
    assert reply == "ok"
    assert llm.calls and llm.calls[0]["user"] == "\n".join(expected_parts), llm.calls[0]["user"]
    print("  ok   _synthesize_reply user prompt unchanged (shared blocks wired)")


def test_assemble_from_parts_keeps_separators_and_traces() -> None:
    parts = ["[时间感知]", "当前时间: x", "本条消息发送时间: y", "", "用户说: 你好", ""]
    out = user_prompt.assemble_from_parts(parts, trace=True)
    assert out == "\n".join(parts), out  # legacy join, empty separators preserved

    from core.context.assembler import read_context_trace

    rows = read_context_trace(limit=5)
    assert rows, "trace should have a record"
    latest = rows[0]
    assert latest.get("section") == "user", latest.get("section")
    names = [block["name"] for block in latest["blocks"]]
    assert "(separator)" in names and "用户说" in names, names
    print("  ok   assemble_from_parts keeps separators and writes a user trace")


def main() -> bool:
    tests = [
        test_prefix_unit,
        test_group_blocks_unit,
            test_synthesize_reply_wiring,
        test_assemble_from_parts_keeps_separators_and_traces,
    ]
    failed: list[str] = []
    for test in tests:
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            failed.append(test.__name__)
            print(f"  FAIL {test.__name__}: {exc!r}")
    print(f"user-prompt block checks: {len(tests) - len(failed)}/{len(tests)} passed")
    return not failed


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
