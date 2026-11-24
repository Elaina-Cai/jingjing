# -*- coding: utf-8 -*-
"""⑩ 只读工具：get_persona（批B Step B3）。

读持久人设（persona）的纯通道口，包成能力池里的一个只读工具；
业务判断（版本编译/scope overrides）不在这里，仍在 core/speech。
"""
from __future__ import annotations

from typing import Any

from agents.tools.base import BaseTool, ToolResult

__all__ = ["GetPersonaTool"]


class GetPersonaTool(BaseTool):
    name = "get_persona"
    description = (
        "读取你自己的持久人设（名字/性格/语气）。当需要确认自己是谁、"
        "该用什么语气说话，或核对设定时调用；不要用它编造人设。"
    )
    parameters: dict = {}

    def __init__(self, repo: Any = None) -> None:
        self._repo = repo

    async def execute(self, **kwargs) -> ToolResult:
        from core.execution.persona_channel import read_persona

        view = read_persona(self._repo)
        if view is None:
            return ToolResult(output="读不到人设", error="persona_unavailable")
        lines = [f"名字：{view.name}"]
        if str(view.traits or "").strip():
            lines.append(f"性格：{view.traits}")
        if str(view.voice_config or "").strip():
            lines.append(f"语气：{view.voice_config}")
        return ToolResult(output="\n".join(lines), data=view.to_dict())
