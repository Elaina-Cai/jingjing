# -*- coding: utf-8 -*-
"""affect → ②世界模型的镜像桥（新架构批 A2）。

把一轮已经算好的 ``AffectContext`` 情绪状态写进世界模型：
``core.affect`` 仍是**唯一计算者**（行为信号→积分/衰减），本模块只搬运，不重算、
不落盘。持久化归⑩数据通道；这里只写内存事件，失败绝不打断回复。

调用点：``agents.conversational._compute_affect``，且只在**读场作用域内**
（``GROUP_SCENE`` 非 off + 群聊 + ``YINYUE_GROUP_SOCIAL_CHATS`` 命中）调用：
off 不写、shadow 只记账（不注入任何 prompt）、on 才随 [局面] 被消费。
"""
from __future__ import annotations

from typing import Any

__all__ = ["mirror_affect_to_world"]


def mirror_affect_to_world(
    *,
    chat_id: Any = "",
    affect: Any,
    who: str = "",
    who_id: str = "",
    at: str = "",
    model: Any = None,
) -> bool:
    """一次 AffectContext → ② ``emotion_observed`` 事件。

    ``model`` 给了就写回**这一个**②（④在自己的 model 句柄上调用）；
    没给才按 ``chat_id`` 去进程内仓储取（只有地址的旧调用方）。
    返回 ``True`` = 已记录；地址/state 缺失返回 ``False``（不创建空模型，也不猜）。
    """
    state = getattr(affect, "state", None)
    if state is None:
        return False
    if model is None:
        key = str(chat_id or "").strip()
        if not key:
            return False
        from core.execution.data_channels import get_world

        model = get_world(key)
    from core.context.situation import note_emotion

    note_emotion(
        model,
        level=getattr(state, "level", 0.0),
        reason=getattr(state, "reason", ""),
        who=who,
        who_id=who_id,
        at=at,
    )
    return True
