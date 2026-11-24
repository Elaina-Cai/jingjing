"""每天第一次启动时，往日志里写一行「L0 还差几天」。

为什么需要它：分级放行的判据已经在 `scripts/stage_gate.py` 里了，但**没人会在第 7 天
记得去跑一条脚本**。放行闸成了摆设，不是判据不对，是没人按。所以把「还差几天」做成
开机第一行——它出现在每天的日志开头，不需要谁记日子。

三条纪律：

1. **一天只写一次**：靠 ``data/.daily_note`` 里记的日期去重；同一天重启十次也只写一行。
2. **绝不影响启动**：整个入口包在 ``try/except`` 里，任何异常只记一行日志（连日志失败都不抛）。
3. **只读判断**：除了那一个 marker 文件，不碰库、不碰锁、不发消息。

日期与天数与 ``scripts/l0_glance.py`` 必须一致（``tests/test_startup_note.py`` 里有 parity 断言）。
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Any, Callable
from safelog import warn

log = logging.getLogger(__name__)

DUE_DATE = "2026-10-27"
MIN_DAYS = 7
MARKER = Path("data") / ".daily_note"
GATE_CMD = r".venv_iso\Scripts\python.exe -X utf8 -m scripts.stage_gate"


def days_left(due: str = DUE_DATE, *, today: date | None = None) -> int:
    """离到期还有几天（已过期为负）。"""
    today = date.today() if today is None else today
    return (date.fromisoformat(due) - today).days


def note_text(*, today: date | None = None, due: str = DUE_DATE, min_days: int = MIN_DAYS) -> str:
    """那一行字。三种状态：还没到 / 今天到期 / 已经过期。"""
    left = days_left(due, today=today)
    if left > 0:
        return (
            f"L0 观察期还剩 {left} 天（到期 {due}，共需 {min_days} 天）；"
            f"到期跑 {GATE_CMD} --level L0"
        )
    if left == 0:
        return f"L0 观察期今天到期（{due}）——该看了：跑 {GATE_CMD} --level L0"
    return f"L0 已经过期 {-left} 天（{due}）——别等了，跑 {GATE_CMD} --level L0"


def read_marker(path: Path = MARKER) -> str:
    """上一次写日志的日期；读不到返回空串（读不到 = 该写）。"""
    try:
        return str(path.read_text(encoding="utf-8")).strip()
    except Exception:  # noqa: BLE001
        warn("core/observability/startup.py:54 Exception", err=repr(__import__("sys").exc_info()[1]))
        return ""


def mark(path: Path = MARKER, *, today: date | None = None) -> bool:
    """记下今天写过；失败返回 False（不抛）。"""
    today = date.today() if today is None else today
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(today.isoformat(), encoding="utf-8")
        return True
    except Exception as exc:  # noqa: BLE001
        log.info("daily_note_mark_failed error=%s", type(exc).__name__)
        return False


def daily_note(
    *,
    today: date | None = None,
    marker: Path = MARKER,
    due: str = DUE_DATE,
    min_days: int = MIN_DAYS,
    write: Callable[[str], Any] | None = None,
    log_fn: Callable[[str], Any] | None = None,
) -> str:
    """一天一次的那行字：今天已写过就返回空串（不写日志、不动文件）。"""
    today = date.today() if today is None else today
    stamp = today.isoformat()
    if read_marker(marker) == stamp:
        return ""
    text = note_text(today=today, due=due, min_days=min_days)
    emit = log_fn if log_fn is not None else (lambda line: log.info("daily_note %s", line))
    try:
        emit(text)
    except Exception as exc:  # noqa: BLE001
        log.info("daily_note_log_failed error=%s", type(exc).__name__)
    try:
        if write is not None:
            write(text)
    except Exception as exc:  # noqa: BLE001
        log.info("daily_note_write_failed error=%s", type(exc).__name__)
    mark(marker, today=today)
    return text


def startup_note(**kw: Any) -> str:
    """给 `scheduler/main.py` 用的入口：吞掉一切异常，永不阻断启动。"""
    try:
        return daily_note(**kw)
    except Exception as exc:  # noqa: BLE001
        log.info("daily_note_failed error=%s", type(exc).__name__)
        return ""
