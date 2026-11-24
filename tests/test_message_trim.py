"""Context-budget helpers: chat_tools message trimming + memory block noise filter."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
# 临时目录的唯一入口（见 tests/_tempdirs.py）：``gettempdir()`` 在系统 TEMP 不可写
# 时会静默回落到仓库根，trace 文件就直接写进仓库。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _tempdirs import temp_base  # noqa: E402

os.environ.setdefault(
    "YINYUE_CONTEXT_TRACE_PATH",
    str(temp_base() / "yinyue_test_context_trace.jsonl"),
)

from agents.conversational import _trim_tool_messages, format_group_members  # noqa: E402
from core.context.assembler import read_context_trace, trace_bundle  # noqa: E402
from core.context.budget import estimate_tokens  # noqa: E402
from core.context.contracts import ContextBlock, ContextBundle  # noqa: E402
from core.terminal.memory_gateway import MemoryGateway, MemoryHit  # noqa: E402


def _msg(role: str, content: str, **extra):
    base = {"role": role, "content": content}
    base.update(extra)
    return base


def test_trim_keeps_system_user_and_recent_groups() -> None:
    messages = [
        _msg("system", "S" * 100),
        _msg("user", "U" * 100),
    ]
    for i in range(6):
        messages.append(_msg("assistant", "", tool_calls=[{"id": f"c{i}"}]))
        messages.append(_msg("tool", "T" * 9000, tool_call_id=f"c{i}"))
    dropped = _trim_tool_messages(messages, max_chars=20000, keep_groups=2)
    assert dropped > 0
    assert messages[0]["role"] == "system" and messages[1]["role"] == "user"
    groups = [m for m in messages if m.get("tool_calls")]
    assert len(groups) == 2, len(groups)
    kept_ids = {g["tool_calls"][0]["id"] for g in groups}
    for m in messages[2:]:
        if m.get("role") == "tool":
            assert m["tool_call_id"] in kept_ids
    total = sum(len(str(m.get("content") or "")) for m in messages)
    assert total <= 20000 + 9000
    print("  ok   message trimming keeps system/user + recent tool groups")


def test_render_filters_mention_noise_and_dedupes() -> None:
    now = datetime(2026, 9, 22, 15, 0, tzinfo=timezone.utc)
    hits = [
        MemoryHit(memory_id="a", content="@鲸鲸", occurred_at=now, event_type="message"),
        MemoryHit(memory_id="b", content="[表情包:abc]", occurred_at=now, event_type="message"),
        MemoryHit(memory_id="c", content="P6是物联网25-2班的", occurred_at=now, event_type="message"),
        MemoryHit(memory_id="d", content="P6是物联网25-2班的", occurred_at=now, event_type="message"),
        MemoryHit(memory_id="e", content="@鲸鲸 谁在无理取闹", occurred_at=now, event_type="message"),
    ]
    block = MemoryGateway.render(hits, max_chars=1200)
    assert "P6是物联网25-2班的" in block
    assert "谁在无理取闹" in block
    assert block.count("P6是物联网25-2班的") == 1
    assert "[表情包:abc]" not in block
    assert block.count("- [") == 2
    print("  ok   render filters mention-only noise and dedupes")


def test_group_member_formatting_carries_ids() -> None:
    rows = [
        ("P5", "member", "wxid_seed05"),
        ("Elaina", "member", "wxid_seed01"),
        ("P6", "member", ""),
        ("P5", "member", "wxid_seed05"),
    ]
    text = format_group_members(rows, {"P5": "计算机25-4班，前学委"})
    assert "P5（…bt22，计算机25-4班，前学委）" in text
    text_bare = format_group_members(rows)
    assert "P5（…bt22）" in text_bare
    assert "Elaina（…y022）" in text
    assert text.count("P5") == 1
    assert "P6" in text and "P6（" not in text
    print("  ok   group member labels carry wxid tails and dedupe")


def test_trace_records_hash_and_token_estimate() -> None:
    bundle = ContextBundle(
        blocks=(
            ContextBlock(
                name="[时间感知]",
                content="当前时间: 2026年09月22日",
                source="context.time",
                priority=0,
            ),
            ContextBlock(
                name="[基本表达]",
                content="像微信里的真人聊天。",
                source="persona.constant",
                priority=0,
            ),
        )
    )
    trace_bundle(bundle, section="system")
    rows = read_context_trace(limit=3)
    assert rows
    last = rows[0]
    assert last.get("approx_tokens", 0) > 0
    assert all(block.get("hash") for block in last["blocks"])
    assert all(block.get("approx_tokens") for block in last["blocks"])
    assert estimate_tokens("测试一下") > 0
    print("  ok   context_trace stores block hash + token estimate")


def test_trace_records_world_state() -> None:
    from types import SimpleNamespace

    bundle = ContextBundle(
        blocks=(
            ContextBlock(
                name="[局况]",
                content="别人在互答，没有提到你。",
                source="room.situation",
                priority=40,
            ),
        )
    )
    ctx = SimpleNamespace(extra={
        "chat_id": "worldtrace@chatroom",
        "world_state": {
            "source": "window",
            "rows_hash": "abc123abc123",
            "window_hash": "win000000000",
            "room_hash": "room00000000",
        },
    })
    trace_bundle(bundle, section="system", context=ctx)
    rows = read_context_trace(chat_id="worldtrace@chatroom", limit=3)
    assert rows, "trace record missing"
    world = rows[0].get("world") or {}
    assert world.get("rows_hash") == "abc123abc123", rows[0]
    assert world.get("window_hash") == "win000000000", rows[0]
    print("  ok   context_trace carries world_state hashes")


def main() -> int:
    tests = [
        test_trim_keeps_system_user_and_recent_groups,
        test_group_member_formatting_carries_ids,
        test_trace_records_hash_and_token_estimate,
        test_trace_records_world_state,
        test_render_filters_mention_noise_and_dedupes,
    ]
    failed = 0
    for test in tests:
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback

            print(f"FAIL {test.__name__}: {exc!r}")
            traceback.print_exc()
    print(f"context budget checks: {len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
