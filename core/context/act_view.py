# -*- coding: utf-8 -*-
"""世界窗口（Step 11，原称「行动视图」）：把世界模型投影渲染成生成侧可读的
``[世界窗口]`` 块。

改名理由（2026-10-23）：它的块头原来也叫 ``[读场]``，而 ``[读场]`` 今天归
``core/context/scene_read.py``（N1 的 semantic_reads，已随 N1 退役删除）；两个不同事实源
共用一个块头 ⇒ ``context_trace`` 与预算表都分不清谁是谁。

只读中枢，不写状态；off/shadow 不追加（逐字节退回现状）。
"""
from __future__ import annotations

from typing import Any, Mapping

from core.context.goal_source import goal_source_state, source_label
from core.context.situation import (
    WorldModel,
    open_intentions,
    position_lines,
    project,
    recent_messages,
)
from safelog import warn

ACT_VIEW_HEADER = "[世界窗口]"

__all__ = ["ACT_VIEW_HEADER", "act_view_enabled", "render_act_view"]


def act_view_enabled(chat_id: Any, is_group: Any) -> bool:
    if not is_group:
        return False
    try:
        from core.context.scene_read import scene_mode, social_chat_scope

        if scene_mode() != "on":
            return False
        scope = social_chat_scope()
        if scope and str(chat_id or "") not in scope:
            return False
        return True
    except Exception:  # noqa: BLE001
        warn("core/context/act_view.py:40 Exception", err=repr(__import__("sys").exc_info()[1]))
        return False


def render_act_view(
    model: WorldModel | None,
    *,
    max_lines: int = 8,
    verify_evidence: bool = False,
) -> str:
    if model is None:
        return ""
    view = project(model.events)
    lines: list[str] = []
    from core.context.cognitive_commit import Scope
    from core.memory.beliefs import belief_evidence_quality

    raw_scope = dict(getattr(model, "scope", None) or {})
    scope = Scope(
        str(raw_scope.get("platform") or ""),
        str(raw_scope.get("account_id") or ""),
        str(raw_scope.get("chat_id") or ""),
    )
    for belief in view.beliefs.values():
        if belief.status in ("active", "pending"):
            quality = belief_evidence_quality(
                belief,
                scope,
                verify=bool(verify_evidence and scope.complete),
            )
            if verify_evidence and not scope.complete:
                quality = "unknown"
            if quality == "dead":
                continue
            who = f"（{belief.who} 说的）" if belief.who else ""
            state = " [依据未核实]" if quality == "unknown" else ""
            lines.append(f"{belief.key} = {belief.value}{who}{state}")
    for line in position_lines(model):
        lines.append(line.lstrip("- "))
    for intent in open_intentions(model):
        state = goal_source_state(intent, model.scope) if verify_evidence else ""
        if state == "dead":
            continue
        label = intent.next_action or intent.desired_outcome or intent.key
        suffix = source_label(state)
        lines.append(f"待办：{intent.kind} {label}{(' ' + suffix) if suffix else ''}")
    # Step 17：最近几轮的「谁 → 对谁：说了什么」有**保底预算**（最多 3 行），
    # 不被事实行挤掉——"谁说的"正是 [读场] 要防的那类混人。
    message_lines = [f"最近：{line}" for line in recent_messages(model, limit=3)]
    budget = max(0, max_lines - len(message_lines))
    ordered = lines[:budget] + message_lines
    seen = set()
    out = []
    for line in ordered:
        if line and line not in seen:
            seen.add(line)
            out.append("- " + line)
    if not out:
        return ""
    return ACT_VIEW_HEADER + "\n" + "\n".join(out[:max_lines])
