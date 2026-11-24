# -*- coding: utf-8 -*-
"""工具泄漏护栏：回复回合只读视图 + 工具报错/JSON 不进正文。"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.base import AgentContext, BaseAgent, sanitize_tool_markup  # noqa: E402
from agents.planner import PLAN_LOOP_READ_ONLY_TOOLS, PlanExecToolView  # noqa: E402


class _Tool:
    def __init__(self, name: str):
        self.name = name

    def schema(self) -> dict:
        return {"type": "function", "function": {"name": self.name}}


class _StubAgent(BaseAgent):
    def __init__(self):
        pass

    async def run(self, *args, **kwargs):
        return None

    def subscribed_events(self):
        return ()


class _Registry:
    def __init__(self, names):
        self._tools = {n: _Tool(n) for n in names}

    def all_tools(self):
        return dict(self._tools)

    def get(self, name):
        return self._tools.get(name)


def test_sanitize_drops_tool_leak_lines():
    text = "你直接在会话里用工具函数就行。\n我们明天见。"
    cleaned = sanitize_tool_markup(text)
    assert "工具函数" not in cleaned
    assert "我们明天见" in cleaned


def test_sanitize_keeps_normal_reply():
    text = "行，那你早点睡。"
    assert sanitize_tool_markup(text) == text


def test_view_filters_send_tools():
    view = PlanExecToolView(_Registry(["web_search", "send_message", "send_sticker"]))
    assert view.get("send_message") is None
    assert view.get("web_search") is not None
    names = view.all_tools()
    assert "send_message" not in names and "web_search" in names


def test_act_rejects_tool_outside_view():
    agent = _StubAgent()
    agent.name = "t"
    agent._tools_used = []
    context = AgentContext()
    context.tool_view = PlanExecToolView(_Registry(["web_search", "send_message"]))
    result = asyncio.run(agent.act("send_message", {"content": "x"}, context))
    assert result.is_error


class _AgentWithTools(_StubAgent):
    """只给 `_tool_view` 用的最小 agent：`tools` 放类属性，不跑 BaseAgent.__init__。"""

    tools = _Registry(["web_search", "send_message"])


def test_sanitize_drops_leaked_tool_call_json():
    text = '{"action": "send_message", "action_input": {"content": "x"}}\n好，我记着了。'
    cleaned = sanitize_tool_markup(text)
    assert cleaned == "好，我记着了。", cleaned


def test_sanitize_drops_leak_keyword_line():
    text = "我用 action_input 把这个发过去了。\n那就这样。"
    cleaned = sanitize_tool_markup(text)
    assert "action_input" not in cleaned, cleaned
    assert "那就这样。" in cleaned, cleaned


def test_tool_view_prefers_reply_round_view():
    """执行期工具视图优先取 `context.tool_view`（回复回合只读白名单）。

    老写法是 `assert "_tool_view" in base` 加
    `getsource(ConversationalAgent).count("PlanExecToolView(self.tools)") >= 1`：
    断言的是「源码里有没有这行字」，换个装法、换个 agent 类照样绿；
    而 `agents/conversational` 退役后它还把这整份测试拖成 ModuleNotFoundError。
    装没装上，问实例要视图就能回答。
    """
    agent = _AgentWithTools()
    context = AgentContext()
    context.tool_view = PlanExecToolView(_Registry(["web_search", "send_message", "send_sticker"]))
    names = agent._tool_view(context).all_tools()
    assert "send_message" not in names, names
    assert "web_search" in names, names
    assert "send_message" not in PLAN_LOOP_READ_ONLY_TOOLS
    assert "web_search" in PLAN_LOOP_READ_ONLY_TOOLS


def test_tool_view_falls_back_to_own_tools():
    agent = _AgentWithTools()
    names = agent._tool_view(AgentContext()).all_tools()
    assert set(names) == {"web_search", "send_message"}, names


TESTS = [
    test_sanitize_drops_tool_leak_lines,
    test_sanitize_keeps_normal_reply,
    test_sanitize_drops_leaked_tool_call_json,
    test_sanitize_drops_leak_keyword_line,
    test_view_filters_send_tools,
    test_act_rejects_tool_outside_view,
    test_tool_view_prefers_reply_round_view,
    test_tool_view_falls_back_to_own_tools,
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
        print(f"tool leak guard checks: {len(TESTS)}/{len(TESTS)} passed")
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
