"""五项设备量具：只记日志与进程内计数，**不写账本正行**。

为什么不进账本（`terminal_events`）：量具是「我们自己跑得好不好」的观测，
不是「外部发生了什么」的事实。混进账本会污染唯一事实源（R1），也没法回放。

五项（与 `docs/decisions/设备与并发.md` §9 同名）：

* ``input_wait_ms`` —— 等手的时间（喂 p90，预算 ≤8s）
* ``preempt_count`` —— 高优先级抢占低优先级的次数（越少越好）
* ``dirty_composer_blocked`` —— 拿到手后发现输入框有残留、停手报错的次数（2026-10-28 起不再清空）
* ``starvation_skips`` —— 饿太久被丢掉的回合（丢要有账）
* ``blocked_capability`` —— 能力自检不过而没动手的次数（按理由分桶）
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from typing import Any, Mapping

log = logging.getLogger(__name__)

METRIC_NAMES: tuple[str, ...] = (
    "input_wait_ms",
    "preempt_count",
    "dirty_composer_blocked",
    "starvation_skips",
    "blocked_capability",
    "lease_stuck",
)

MAX_FIELDS = 8
_MAX_SAMPLES = 1000

_lock = threading.Lock()
_counts: dict[str, int] = defaultdict(int)
_samples: dict[str, list[float]] = defaultdict(list)


def bump(name: str, **fields: Any) -> int:
    """记一次。名字必须在 ``METRIC_NAMES`` 里——打错字就等于这门量具静默为零。

    返回该量具当前的累计次数。字段只进日志（一行），不落盘。
    """
    if name not in METRIC_NAMES:
        raise ValueError(
            f"未知量具 {name!r}：只认 {', '.join(METRIC_NAMES)}"
            "（新增量具先加进 METRIC_NAMES 与设备文档 §9）"
        )
    if len(fields) > MAX_FIELDS:
        raise ValueError(f"量具 {name} 的字段太多（{len(fields)} > {MAX_FIELDS}）")

    value = fields.get("value")
    with _lock:
        _counts[name] += 1
        current = _counts[name]
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            series = _samples[name]
            series.append(float(value))
            if len(series) > _MAX_SAMPLES:
                del series[: len(series) - _MAX_SAMPLES]

    parts = " ".join(f"{k}={v}" for k, v in fields.items())
    log.info("metric %s n=%d%s", name, current, f" {parts}" if parts else "")
    return current


def counts() -> dict[str, int]:
    """当前累计（拷贝，改它不影响内部）。"""
    with _lock:
        return {name: _counts.get(name, 0) for name in METRIC_NAMES}


def p90(name: str) -> float:
    """``value`` 字段的 p90（样本不足返回 0.0）。"""
    with _lock:
        series = sorted(_samples.get(name, []))
    if not series:
        return 0.0
    idx = min(len(series) - 1, int(round(0.9 * (len(series) - 1))))
    return float(series[idx])


def reset() -> None:
    """清空（只给测试用）。"""
    with _lock:
        _counts.clear()
        _samples.clear()


def snapshot() -> Mapping[str, Any]:
    """一行日报所需的全部：``{name: {n, p90}}``。"""
    with _lock:
        names = list(METRIC_NAMES)
    return {name: {"n": counts()[name], "p90": p90(name)} for name in names}
