from __future__ import annotations

"""模型侧的「动手」工具壳（只声明，不发送）。

真机教训（2026-10-21）：仓库里有**两个工具目录**——
``agents.tools.ToolRegistry.create_default``（模型看得见的那 18 个只读工具）
与 ``core.tools.builtin``（执行器吃的 10 个，含 ``send_plan`` / ``gui_*``）。
工具造在后者，模型永远看不见，于是她只能照实说「我没有往群里发消息的口」。

这里补的正是**模型侧那两个壳**：把执行器侧已有的能力包成模型能看见、能调的形状。
壳里不许发送：``send_plan`` 只开一件事并停在「等你批准」，``ui_state`` 只读屏。
"""

import asyncio
from typing import Any

from agents.tools.base import BaseTool, ToolResult
from safelog import warn


def _chat_id(tool: BaseTool) -> str:
    """当前会话 id：优先现场 context，取不到就当没有（不猜）。"""
    ctx = getattr(tool, "ctx", None)
    for attr in ("chat_id", "conversation_id"):
        value = getattr(ctx, attr, "")
        if value:
            return str(value)
    extra = getattr(ctx, "extra", None) or {}
    if isinstance(extra, dict):
        return str(extra.get("chat_id") or "")
    return ""


class SendPlanTool(BaseTool):
    name = "send_plan"
    description = (
        "把「往某个会话说一句话」开成一件事，停在等你主人批准，**不直接发送**。"
        "要替主人往群里/私聊发消息时用它；对方批准后才会真的发出去。"
    )
    parameters = {
        "target": {"type": "string", "description": "会话名，例如「鲸鲸的测试群」或「Elaina」", "required": True},
        "text": {"type": "string", "description": "要说的正文", "required": True},
        "criteria": {
            "type": "string",
            "description": "怎么算这件事办成了（一句判据）；可以不填",
        },
    }

    async def execute(self, **kwargs: Any) -> ToolResult:
        target = str(kwargs.get("target") or "").strip()
        text = str(kwargs.get("text") or "").strip()
        if not target or not text:
            return ToolResult("没开成：目标和正文都得有。")
        chat_id = _chat_id(self)
        if not chat_id:
            return ToolResult("没开成：不知道现在在哪个会话里，我不猜。")
        plan = {
            "goal": f"往「{target}」说一句",
            "steps": [
                {"action": f"send:{target}|{text}", "expect": "对方会话里出现这句话"},
            ],
        }
        try:
            from core.jobs import plan_block

            summary = await asyncio.to_thread(
                plan_block.consume, plan, chat_id=chat_id, trace_id="send_plan_tool"
            )
        except Exception as exc:  # noqa: BLE001 - 失败要说人话，不许静默
            warn("agents/tools/jobs.py:68 Exception", err=repr(exc))
            return ToolResult(f"没开成：{type(exc).__name__}")
        return ToolResult(summary or "开成一件事了，等你批准。")


class UiStateTool(BaseTool):
    name = "ui_state"
    description = "看一眼当前电脑界面（UIA 控件树，只读）。想知道「屏幕上现在是什么」时用它。"
    parameters = {
        "target": {"type": "string", "description": "窗口名或进程名，留空=当前前台窗口"},
    }

    async def execute(self, **kwargs: Any) -> ToolResult:
        target = str(kwargs.get("target") or "").strip()
        try:
            from core.tools import builtin

            result = await asyncio.to_thread(builtin.gui_state_handler, target=target)
        except Exception as exc:  # noqa: BLE001
            warn("agents/tools/jobs.py:86 Exception", err=repr(exc))
            return ToolResult(f"看不到：{type(exc).__name__}")
        text = getattr(result, "text", "") or getattr(result, "content", "") or ""
        return ToolResult(str(text)[:4000] or "没读到内容。")


__all__ = ["SendPlanTool", "UiStateTool"]
