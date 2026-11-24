"""路由决策台账（2026-10-23）：给 perceive 对账补上"回/不回/等"这条边。

为什么需要独立台账：``logs/decision_trace.jsonl`` 里只有 addressee_* / intent_detail /
context_detail 这些 kind，**没有承载路由决策的行**，也没有能和 perceive 对齐的
trace_id —— 于是 ``scripts/perceive_reconcile.py`` 的 route 一致率永远是 0/0。

为什么不用 trace_id 对齐：路由决策发生在回合建立**之前**（判成不回就没有回合、
也就没有 trace_id）。所以这里落 ``chat_id + 秒级时间 + kind``，对账时按
「同一会话、最近一次、容差内」配对 —— 宁可少配，也不硬凑。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from safelog import warn
from core.infra.envflags import text

TRACE_ENV = "YINYUE_ROUTE_TRACE_PATH"
_VALID_KINDS = ("reply_now", "defer", "silent")


def trace_path() -> Path:
    raw = text("YINYUE_ROUTE_TRACE_PATH")
    if raw:
        return Path(raw)
    return Path(__file__).resolve().parent.parent.parent / "logs" / "route_trace.jsonl"


def record(
    chat_id: str,
    kind: str,
    reason: str = "",
    *,
    event_id: str = "",
    at: str = "",
    path: Path | None = None,
) -> bool:
    """追加一行路由决策（**永不抛**：台账不许影响路由）。"""
    try:
        kind = str(kind or "").strip()
        if kind not in _VALID_KINDS:
            return False
        item: dict[str, Any] = {
            "at": at or datetime.now().isoformat(timespec="milliseconds"),
            "chat_id": str(chat_id or ""),
            "event_id": str(event_id or ""),
            "kind": kind,
            "reason": str(reason or "")[:160],
        }
        target = path or trace_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")
        return True
    except Exception:  # noqa: BLE001 - 审计不许影响业务
        warn("core/routing/route_ledger.py:57 Exception", err=repr(__import__("sys").exc_info()[1]))
        return False