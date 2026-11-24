from __future__ import annotations

import structlog

from agents.tools.base import BaseTool, ToolResult

log = structlog.get_logger()


class CorrectEmojiDescTool(BaseTool):
    name = "correct_emoji_desc"
    description = "纠正表情包描述。当发现对方发表情包后自动识别的描述不准确时，用此工具修正。"
    parameters = {
        "md5": {
            "type": "string",
            "description": "表情包的MD5值（32位十六进制）",
            "required": True,
        },
        "correct_desc": {
            "type": "string",
            "description": "正确的描述，2-6个中文词，如「尴尬流汗」「傲娇哼哼」",
            "required": True,
        },
    }

    async def execute(self, md5: str, correct_desc: str) -> ToolResult:
        from core.preprocess.emoji_recognition import (
            get_emoji_description,
            update_emoji_description,
        )

        old = get_emoji_description(md5)
        if not old:
            return ToolResult(output="", error=f"未找到MD5为{md5}的表情包缓存")

        if update_emoji_description(md5, correct_desc):
            log.info("emoji_desc_corrected_by_agent", md5=md5, old=old, new=correct_desc)
            return ToolResult(output=f"已修正: 「{old}」→「{correct_desc}」")
        return ToolResult(output="", error="描述格式不正确，需2-20个字符")