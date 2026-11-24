# -*- coding: utf-8 -*-
"""情绪层 / 态度层改成**工具拉取**（2026-10-23 主人拍板）。

为什么不在提示词里：这两层是「此刻的状态」，一条消息一条，边际信息量比代价小得多；
放成工具后模型自己决定「要不要看一眼」，并且能**指名**——「对这个人现在什么态度」。

两件工具都是只读，不写任何状态：
- ``read_emotion``：心境 + 对某人的态度（情绪层，读世界模型的 ``emotion_observed`` 事件流）；
- ``get_stance_cards``：立场卡（态度层的持久那份，按人/按群分）。
"""
from __future__ import annotations

from agents.tools.base import BaseTool, ToolResult


class ReadEmotionTool(BaseTool):
    name = "read_emotion"
    description = (
        "查此刻的心境，以及（给了 person_id 时）对某个人的态度。"
        "情绪层不在提示词里，要依据心情说话就主动取一次；"
        "不传 person_id 只读心境，传了会合并所有会话里对这个人的读数。"
    )
    parameters = {
        "person_id": {
            "type": "string",
            "description": "要查态度的那个人的 wxid（与聊天记录里的 sender_id 同一个）；不传只读心境",
        },
        "person_name": {
            "type": "string",
            "description": "这个人怎么称呼，只用于读数输出里好读；不传就用 person_id",
        },
        "chat_id": {
            "type": "string",
            "description": "只读这个会话的读数；不传=跨会话合并（推荐，心境不该按窗口切开）",
        },
    }

    async def execute(self, **kwargs) -> ToolResult:
        from core.context import emotion

        person_id = str(kwargs.get("person_id") or "").strip()
        who = str(kwargs.get("person_name") or "").strip() or person_id
        chat_id = str(kwargs.get("chat_id") or "").strip()

        mood = emotion.mood_across_chats(chat_id=chat_id)
        lines = [f"心境：{mood.label}（{mood.level:+.2f}，{mood.samples} 条读数）"]
        if person_id:
            attitude = emotion.attitude_across_chats(person_id, chat_id=chat_id)
            lines.append(f"对{who}：{attitude.label}（{attitude.level:+.2f}，{attitude.samples} 条读数）")
            if attitude.samples:
                lines.append(emotion.tone_hint(mood, attitude, who=who))
        if not mood.samples and not person_id:
            lines.append("（没有情绪读数：这一层关着，或者最近没发生过带情绪的事）")
        if not chat_id:
            lines.append("（跨会话合并：只含当前进程里已载入的会话）")
        return ToolResult("\n".join(lines))


class GetStanceCardsTool(BaseTool):
    name = "get_stance_cards"
    description = (
        "查鲸鲸写下的立场卡：对某个群、对某个人说过的态度约定。"
        "态度层的持久那份不在提示词里——要按立场说话（不替人催债、不站队等）就先取一次。"
    )
    parameters = {
        "person_id": {
            "type": "string",
            "description": "只看对这个人的卡（wxid 或已有的 person_key）；不传=本会话生效的群卡+全局卡",
        },
        "chat_id": {
            "type": "string",
            "description": "哪个会话的卡；不传=所有会话里生效的卡",
        },
    }

    def __init__(self, meta_repo=None):
        self.meta_repo = meta_repo

    async def execute(self, **kwargs) -> ToolResult:
        from core.context import stance

        person_id = str(kwargs.get("person_id") or "").strip()
        chat_id = str(kwargs.get("chat_id") or "").strip()
        rows = stance.list_cards(chat_id, active_only=True, meta_repo=self.meta_repo)
        if person_id:
            rows = [
                row for row in rows
                if str(row.get("person_key") or "").strip() in ("", person_id)
            ]
        if not rows:
            return ToolResult("没有生效的立场卡。")
        lines: list[str] = []
        for row in rows[:8]:
            key = str(row.get("person_key") or "").strip()
            where = key or (str(row.get("chat_id") or "").strip() or "全局")
            content = str(row.get("content") or "").strip()
            if content:
                lines.append(f"- 对 {where}：{content}")
        return ToolResult("\n".join(lines) if lines else "没有生效的立场卡。")
