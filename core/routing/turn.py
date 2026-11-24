"""回合入口的唯一轨迹点：**每一步都必须留一行**。

为什么需要它（真机教训，2026-10-21）：仓里有两条回合主干——旧的
``core/routing/turn_pipeline.py::generate_turn`` 与新的
``agents/conversational.py`` agentic 循环。新能力（批准解析、读数注入、第六态回执）
全装在旧路上，而用户消息走的是新路，于是「件开出来了、没人听批准」，日志里
**一条线索都没有**。根因不是某处写错，而是**没有唯一入口、也没有闸查可达性**。

这里先立最便宜的一步：一个回合内可以随时打点的轨迹器。
每一行都是 ``turn_step turn=<id> step=<name> status=<…> note=<…>``，
于是「这一步跑了吗」永远能从日志读出来，不用猜。

纪律：
* 轨迹**只写日志**，不进账本正行（账本记外部事实，轨迹记我们自己跑得好不好）；
* 任何打点失败都不许影响回合（全部吞异常）；
* step 名必须是 ``STEPS`` 里的（写错名字等于制造新的静默）。
"""

from __future__ import annotations

import logging
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any
from safelog import warn

log = logging.getLogger(__name__)

#: 九步回合的步骤名（固定顺序，与 ``docs/重构进度.md`` 的主线一致）。
STEPS: tuple[str, ...] = (
    "ingest",
    "read",
    "judge",
    "record",
    "act",
    "say",
    "receipt",
    "metrics",
    "finish",
)

_CURRENT: ContextVar["TurnTrace | None"] = ContextVar("yinyue_turn_trace", default=None)


@dataclass
class TurnTrace:
    """一次回合的轨迹：可以随 ctx 一路带下去，也可以从 contextvar 取。"""

    chat_id: str = ""
    turn_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    started_at: float = field(default_factory=time.monotonic)
    seen: dict[str, str] = field(default_factory=dict)

    def step(self, name: str, status: str = "ok", note: str = "", ms: int | None = None) -> None:
        """打一个点。名字不在 ``STEPS`` 里也不抛——只记一行警告。"""
        if name not in STEPS:
            log.warning("turn_step_unknown name=%s", name)
        self.seen[name] = status
        spent = ms if ms is not None else int((time.monotonic() - self.started_at) * 1000)
        log.info(
            "turn_step turn=%s step=%s status=%s ms=%d chat_id=%s note=%s",
            self.turn_id,
            name,
            status,
            spent,
            self.chat_id,
            note[:120],
        )

    def missing(self) -> list[str]:
        """还没打点的步骤（排查「哪一步没跑」直接看这个）。"""
        return [name for name in STEPS if name not in self.seen]


def begin(*, chat_id: str = "", turn_id: str = "") -> TurnTrace:
    """开一条轨迹并设为当前（同一回合内任何深度都能取到）。"""
    trace = TurnTrace(chat_id=str(chat_id or ""), turn_id=str(turn_id or "") or uuid.uuid4().hex[:12])
    _CURRENT.set(trace)
    return trace


def current() -> TurnTrace | None:
    return _CURRENT.get()


def step(name: str, status: str = "ok", note: str = "", ms: int | None = None) -> None:
    """在当前轨迹上打点；没有轨迹时**什么也不做**（不给旧路径添负担）。"""
    trace = _CURRENT.get()
    if trace is None:
        return
    try:
        trace.step(name, status=status, note=note, ms=ms)
    except Exception:  # noqa: BLE001 - 打点不许影响回合
        warn("core/routing/turn.py:94 Exception", err=repr(__import__("sys").exc_info()[1]))
        pass  # noqa: silent（有意忽略，见 CONTRACT 第八条）


def safe_note(value: Any, limit: int = 120) -> str:
    """把任意东西压成一小段能进日志的文本。"""
    try:
        text = " ".join(str(value or "").split())
    except Exception:  # noqa: BLE001
        warn("core/routing/turn.py:102 Exception", err=repr(__import__("sys").exc_info()[1]))
        return ""
    return text[:limit]


__all__ = ["STEPS", "TurnTrace", "begin", "current", "step", "safe_note"]
