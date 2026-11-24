# -*- coding: utf-8 -*-
"""承诺的展示：目标优先，账本只补旧行。

账本仍是流水与审计，但「我还欠着什么」以目标投影为准：
- 已经映射成目标的承诺，从目标渲染；
- 还没映射的旧账本行，从账本渲染，并在下一次 `promise` 写入时自然收敛。
两处用同一个 `exclude_ids` 排除，不会重复出现。
"""
from __future__ import annotations

from typing import Any

import structlog

from . import ledger
from core.context.goal_source import goal_source_state, source_label
from core.context.promise_goal import ensure_promise_goal, promise_goal_ids
from core.context.situation import open_intentions
from core.execution.data_channels import get_world

log = structlog.get_logger(__name__)

_HEADER = "[我还欠着的]（我自己答应过的，还没销账）"


def render_block(chat_id: Any, *, scope: Any = None, limit: int | None = None) -> str:
    chat = str(chat_id or "").strip()
    if not chat:
        return ""

    if scope is None:
        from loop.ports import current_read_scope
        from core.context.cognitive_commit import Scope
        active = current_read_scope()
        if active is not None and active.chat_id == chat:
            scope = Scope(active.platform, active.account_id, chat, "group" if active.is_group else "private", chat if active.is_group else "")
    model = get_world(chat, scope=scope)
    goals = []
    for item in open_intentions(model, kind="promise"):
        state = goal_source_state(item, scope or model.scope)
        if state != "dead":
            goals.append((item, state))
    mapped = promise_goal_ids(chat, scope=scope)
    exclude = {key.split(":", 1)[1] for key in mapped if key.startswith("promise:")}
    legacy = ledger.render_promises(limit=limit, exclude_ids=exclude, chat_id=chat, scope=scope or model.scope)

    lines: list[str] = []
    if goals:
        lines.append(_HEADER)
        visible = goals[:limit] if limit else goals
        for item, state in visible:
            stamp = (item.opened_at or "")[5:16]
            what = item.desired_outcome or item.evidence or item.key
            label = source_label(state)
            lines.append(f"- {stamp}｜{what}{('｜' + label) if label else ''}")
    if legacy:
        body = legacy.splitlines()[1:]
        if not lines:
            lines.append(_HEADER)
        lines.extend(body)
    return "\n".join(lines)


def import_legacy(limit: int = 200) -> int:
    """把还没映射的旧账本承诺补成目标（启动时跑；同一条幂等）。

    旧行没有会话坐标信息可用时只能跳过——宁可不建目标，也不把承诺挂错会话。
    """
    rows = ledger.open_promises()
    if limit:
        rows = rows[: max(0, int(limit))]
    imported = 0
    for row in rows:
        chat = str(row.get("chat_id") or row.get("target") or "").strip()
        promise_id = row.get("id")
        if not chat or not promise_id:
            continue
        try:
            key = ensure_promise_goal(
                chat,
                promise_id=promise_id,
                what=str(row.get("what") or ""),
                who=str(row.get("who") or ""),
                due=str(row.get("due") or ""),
                at=str(row.get("at") or ""),
                scope=row.get("scope"),
            )
        except Exception as exc:  # noqa: BLE001 - 单条失败不影响其余
            log.warning(
                "promise_legacy_import_failed",
                promise=str(promise_id)[:32],
                error=str(exc)[:120],
            )
            continue
        if key:
            imported += 1
    return imported
