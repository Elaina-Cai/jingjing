"""状态快照与演化（试验 · 写死版）。

两个维度：``pressure``（余量压力）+ ``alertness``（清醒度，第二维度）。
``alertness`` 是**瞬时值**（时钟函数，不平滑）；``pressure`` 才需要累积/平滑。
演化两条（施工单 §3.2）：① 日切重置；② 平滑（避免抖成"神经质"）。
进程级内存态 —— 重启丢状态可接受（试验范围）。

试验：写死版。抽象条件见施工单 §6。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from safelog import warn


@dataclass
class ResourceState:
    pressure: float = 0.0
    alertness: float = 1.0   # 第二维度：清醒度（瞬时值，来自时钟）
    day: str = ""
    updated_at: float = 0.0


#: 进程级单例（写死版；抽象后进 StateStore）
_STATE = ResourceState()

#: 平滑系数（太抖就调到 0.85/0.15）
_KEEP = 0.7
_NEW = 0.3


def snapshot(now: float | None = None) -> ResourceState:
    """读 + 演化，返回当前状态快照。任何异常都不抛（返回零状态）。

    「跨时间连续」（理论条件⑤）就落在这一步：状态不是每轮现算的，
    而是**带记忆**地累积/平滑——所以它会有惯性，不会因为一条消息跳变。
    """
    global _STATE
    try:
        from core.state.drivers import alertness, budget_pressure, today_key

        ts = float(now if now is not None else time.time())
        day = today_key(ts)
        raw = budget_pressure(ts)
        if _STATE.day != day:
            # 日切：新的一天，从当日的原始读数起（不是从 0 硬切，避免假跳变）
            _STATE = ResourceState(
                pressure=raw, alertness=alertness(ts), day=day, updated_at=ts,
            )
            return _STATE
        _STATE.pressure = _KEEP * _STATE.pressure + _NEW * raw
        _STATE.alertness = alertness(ts)     # 瞬时值：直接取，不平滑
        _STATE.updated_at = ts
        return _STATE
    except Exception:  # noqa: BLE001 - 状态坏了 = 零状态（绝不抛）
        warn("core/state/resource.py:55 Exception", err=repr(__import__("sys").exc_info()[1]))
        return ResourceState()
