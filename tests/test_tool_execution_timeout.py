# -*- coding: utf-8 -*-
"""GAP-1: 工具 handler 整体超时（act 派发点）的 given/when/then 回归。

对应 docs/任务清单.md 主线三 GAP-1 的验收：
  * 超时 → **失败回执**（不吞掉、也不把 TimeoutError 抛穿整个回合）；
  * 正常路径字节行为不变（返回同一个 ToolResult 对象、参数原样透传）；
  * 超时同样走 action_log(failed) / cmd_trace(ok=False) 两条失败链路；
  * 阈值不是拍脑袋：见文件末尾的生产数据边界用例。

Run directly:
    python tests/test_tool_execution_timeout.py
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import time
from pathlib import Path
from types import MethodType, SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from _tempdirs import temp_base  # noqa: E402

# 与其它独立用例同一隔离口径：import core.* / agents.* 之前，把库/数据/日志指到临时根。
_ISO = temp_base() / "tool_timeout_iso"
_ISO.mkdir(parents=True, exist_ok=True)
os.environ["META_DB_PATH"] = str(_ISO / "meta.db")
os.environ["PLATFORMS_DIR"] = str(_ISO / "platforms")
os.environ["DATA_DIR"] = str(_ISO / "data")
os.environ["LOG_DIR"] = str(_ISO / "logs")
for _key in ("PLATFORMS_DIR", "DATA_DIR", "LOG_DIR"):
    Path(os.environ[_key]).mkdir(parents=True, exist_ok=True)

from agents.base import BaseAgent  # noqa: E402
from agents.tools import base as tools_base  # noqa: E402
from agents.tools.base import (  # noqa: E402
    BaseTool,
    ToolRegistry,
    ToolResult,
    execute_tool_with_timeout,
)

#: 真实阈值 45s；用例里换成 50ms 才能在断言时限内跑完。
_TEST_LIMIT_S = 0.05


class _SleepyTool(BaseTool):
    """given: 一个会一直睡下去（模拟卡在 DB 锁/文件 IO/死循环）的 handler。"""

    name = "sleepy"
    description = "test-only"
    parameters = {}

    def __init__(self):
        self.cancelled = False
        self.finished = False

    async def execute(self, **kwargs):
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        self.finished = True
        return ToolResult("late")


class _FastTool(BaseTool):
    """given: 一个立即返回的 handler；结果对象保持同一引用。"""

    name = "fast"
    description = "test-only"
    parameters = {}

    def __init__(self):
        self.seen = None
        self.result = ToolResult("ok", data={"k": "v"})

    async def execute(self, **kwargs):
        self.seen = kwargs
        return self.result


def _agent(*tools):
    """act 只用到这几处，用 SimpleNamespace 绕开 BaseAgent.__init__ 的依赖装配。"""
    agent = SimpleNamespace(
        name="timeout_test",
        tools=ToolRegistry(),
        _tools_used=[],
        action_logs=[],
        traces=[],
    )
    for tool in tools:
        agent.tools.register(tool)
    agent._write_action_log = (
        lambda action, action_input, result, duration, context:
        agent.action_logs.append((action, result, duration))
    )
    agent._trace_command_execution = (
        lambda action, result, duration, context, ok:
        agent.traces.append((action, ok, duration))
    )
    # act() 现在走 self._tool_view(context)（回合工具白名单，agents/base.py:305）。
    # SimpleNamespace 没有类，得把方法显式绑到实例上：直接赋裸函数会把 context
    # 当成 self 传进去（TypeError），而漏掉它就是原来的 AttributeError。
    agent._tool_view = MethodType(BaseAgent._tool_view, agent)
    return agent


def _patch_limit(seconds=_TEST_LIMIT_S):
    old = tools_base.TOOL_EXECUTION_TIMEOUT_S
    tools_base.TOOL_EXECUTION_TIMEOUT_S = seconds
    return lambda: setattr(tools_base, "TOOL_EXECUTION_TIMEOUT_S", old)


def test_act_timeout_returns_failure_and_cancels_handler():
    """given 慢 handler / when act 执行 / then 限期内拿到失败回执且 handler 被取消。"""
    restore = _patch_limit()
    try:
        tool = _SleepyTool()
        agent = _agent(tool)
        start = time.monotonic()
        result = asyncio.run(BaseAgent.act(agent, "sleepy", {}, None))
        elapsed = time.monotonic() - start
    finally:
        restore()
    assert isinstance(result, ToolResult)
    assert result.is_error, result
    assert "超时" in result.error and "sleepy" in result.error, result.error
    assert tool.cancelled is True, "handler 必须被 wait_for 取消，不能继续飘着"
    assert tool.finished is False
    assert elapsed < 5, f"超时没有生效，用时 {elapsed:.2f}s"


def test_act_normal_path_unchanged():
    """given 快 handler / when act 执行 / then 同一个 ToolResult、参数原样透传。"""
    tool = _FastTool()
    agent = _agent(tool)
    result = asyncio.run(BaseAgent.act(agent, "fast", {"who": "x", "n": 1}, None))
    assert result is tool.result, "正常路径不得被包装成新对象"
    assert tool.seen == {"who": "x", "n": 1}
    assert result.error == "" and result.output == "ok" and result.data == {"k": "v"}


def test_timeout_writes_failed_action_log_and_trace():
    """given 慢 handler / when 超时 / then action_log 与 cmd_trace 都是失败路径。"""
    restore = _patch_limit()
    try:
        tool = _SleepyTool()
        agent = _agent(tool)
        result = asyncio.run(BaseAgent.act(agent, "sleepy", {}, None))
    finally:
        restore()
    assert len(agent.action_logs) == 1, agent.action_logs
    action, logged, duration = agent.action_logs[0]
    assert action == "sleepy" and logged is result and logged.is_error
    assert 0 < duration < 5000, duration

    assert len(agent.traces) == 1, agent.traces
    traced_action, ok, _ = agent.traces[0]
    assert traced_action == "sleepy" and ok is False


def test_timeout_error_message_names_tool_and_limit():
    """given 慢 handler / when 直接调 helper / then 错误文本可定位工具与期限。"""
    restore = _patch_limit()
    try:
        result = asyncio.run(execute_tool_with_timeout(_SleepyTool()))
    finally:
        restore()
    assert result.is_error
    assert "sleepy" in result.error and "0.05s" in result.error, result.error


#: 2026-10-10 生产 action_log 实测（data/platforms/*/*/account.db，n=417 有耗时记录）：
#: 成功 342 次，最长单次成功 38851ms；全部耗时最长 49704ms，且 >38851ms 的唯一样本
#: 正是那次失败挂起。阈值必须落在两者之间：零误杀实测成功，且能截住实测最坏挂起。
_OBSERVED_MAX_OK_S = 38.851
_OBSERVED_WORST_FAIL_S = 49.704


def test_threshold_has_measured_basis():
    """阈值改值必须同步重推证据：≥ 实测成功上限、< 实测最坏挂起。"""
    limit = tools_base.TOOL_EXECUTION_TIMEOUT_S
    assert limit >= _OBSERVED_MAX_OK_S, (limit, _OBSERVED_MAX_OK_S)
    # 2026-10-31：主人要求取消上限，阈值抬到 600s，不再受「落在空白带内」约束；
    # 只保留「不得低于实测成功上限」这条下界守卫。
    assert limit >= 600.0, limit


#: 生成路径的顶层工具派发点；工具内部互相调用的嵌套点由外层超时取消传播覆盖。
_GENERATION_DISPATCH_FILES = ("agents/base.py", "agents/conversational.py", "agents/planner.py")

#: 只匹配真实赋值语句，放过 docstring 里 "``await tool.execute(...)``" 这类示意文本。
_DIRECT_DISPATCH_RE = re.compile(
    r"^\s*(?:result|fb_result)\s*=\s*await\s+(?:tool|fallback_tool)\.execute\("
)


def test_generation_path_dispatch_is_guarded():
    """守卫：这三处生成路径不得再出现无上限的直连 ``tool.execute``（防新路回流）。"""
    root = Path(__file__).resolve().parent.parent
    base = (root / "agents" / "base.py").read_text(encoding="utf-8")
    assert "execute_tool_with_timeout(" in base, "守卫本体不见了"
    for rel in _GENERATION_DISPATCH_FILES:
        if not (root / rel).exists():   # 2026-10-31：agents/conversational.py 已随重构删除
            continue
        source = (root / rel).read_text(encoding="utf-8")
        # 派发统一走 ``self.act``，守卫在 agents/base.py 那一层；生成路径
        # 只许经 act 派发，不许自己直连 tool.execute。
        assert "self.act(" in source or "execute_tool_with_timeout(" in source, rel
        for line in source.splitlines():
            assert not _DIRECT_DISPATCH_RE.match(line), f"{rel} 有新直连派发点: {line.strip()}"


def main() -> int:
    tests = [
        test_act_timeout_returns_failure_and_cancels_handler,
        test_act_normal_path_unchanged,
        test_timeout_writes_failed_action_log_and_trace,
        test_timeout_error_message_names_tool_and_limit,
        test_threshold_has_measured_basis,
        test_generation_path_dispatch_is_guarded,
    ]
    for test in tests:
        test()
    print(f"ALL PASSED ({len(tests)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
