# -*- coding: utf-8 -*-
"""位置投影渲染（Step 7）。

位置是中枢 Position/Desires 投影：硬约束来自主人设定，关系证据来自世界事件。
本模块只渲染、只读 flag/作用域，不新建第二套位置存储，也不调用 LLM。
off -> 空串（不入列，逐字节退回现状）。
"""
from __future__ import annotations

from typing import Any, Sequence
from safelog import warn

POSITION_HEADER = "[位置]"

#: 硬约束：主人设定，不可被群聊观察覆盖。
HARD_LINES: tuple[str, ...] = (
    "- 你是 Elaina 的人；在群里可以玩，但不是谁都能使唤",
    "- 没对你说、只是提到你 -> 不接",
    "- 谁在维护你，要接得住",
    "- 公开场合不拆主人的台、不站到他对面（红线）",
    "- 怎么说仍由你",
)

__all__ = ["HARD_LINES", "POSITION_HEADER", "position_enabled", "render_position_block"]


def position_enabled(chat_id: Any, is_group: Any) -> bool:
    """仅 GROUP_SCENE=on + 群聊 + 作用域内启用。"""
    if not is_group:
        return False
    try:
        from core import features

        if features.mode(features.GROUP_SCENE, features.MODE_OFF) != features.MODE_ON:
            return False
        from core.context.scene_read import social_chat_scope

        scope = social_chat_scope()
        if scope and str(chat_id or "") not in scope:
            return False
        return True
    except Exception:  # noqa: BLE001 - flag 读不到按关闭，不阻断装配
        warn("core/context/position.py:41 Exception", err=repr(__import__("sys").exc_info()[1]))
        return False


def render_position_block(
    chat_id: Any,
    is_group: Any,
    *,
    extra_lines: Sequence[str] = (),
) -> str:
    """渲染 [位置] 块；未启用返回空串（调用方不入列）。"""
    if not position_enabled(chat_id, is_group):
        return ""
    lines = list(HARD_LINES)
    lines.extend(str(item) for item in extra_lines if str(item).strip())
    return POSITION_HEADER + "\n" + "\n".join(lines)
