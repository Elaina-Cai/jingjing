# -*- coding: utf-8 -*-
"""WP-08 renderer Phase 2b：触发原因状态的唯一写口（typed carrier）。

Extra 写点删除后，本模块只剩 carrier 更新；届时可并入 turn_decision。
"""
from __future__ import annotations

from typing import Any
from safelog import warn


def set_trigger_reason(
    context: Any,
    *,
    reason: str | None = None,
    trigger_reasons: Any = None,
) -> None:
    """同步更新旧 extra 投影与 typed ``turn_decision``（同值、同时机）。"""

    decision = getattr(context, "turn_decision", None)
    if decision is None:
        return
    try:
        from core.routing.turn_decision import with_reason

        context.turn_decision = with_reason(
            decision, reason=reason, trigger_reasons=trigger_reasons,
        )
    except Exception:  # noqa: BLE001 - 状态同步绝不拖垮回合
        warn("core/routing/reason_state.py:28 Exception", err=repr(__import__("sys").exc_info()[1]))
