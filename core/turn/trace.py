"""每回合每步一行日志：让「这一步跑了吗」可读。

只写日志，**不写账本**——轨迹是「我们自己跑得好不好」，
不是「外部发生了什么」（R1 只记后者）。
"""

from __future__ import annotations

import contextvars
import logging
import time
from dataclasses import dataclass, field

from core.turn.steps import STEP_NAMES, is_step, order_of
from safelog import warn

log = logging.getLogger(__name__)

NOTE_MAX_CHARS = 120


def safe_note(value: object, limit: int = NOTE_MAX_CHARS) -> str:
    """把任意东西压成一行可读文本（不抛异常）。"""
    try:
        text = " ".join(str(value).split())
    except Exception:  # pragma: no cover - 极端对象
        warn("core/turn/trace.py:25 Exception", err=repr(__import__("sys").exc_info()[1]))
        return ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "\u2026"


@dataclass
class TurnTrace:
    """一次回合的九步记录。"""

    chat_id: str = ""
    turn_id: str = ""
    started_at: float = field(default_factory=time.monotonic)
    seen: dict = field(default_factory=dict)

    def step(self, name: str, *, status: str = "ok", note: str = "", ms: int | None = None) -> None:
        if not is_step(name):
            log.warning("turn_step_unknown step=%s", name)
            return
        if ms is None:
            ms = 0
        note = safe_note(note)
        self.seen[name] = {"status": status, "note": note, "ms": int(ms)}
        log.info(
            "turn_step turn=%s step=%s status=%s ms=%d chat_id=%s note=%s",
            self.turn_id or "-",
            name,
            status,
            int(ms),
            self.chat_id or "-",
            note or "-",
        )

    def missing(self) -> list[str]:
        """哪几步没跑（按九步顺序）。"""
        return [name for name in STEP_NAMES if name not in self.seen]

    def last_failed(self) -> str:
        """最后一个非 ok 的步骤名（没有则空串）。"""
        worst = ""
        for name in STEP_NAMES:
            item = self.seen.get(name)
            if item and item.get("status") not in ("ok",):
                worst = name
        return worst


_CURRENT: contextvars.ContextVar["TurnTrace | None"] = contextvars.ContextVar("turn_trace", default=None)


def begin(*, chat_id: str = "", turn_id: str = "") -> TurnTrace:
    """开一条轨迹并设为当前。"""
    trace = TurnTrace(chat_id=str(chat_id or ""), turn_id=str(turn_id or ""))
    _CURRENT.set(trace)
    return trace


def current() -> "TurnTrace | None":
    """当前轨迹（没有则 None）。"""
    return _CURRENT.get()


def step(name: str, *, status: str = "ok", note: str = "", ms: int | None = None) -> None:
    """打一个点（没有轨迹时静默）。"""
    trace = _CURRENT.get()
    if trace is not None:
        trace.step(name, status=status, note=note, ms=ms)


def end() -> "TurnTrace | None":
    """结束并清空当前轨迹。"""
    trace = _CURRENT.get()
    _CURRENT.set(None)
    return trace


def order_index(name: str) -> int:
    """给测试用：步骤序号。"""
    return order_of(name)
