"""短句种子生产者测试：工具成功 -> short_reply_signal。

Run directly:
    python tests/test_short_reply_producers.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agents.planner import (  # noqa: E402
    SHORT_REPLY_SIGNAL_BY_TOOL,
    Planner,
    RetryPolicy,
    SubTask,
    ToolCache,
)
from core.speech.short_reply_seed import SIGNAL_TO_KEY  # noqa: E402


class _ToolResult:
    def __init__(self, output: str = "", error: str = ""):
        self.output = output
        self.error = error
        self.data = None

    @property
    def is_error(self) -> bool:
        return bool(self.error)


class _FakeTool:
    def __init__(self, name: str, result: _ToolResult):
        self.name = name
        self.result = result
        self.ctx = None

    def set_context(self, ctx) -> None:
        self.ctx = ctx

    async def execute(self, **kwargs):
        return self.result


class _FakeTools:
    def __init__(self, tools: list[_FakeTool]):
        self._tools = {tool.name: tool for tool in tools}

    def get(self, name: str):
        return self._tools.get(name)


def _ctx(extra: dict | None = None) -> NS:
    return NS(extra=dict(extra or {}))


def _run_tool(planner: Planner, tools: _FakeTools, tool_name: str,
              *, error: str = "") -> NS:
    ctx = _ctx()
    planner.set_context(ctx)
    task = SubTask(
        id=1,
        description="测试工具",
        tool=tool_name,
        tool_args={},
        retry_policy=RetryPolicy(max_retries=0),
    )
    asyncio.run(planner.execute_task_with_retry(task, tools))
    return ctx


def test_mapping_is_known_and_conservative() -> bool:
    assert SHORT_REPLY_SIGNAL_BY_TOOL
    assert SHORT_REPLY_SIGNAL_BY_TOOL["create_reminder"] == "reminder_recorded"
    assert SHORT_REPLY_SIGNAL_BY_TOOL["web_search"] == "serious_result"
    for signal in SHORT_REPLY_SIGNAL_BY_TOOL.values():
        assert signal in SIGNAL_TO_KEY, signal
    assert "send_sticker" not in SHORT_REPLY_SIGNAL_BY_TOOL
    return True


def test_tool_success_writes_signal_and_priority() -> bool:
    tools = _FakeTools([
        _FakeTool("web_search", _ToolResult("查到了")),
        _FakeTool("complete_todo", _ToolResult("做完了")),
        _FakeTool("create_reminder", _ToolResult("记上了")),
    ])
    planner = Planner(llm=None, tools=tools, cache=ToolCache())

    ctx = _run_tool(planner, tools, "web_search")
    assert ctx.extra.get("short_reply_signal") == "serious_result", ctx.extra

    ctx = _run_tool(planner, tools, "complete_todo")
    assert ctx.extra.get("short_reply_signal") == "task_done", ctx.extra

    ctx = _run_tool(planner, tools, "create_reminder")
    assert ctx.extra.get("short_reply_signal") == "reminder_recorded", ctx.extra

    ctx = _ctx({"short_reply_signal": "serious_result"})
    planner.set_context(ctx)
    task = SubTask(id=1, description="测试工具", tool="complete_todo",
                   tool_args={}, retry_policy=RetryPolicy(max_retries=0))
    asyncio.run(planner.execute_task_with_retry(task, tools))
    assert ctx.extra.get("short_reply_signal") == "task_done", ctx.extra

    ctx = _ctx({"short_reply_signal": "reminder_recorded"})
    planner.set_context(ctx)
    task = SubTask(id=1, description="测试工具", tool="web_search",
                   tool_args={}, retry_policy=RetryPolicy(max_retries=0))
    asyncio.run(planner.execute_task_with_retry(task, tools))
    assert ctx.extra.get("short_reply_signal") == "reminder_recorded", ctx.extra
    return True


def test_tool_error_writes_nothing_and_explicit_wins() -> bool:
    broken = _FakeTools([_FakeTool("web_search", _ToolResult(error="boom"))])
    planner = Planner(llm=None, tools=broken, cache=ToolCache())
    ctx = _run_tool(planner, broken, "web_search")
    assert "short_reply_signal" not in ctx.extra, ctx.extra

    ctx = _ctx({"short_reply_signal": "praised"})
    planner.set_context(ctx)
    task = SubTask(id=1, description="测试", tool="web_search", tool_args={},
                   retry_policy=RetryPolicy(max_retries=0))
    asyncio.run(planner.execute_task_with_retry(task, broken))
    assert ctx.extra.get("short_reply_signal") == "praised", ctx.extra
    return True


def main() -> bool:
    tests = [
        test_mapping_is_known_and_conservative,
        test_tool_success_writes_signal_and_priority,
        test_tool_error_writes_nothing_and_explicit_wins,
    ]
    print("=" * 50)
    print("短句种子生产者测试")
    print("=" * 50)
    failed: list[str] = []
    for test in tests:
        try:
            test()
            print(f"  ok   {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed.append(test.__name__)
            print(f"  FAIL {test.__name__}: {exc!r}")
    print(f"\nshort-reply producer checks: {len(tests) - len(failed)}/{len(tests)} passed")
    return not failed


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
