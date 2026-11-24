"""统一的说话人解析（L3 收敛）。

历史上四处各写一份 ``_speaker``（``core/context/chat_window`` /
``core/analysis/projectors`` / ``core/terminal/memory_gateway`` /
``core/terminal/memory_projection``），bot 事件的判断集合和优先级都不一致，
导致"谁说了什么"出现漂移（张冠李戴）。这里收敛成一个权威实现：

- ``is_bot_event``：事件是否由 terminal（鲸鲸）自己发出。
- ``resolve_speaker``：返回说话人的稳定标识（bot 用 ``BOT_NAME``，否则
  sender_name → display_name → actor 的 wxid）。

L1 完成后 reply/thought 的 actor 会写回 bot 自己，届时这些事件可直接读
actor；在那之前仍保留 event_type 判断以维持行为不变（可回滚）。
"""

from __future__ import annotations

from typing import Any

BOT_NAME = "鲸鲸"

# bot 自己发出的事件类型（回复 / 思考 / 动作 / 工具调用 / 情绪）。
_BOT_EVENT_TYPES = frozenset({"reply", "thought", "action", "tool_call", "emotion"})


def is_bot_event(event_type: Any, metadata: dict[str, Any] | None = None) -> bool:
    """事件是否由 terminal 自己发出。

    统一了历史上四处不一致的判断：只认 REPLY 的、认 REPLY/ACTION/TOOL_CALL 的、
    认 REPLY/ACTION/TOOL_CALL/EMOTION 的、以及认 reply/thought + is_self 的。
    这里取并集 + is_self 标记，作为"bot 发出"的唯一口径。
    """
    if str(event_type or "") in _BOT_EVENT_TYPES:
        return True
    return bool((metadata or {}).get("is_self"))


def resolve_speaker(
    event_type: Any,
    actor_person_id: str = "",
    *,
    metadata: dict[str, Any] | None = None,
    names: dict[str, str] | None = None,
    actor_account_id: str = "",
) -> str:
    """返回说话人标识。

    bot 事件返回 ``BOT_NAME``；否则优先 sender_name / display_name，再回退到
    actor 的 wxid。``names`` 是可选 wxid→昵称 映射：提供时返回昵称
    （chat_window 显示用），否则返回原始 wxid（投影 / 召回用）。
    """
    if is_bot_event(event_type, metadata):
        return BOT_NAME
    meta = metadata or {}
    name = str(meta.get("sender_name") or "").strip()
    if not name:
        name = str(meta.get("display_name") or "").strip()
    if not name:
        principal = meta.get("principal")
        if isinstance(principal, dict):
            name = str(principal.get("display_name") or "").strip()
    if name:
        return name
    # task-49：actor 归一后 ``actor_person_id`` 是 person id、``actor_account_id``
    # 才是平台号；``names`` 映射是 wxid→昵称，所以先试账号号，再退回 person id。
    raw = str(actor_account_id or actor_person_id or "").strip()
    if raw and names:
        mapped = names.get(raw)
        if mapped:
            return mapped
    return raw


def _row_get(row: Any, name: str, default: Any = None) -> Any:
    """读一行消息/窗口行字段：dict 与对象都支持。"""
    if isinstance(row, dict):
        return row.get(name, default)
    return getattr(row, name, default)


def resolve_speaker_from_row(
    row: Any,
    *,
    names: dict[str, str] | None = None,
    default: str = "?",
) -> str:
    """从一行消息/窗口行解析说话人（WP-03：唯一 owner）。

    输入优先级与历史三处 ``_speaker`` 保持一致，但判定只在这里：

    1) 行上有 ``sender_name`` / ``display_name`` / ``sender_id``（或 ``is_self``）
       → 交给 :func:`resolve_speaker`：bot 事件返回 ``BOT_NAME``，否则
       name → display_name → actor；
    2) 只有渲染好的 ``speaker`` 列（``WindowMessage``）→ 直接返回；
    3) 全空 → ``default``（room_state 传 ``"?"``，threads 传 ``""``）。
    """
    name = str(_row_get(row, "sender_name") or "").strip()
    display = str(_row_get(row, "display_name") or "").strip()
    actor = str(
        _row_get(row, "sender_id")
        or _row_get(row, "actor_account_id")
        or _row_get(row, "actor_person_id")
        or ""
    ).strip()
    is_self = bool(_row_get(row, "is_self"))
    event_type = _row_get(row, "event_type") or ""
    if name or display or actor or is_self:
        resolved = resolve_speaker(
            event_type,
            actor,
            metadata={"is_self": is_self, "sender_name": name, "display_name": display},
            names=names,
        )
        if resolved:
            return resolved
    speaker = str(_row_get(row, "speaker") or "").strip()
    return speaker or default
