"""TimingPolicy：所有对话时间窗口的唯一读数源（WP-08 8.3）。

旧 ``core.group_state`` 的窗口函数从这里取数（``core.context.intent`` 那个读者已随 N1 退役）；
``get()`` 每次读 env，不缓存（与 ``core.features`` 同一纪律）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from safelog import warn
from core.infra.envflags import text


def _env_float(name: str, default: float, low: float, high: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except Exception:  # noqa: BLE001 - 读不动回落默认值
        warn("core/timing.py:19 Exception", err=repr(__import__("sys").exc_info()[1]))
        return default
    return max(low, min(high, value))


def _unit_silence() -> float:
    # 旧名 YINYUE_PAT_GRACE_SECONDS 是同一窗口的历史别名；优先新名。
    for name in ("YINYUE_UNIT_SILENCE_SECONDS", "YINYUE_PAT_GRACE_SECONDS"):
        raw = (os.environ.get(name) or "").strip()
        if not raw:
            continue
        try:
            return max(0.05, min(15.0, float(raw)))
        except Exception:  # noqa: BLE001 - 读不动继续（不改变旧行为）
            warn("core/timing.py:32 Exception", err=repr(__import__("sys").exc_info()[1]))
            continue
    return 6.0


def _stale_message() -> float:
    # 旧读法保留：0 = 关闸；读不动回落 1800.0；不设上限钳制（与旧行为一致）。
    raw = text("YINYUE_GROUP_STALE_MESSAGE_SECONDS")
    if not raw:
        return 1800.0
    try:
        return max(0.0, float(raw))
    except Exception:  # noqa: BLE001
        warn("core/timing.py:44 Exception", err=repr(__import__("sys").exc_info()[1]))
        return 1800.0


@dataclass(frozen=True)
class TimingPolicy:
    unit_silence: float = 6.0
    unit_max_wait: float = 30.0
    media_followup: float = 15.0
    deferred_wait: float = 15.0
    observation_window: float = 3.0
    rhythm: float = 6.0
    # WP-08 8.4 收口：原先散在 group_state / room_state / threads 的窗口统一归这里。
    coalesce: float = 4.0
    coalesce_max_wait: float = 12.0
    pat_grace_max_wait: float = 12.0
    stale_message: float = 1800.0
    merge_gap: float = 10.0
    merge_span: float = 30.0
    merge_max_messages: int = 5
    room_recent: float = 300.0
    self_ago: float = 90.0
    qa_gap: float = 60.0
    duplicate_window: float = 120.0

    def __post_init__(self) -> None:
        # rhythm 与 unit_silence 同值同源，不许再独立。
        object.__setattr__(self, "rhythm", self.unit_silence)
        # 硬上限不早于 N。
        if self.unit_max_wait < self.unit_silence:
            object.__setattr__(self, "unit_max_wait", self.unit_silence)
        if self.pat_grace_max_wait < self.unit_silence:
            object.__setattr__(self, "pat_grace_max_wait", self.unit_silence)
        if self.coalesce_max_wait < self.coalesce:
            object.__setattr__(self, "coalesce_max_wait", self.coalesce)


def get() -> TimingPolicy:
    """Read the timing policy from env every call (no cache)."""
    unit_silence = _unit_silence()
    unit_max_wait = _env_float("YINYUE_UNIT_MAX_WAIT_SECONDS", 30.0, 1.0, 600.0)
    coalesce = _env_float("YINYUE_GROUP_COALESCE_SECONDS", 4.0, 0.05, 15.0)
    return TimingPolicy(
        unit_silence=unit_silence,
        unit_max_wait=max(unit_max_wait, unit_silence),
        media_followup=_env_float(
            "YINYUE_UNIT_MEDIA_FOLLOWUP_SECONDS", 15.0, 1.0, 60.0
        ),
        deferred_wait=15.0,
        observation_window=3.0,
        rhythm=unit_silence,
        coalesce=coalesce,
        coalesce_max_wait=max(
            _env_float("YINYUE_GROUP_COALESCE_MAX_WAIT", 12.0, 0.1, 60.0), coalesce
        ),
        pat_grace_max_wait=max(
            _env_float("YINYUE_PAT_GRACE_MAX_WAIT", 12.0, 0.1, 60.0), unit_silence
        ),
        stale_message=_stale_message(),
    )


__all__ = ["TimingPolicy", "get"]
