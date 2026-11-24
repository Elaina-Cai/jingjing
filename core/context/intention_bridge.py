# -*- coding: utf-8 -*-
"""提醒 → 世界模型意图的单向桥（Step 12：调度器 tick 的执行入口）。

``reminders`` 仍是**唯一执行权威**；本模块只做登记/查询，不投递：

* 创建提醒且 ``INTENTION_TICK != off`` 时，在来源会话的世界模型里开一条
  ``reminder:<id>@<due_at>``（kind=``remind``）——这是调度 tick 到点执行的对象；
* tick 执行成功/止损后由 ``intention_runner`` 关闭它；
* 扫描器 ``run_reminder_delivery`` 在 ``on`` 档对**仍有 open 意图**的提醒让位，
  保证同一到点时刻只有一条投递路径（off 档从不登记，行为逐字节不变）。

关键设计：**每一条到点意图一个独立 key**（带 due_at）。重复提醒顺延时旧意图关闭、
新意图按下一到点时刻另开一条，不会有"关闭了新意图"的事件顺序歧义。
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from core import features
from safelog import warn

__all__ = [
    "REMINDER_KEY_PREFIX",
    "close_reminder_intention",
    "has_open_reminder_intention",
    "open_reminder_intention_keys",
    "open_reminder_intention",
    "parse_reminder_key",
    "reminder_due_text",
    "reminder_intention_key",
    "reminder_intentions_enabled",
]

REMINDER_KEY_PREFIX = "reminder:"
_KEY_RE = re.compile(r"^reminder:(\d+)@(.*)$")


def reminder_intentions_enabled() -> bool:
    """off（默认）= 一行都不走；shadow/on = 登记（shadow 供 tick 审计）。"""
    return features.mode(features.INTENTION_TICK, features.MODE_OFF) != features.MODE_OFF


def reminder_due_text(value: Any) -> str:
    """到点时刻统一成 ``YYYY-MM-DD HH:MM``（``due_intentions`` 的字典序比较口径）。"""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    return " ".join(str(value or "").split())


def reminder_intention_key(reminder_id: Any, due_at: Any) -> str:
    return f"{REMINDER_KEY_PREFIX}{int(reminder_id)}@{reminder_due_text(due_at)}"


def parse_reminder_key(key: Any) -> tuple[int | None, str]:
    """``reminder:<id>@<due>`` -> ``(id, due)``；不是这个形状返回 ``(None, "")``。"""
    match = _KEY_RE.match(str(key or ""))
    if not match:
        return None, ""
    return int(match.group(1)), match.group(2)


def open_reminder_intention(
    *,
    chat_id: Any,
    reminder_id: Any,
    due_at: Any,
    evidence: str = "",
    at: str = "",
    scope: Any = None,
    source_refs: tuple[str, ...] | list[str] = (),
) -> bool:
    """登记一条到点提醒意图（best-effort：调用方只需 try/except 包住）。

    返回 False = flag off / 缺 chat_id / 缺 id（不登记）；落盘失败不影响登记结果。
    ``scope`` 给了就顺手把会话坐标绑到世界模型上，后续执行侧关闭才有身份可写。
    """
    if not reminder_intentions_enabled():
        return False
    key_id = str(reminder_id if reminder_id is not None else "").strip()
    chat = str(chat_id or "").strip()
    if not key_id or not chat:
        return False
    from core.context.situation import note_evidence, open_intention, project
    from core.execution.data_channels import get_world, save_world

    model = get_world(chat, scope=scope)
    key = reminder_intention_key(reminder_id, due_at)
    refs = tuple(str(ref).strip() for ref in source_refs or () if str(ref).strip())
    current = project(model.events).intentions.get(key)
    if current is None:
        open_intention(
            model,
            key,
            kind="remind",
            at=str(at or ""),
            evidence=str(evidence or ""),
            due_at=reminder_due_text(due_at),
            evidence_refs=refs,
        )
    else:
        for ref in refs:
            if ref not in current.evidence_refs:
                note_evidence(
                    model,
                    key,
                    ref,
                    at=str(at or ""),
                    source="reminder_source",
                )
    save_world(chat, scope=model.scope)
    return True


def open_reminder_intention_keys(chat_id: Any, reminder_id: Any) -> tuple[str, ...]:
    """这条提醒当前所有 open 意图的 key（读不到 = 空：宁可由扫描器兜底投递）。"""
    key_id = str(reminder_id if reminder_id is not None else "").strip()
    chat = str(chat_id or "").strip()
    if not key_id or not chat:
        return ()
    from core.context.situation import open_intentions
    from core.execution.data_channels import get_world

    prefix = f"{REMINDER_KEY_PREFIX}{key_id}@"
    try:
        model = get_world(chat)
    except Exception:  # noqa: BLE001
        warn("core/context/intention_bridge.py:109 Exception", err=repr(__import__("sys").exc_info()[1]))
        return ()
    return tuple(
        intent.key for intent in open_intentions(model) if intent.key.startswith(prefix)
    )


def has_open_reminder_intention(chat_id: Any, reminder_id: Any) -> bool:
    """该会话是否还有这条提醒的 open 意图（调用方先判 flag 档位/兜底窗口）。"""
    return bool(open_reminder_intention_keys(chat_id, reminder_id))

def close_reminder_intention(
    chat_id: Any, reminder_id: Any, *, at: str = "", result: str = "",
    evidence_ref: str = "", scope: Any = None, state: str = "",
) -> bool:
    """权威侧到终局后收口镜像意图（扫描器兜底 / shadow 投递走的这条）。

    * ``on``：有 open 意图时扫描器已让位，本函数只处理「意图在 tick 处理前
      就已经终局」的收尾（如 tick 异常后扫描器兜底投递成功）；
    * ``shadow``：扫描器仍是执行方，投递成功后关闭镜像意图，避免同一个到点
      意图每分钟重复审计、状态永远滞后；
    * ``off``（默认）：一行都不走（也不会有意图存在）。

    有 ``evidence_ref`` 时先写认知库；写不进去才退回文件（防重放），不是双写。
    """
    if not reminder_intentions_enabled():
        return False
    key_id = str(reminder_id if reminder_id is not None else "").strip()
    chat = str(chat_id or "").strip()
    if not key_id or not chat:
        return False
    from core.context.situation import open_intentions
    from core.execution.data_channels import get_world, save_world

    prefix = f"{REMINDER_KEY_PREFIX}{key_id}@"
    terminal_state = str(state or "")
    if not terminal_state:
        terminal_state = (
            "completed"
            if str(result or "").startswith(("delivered", "sent"))
            else "abandoned"
        )
    if terminal_state == "completed" and not evidence_ref:
        return False
    model = get_world(chat, scope=scope)
    keys = [item.key for item in open_intentions(model) if item.key.startswith(prefix)]
    local: list[str] = []
    for key in keys:
        if evidence_ref:
            from core.context.goal_close import commit_close

            if commit_close(
                model, key, at=str(at or ""), result=str(result or ""),
                evidence_ref=evidence_ref, mark=True, state=terminal_state,
            ):
                continue
        local.append(key)
    for key in local:
        from core.context.goal_close import close_local

        close_local(
            model, key, at=str(at or ""), result=str(result or ""),
            evidence_ref=evidence_ref, state=terminal_state,
        )
    if local:
        save_world(chat, scope=model.scope)
    return bool(keys)
