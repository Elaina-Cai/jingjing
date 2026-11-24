"""Per-turn LLM usage ledger (calls / tokens / per-agent counts).

为什么需要它：群聊一个回合的 LLM 调用已经不止一次——speak_render（出稿）+
admission judge（准入，on 时每群回合一次）+ vision …
但现有观测只有"逐条 llm_call 日志"和"per-agent 累计"，要回答"这一回合
花了几次调用、多少 token、慢在哪"只能事后 grep。

做法：一个 contextvar 账本——回合开始时 ``begin()``，``llm/client.py`` 每次
成功调用 ``record(agent, tokens)``，回合结束时 ``end()`` 取快照并由调用方落
一条 ``turn_llm_usage`` 日志。contextvar 按 asyncio 任务隔离，后台 task 里
的调用不会串进父回合。

Sink-only：只记账，任何异常都不影响主路径（调用方 try/except 包住）。
"""

from __future__ import annotations

import contextvars
from typing import Any

_ledger: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "yinyue_turn_usage", default=None
)


def begin() -> None:
    """Start a fresh ledger for the current turn (contextvar per task)."""
    _ledger.set({"calls": 0, "tokens": 0, "agents": {}})


def record(agent_name: str, tokens: int) -> None:
    """Count one successful LLM call. No-op when no turn is active."""
    acc = _ledger.get()
    if acc is None:
        return
    acc["calls"] += 1
    acc["tokens"] += int(tokens or 0)
    name = str(agent_name or "unknown")
    acc["agents"][name] = acc["agents"].get(name, 0) + 1


def snapshot() -> dict[str, Any]:
    """Current ledger copy (``{}`` when no turn is active)."""
    acc = _ledger.get()
    if acc is None:
        return {}
    return {
        "calls": acc["calls"],
        "tokens": acc["tokens"],
        "agents": dict(acc["agents"]),
    }


def end() -> dict[str, Any]:
    """Finish the turn: return the final snapshot and clear the ledger."""
    snap = snapshot()
    _ledger.set(None)
    return snap


__all__ = ["begin", "record", "snapshot", "end"]
