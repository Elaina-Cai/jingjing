# -*- coding: utf-8 -*-
"""回合世界态（WorldState，B2）：同一份窗口数据，同时供 prompt 与闸门。

它回答的不是「有什么记忆」，而是「这一轮重建出的局面是什么」：窗口原文、局况、
话题线、姿态（可选），以及每块的 hash——「她当时看到什么 / 闸门依据什么」可对账。

为什么要有它：批2b 的 `build_window_room_situation` 与 prompt 的窗口块过去是
**两次独立读取**（各自 load 一遍 terminal_events，切片口径也不同）。两次读之间
出现新消息、或窗口切片不同时，「局况/闸门」和「她看到的原文」就会出自不同样本
——最坏的情况是闸门依据一份比模型看到的更浅的样本去拦。

本模块的纪律：**一次构建只读一次窗口**；窗口块、局况、话题线、姿态都从这一份
rows 派生；每块给出 sha12，写进 `extra["world_state"]`，回放时可核对。

窗口开关关 / 会话不在白名单 / 读取失败 → `source="none"`，由调用方回落到内存
buffer（失败的语义是回落，不是放行）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Sequence
from safelog import warn

DEFAULT_DAYS = 7


def sha12(text: Any) -> str:
    """Stable short fingerprint for audit/trace lines."""
    return hashlib.sha1(str(text or "").encode("utf-8")).hexdigest()[:12]


def hash_rows(chat_id: str, rows: Sequence[Any]) -> str:
    """Fingerprint the exact rows a turn was built from (order matters)."""
    parts = [f"chat={chat_id}"]
    for row in rows or ():
        occurred = (
            getattr(row, "occurred_at", None)
            or getattr(row, "created_at", None)
            or ""
        )
        speaker = (
            getattr(row, "speaker", "")
            or getattr(row, "sender_name", "")
            or ""
        )
        content = str(getattr(row, "content", "") or "")
        is_self = bool(getattr(row, "is_self", False))
        parts.append(f"{occurred}|{'self' if is_self else 'other'}|{speaker}|{content}")
    return sha12("\n".join(parts))


@dataclass
class TurnWorld:
    """One turn's reconstructed world (read-only view, not a store)."""

    chat_id: str = ""
    now: datetime | None = None
    source: str = "none"            # window / none（buffer 回落由调用方决定）
    rows: int = 0
    rows_hash: str = ""
    window_text: str = ""           # 注入用文本（轮次模式＝去掉最近原文段的 digest）
    window_hash: str = ""
    #: task-51 B：这一份窗口里**真的单独渲染成行**的入站事件 event_id
    #: （seq 升序、去重、≤40；聚合的"某天一行"与她自己发的行不计）。
    window_event_ids: tuple[str, ...] = ()
    window_full_text: str = ""      # 现状全量文本（含最近原文段；审计/对账用）
    window_full_hash: str = ""
    turns: list[dict[str, Any]] = field(default_factory=list)  # 轮次通道（原文，整条进出）
    turns_hash: str = ""
    turns_chars: int = 0
    # 本轮读到的原始 rows（窗口对象）；仅供 router 的准入分支做"请求-答复成对"，
    # 不进 as_dict / 不落盘。
    rows_data: list[Any] = field(default_factory=list)
    situation: Any = None           # RoomSituation | None
    room_block: str = ""
    room_hash: str = ""

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe trace payload (no big text: hashes + lengths)."""
        return {
            "chat_id": self.chat_id,
            "now": self.now.isoformat(timespec="seconds") if self.now else "",
            "source": self.source,
            "rows": self.rows,
            "rows_hash": self.rows_hash,
            "window_chars": len(self.window_text),
            "window_hash": self.window_hash,
            "window_full_chars": len(self.window_full_text),
            "window_full_hash": self.window_full_hash,
            "turns_n": len(self.turns),
            "turns_chars": self.turns_chars,
            "turns_hash": self.turns_hash,
            "situation": self.situation is not None,
            "room_chars": len(self.room_block),
            "room_hash": self.room_hash,
        }


def build_turn_world(
    chat_id: str,
    *,
    now: datetime,
    current_room: Any | None = None,
    current_window: Any | None = None,
    pending: Sequence[str] = (),
    want_room_block: bool = False,
    days: int = DEFAULT_DAYS,
    loader: Callable[[], list[Any]] | None = None,
) -> TurnWorld:
    """Load the window once and derive every per-turn block from it.

    ``loader`` is injectable for tests/replay (must return WindowMessage-like
    rows).  The window flags check stays inside ``window_enabled_for`` so a
    disabled window means "fall back", exactly like the old paths.
    """
    chat = str(chat_id or "")
    world = TurnWorld(chat_id=chat, now=now)
    try:
        from core.context.chat_window import window_enabled_for

        if not chat or not window_enabled_for(chat):
            return world
    except Exception:  # noqa: BLE001
        warn("core/context/world_state.py:126 Exception", err=repr(__import__("sys").exc_info()[1]))
        return world

    try:
        if callable(loader):
            rows = list(loader())
        else:
            from core.context.chat_window import load_messages

            rows = list(load_messages(chat, days=int(days)))
    except Exception:  # noqa: BLE001
        warn("core/context/world_state.py:136 Exception", err=repr(__import__("sys").exc_info()[1]))
        return world

    if not rows:
        return world

    world.source = "window"
    world.rows = len(rows)
    world.rows_data = list(rows)
    world.rows_hash = hash_rows(chat, rows)

    try:
        from core.context.chat_window import (
            build_window_parts,
            messages_from_window,
            turns_enabled_for,
        )

        parts = build_window_parts(
            chat, messages=rows, current=current_window, now=now
        )
        world.window_full_text = str(parts.text_full or "")
        world.window_full_hash = sha12(world.window_full_text)
        if turns_enabled_for(chat):
            # 通道分离（2026-09-28）：文本通道只留压缩/更早；最近原文走轮次通道。
            # 轮次不含 current —— 当前消息由 user prompt 携带，避免两边重复。
            world.window_text = str(parts.text_digest or "")
            turn_parts = build_window_parts(chat, messages=rows, current=None, now=now)
            from core.context.answer_links import augment_rows_with_links

            turn_rows = augment_rows_with_links(turn_parts.recent, rows)
            world.turns = messages_from_window(turn_rows)
            world.turns_chars = sum(len(str(m.get("content") or "")) for m in world.turns)
            world.turns_hash = (
                sha12(json.dumps(world.turns, ensure_ascii=False)) if world.turns else ""
            )
        else:
            world.window_text = world.window_full_text
        world.window_hash = sha12(world.window_text)
        # task-51 B：与"渲染成行"同一次结果里取 id（``parts.recent`` 就是渲染用到的行）。
        # 只算单独成行的入站：有 event_id、且不是她自己的；占位/系统行由调用方再滤。
        ids: list[str] = []
        for row in list(getattr(parts, "recent", ()) or ()) + list(
            getattr(locals().get("turn_parts"), "recent", ()) or ()
        ):
            item = row if isinstance(row, dict) else getattr(row, "__dict__", {})
            eid = str(item.get("event_id") or "")
            if not eid or eid in ids:
                continue
            if item.get("is_self") or str(item.get("sender_id") or "") == "self":
                continue
            ids.append(eid)
        world.window_event_ids = tuple(ids)[:40]
    except Exception:  # noqa: BLE001
        warn("core/context/world_state.py:189 Exception", err=repr(__import__("sys").exc_info()[1]))
        world.window_text = ""
        world.window_hash = ""

    try:
        from core.context.room_state import build_window_room_situation

        world.situation = build_window_room_situation(
            chat,
            pending=pending,
            now=now,
            current=current_room,
            loader=lambda: rows,
        )
    except Exception:  # noqa: BLE001
        warn("core/context/world_state.py:203 Exception", err=repr(__import__("sys").exc_info()[1]))
        world.situation = None

    if world.situation is not None:
        if want_room_block:
            world.room_block = str(world.situation.render() or "")
        world.room_hash = sha12(world.room_block)

    return world


__all__ = ["DEFAULT_DAYS", "TurnWorld", "build_turn_world", "hash_rows", "sha12"]
