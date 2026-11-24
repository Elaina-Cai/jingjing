"""短句种子出稿层接线单测（flag / 范围 / signal / 无嘿嘿）。

Run directly:
    python tests/test_short_reply_wiring.py
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agents.conversational import ConversationalAgent  # noqa: E402

FLAG = "YINYUE_FEATURE_SHORT_REPLY_SEED"


class _FakeLLM:
    async def chat(self, **kwargs):
        return ""


@contextmanager
def _env(key: str, value: str | None):
    saved = os.environ.get(key)
    try:
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
        yield
    finally:
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved


def _snapshot(*, is_owner: bool = True, is_group: bool = False,
              inbound: str = "", trigger_reason: str = "") -> NS:
    target = NS(is_group=is_group)
    turn = NS(
        target=target,
        scene="group" if is_group else "private",
        is_owner=is_owner,
        inbound_text=inbound,
        trigger_reason=trigger_reason,
    )
    persona = NS(name="鲸鲸", aliases=("鲸鲸",), voice={})
    return NS(turn=turn, persona=persona)


def _context(*, is_owner: bool = True, is_group: bool = False,
             extra: dict | None = None) -> NS:
    principal = NS(is_group=is_group, is_owner=is_owner, display_name="Elaina")
    return NS(principal=principal, extra=dict(extra or {}))


def _agent(snapshot: NS | None, context: NS | None, inbound: str = "") -> ConversationalAgent:
    agent = ConversationalAgent.__new__(ConversationalAgent)
    agent._llm = _FakeLLM()
    agent._speech_snapshot = snapshot
    agent._last_context = context
    agent._inbound_content = inbound
    agent._trigger_reason = ""
    return agent


def test_flag_default_on_and_rollback() -> bool:
    with _env(FLAG, None):
        assert ConversationalAgent._short_reply_seed_on() is True
    with _env(FLAG, "1"):
        assert ConversationalAgent._short_reply_seed_on() is True
    with _env(FLAG, "0"):
        assert ConversationalAgent._short_reply_seed_on() is False
    return True


def test_block_all_scopes_when_on() -> bool:
    agent = _agent(_snapshot(inbound="嘿，鲸鲸"), _context(), "嘿，鲸鲸")
    with _env(FLAG, "0"):
        assert agent._render_short_reply_seed_block() == ""
    with _env(FLAG, "1"):
        block = agent._render_short_reply_seed_block()
        assert "【短句种子" in block
        assert "咋了" in block
        assert "嗯？" in block
    group = _agent(_snapshot(is_group=True, inbound="嘿，鲸鲸"), _context(is_group=True), "嘿，鲸鲸")
    with _env(FLAG, "1"):
        group_text = group._render_short_reply_seed_block()
        assert "【短句种子" in group_text, group_text
        assert "咋了" in group_text, group_text
    group_other = _agent(
        _snapshot(is_group=True, is_owner=False, inbound="嘿，鲸鲸"),
        _context(is_group=True, is_owner=False),
        "嘿，鲸鲸",
    )
    with _env(FLAG, "1"):
        assert "【短句种子" in group_other._render_short_reply_seed_block()
    other = _agent(_snapshot(is_owner=False, inbound="嘿，鲸鲸"), _context(is_owner=False), "嘿，鲸鲸")
    with _env(FLAG, "1"):
        other_text = other._render_short_reply_seed_block()
        assert "【短句种子" in other_text, other_text
        assert "咋了" in other_text, other_text
    return True


def test_block_selects_signal_and_keeps_awkward_clean() -> bool:
    with _env(FLAG, "1"):
        waiting = _agent(
            _snapshot(inbound="你是不是在等我"),
            _context(),
            "你是不是在等我",
        )
        block = waiting._render_short_reply_seed_block()
        assert "没有" in block and "才没有" in block
        assert "嘿嘿" not in block.split("这轮不要用", 1)[0]

        explicit = _agent(
            _snapshot(inbound="随便"),
            _context(extra={"short_reply_signal": "praised"}),
            "随便",
        )
        praised = explicit._render_short_reply_seed_block()
        assert "少来" in praised

        serious = _agent(
            _snapshot(inbound="随便"),
            _context(extra={"short_reply_signal": "serious_result"}),
            "随便",
        )
        assert "查到了，等我发你。" in serious._render_short_reply_seed_block()
    return True


def test_speak_render_system_gets_block_only_when_on() -> bool:
    """2026-10-23：`_speak_render_system` 已随 Speak Contract 退役。

    短句种子块本身仍在（`_render_short_reply_seed_block`），但它不再往一次
    「只有草稿、没有上下文」的重写调用里塞——那种重写的产物她自己也看不见。
    """
    agent = _agent(_snapshot(inbound="在吗"), _context(), "在吗")
    assert not hasattr(agent, "_speak_render_system"), "说人话重写入口回流了"
    text = agent._render_short_reply_seed_block()
    assert "【短句种子" in text, text
    return True


def test_low_battery_from_quiet_affect_only() -> bool:
    with _env(FLAG, "1"):
        presence = _agent(
            _snapshot(inbound="在吗"),
            _context(extra={"affect": {"band": "quiet"}}),
            "在吗",
        )
        text = presence._render_short_reply_seed_block()
        assert "在。累" in text, text

        called = _agent(
            _snapshot(inbound="嘿，鲸鲸"),
            _context(extra={"affect": {"band": "quiet"}}),
            "嘿，鲸鲸",
        )
        assert "困了" in called._render_short_reply_seed_block()

        warm = _agent(
            _snapshot(inbound="在吗"),
            _context(extra={"affect": {"band": "warm"}}),
            "在吗",
        )
        warm_text = warm._render_short_reply_seed_block()
        assert "在。累" not in warm_text, warm_text
        assert "咋了" in warm_text, warm_text

        explicit = _agent(
            _snapshot(inbound="在吗"),
            _context(extra={"affect": {"band": "quiet"}, "short_reply_signal": "praised"}),
            "在吗",
        )
        praised = explicit._render_short_reply_seed_block()
        assert "少来" in praised
        assert "在。累" not in praised
    return True


def main() -> bool:
    tests = [
        test_flag_default_on_and_rollback,
        test_block_all_scopes_when_on,
        test_block_selects_signal_and_keeps_awkward_clean,
        test_speak_render_system_gets_block_only_when_on,
        test_low_battery_from_quiet_affect_only,
    ]
    print("=" * 50)
    print("短句种子出稿层接线测试")
    print("=" * 50)
    failed: list[str] = []
    for test in tests:
        try:
            test()
            print(f"  ok   {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed.append(test.__name__)
            print(f"  FAIL {test.__name__}: {exc!r}")
    print(f"\nshort-reply wiring checks: {len(tests) - len(failed)}/{len(tests)} passed")
    return not failed


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
