"""把三样观察合成「这一屏」，并排成判能读的一小块。

三档（``features.SENSORY``）：

- ``off``（默认）——一行不做，连窗口都不读，提示词逐字不变；
- ``shadow``——只读**窗口标题**与**剪贴板长度**，记一条日志，不截图、不落盘、不进提示词；
- ``on``——窗口 + 剪贴板 + 截图（落 ``data/evidence/senses/``），排成 ``[现在这一屏]``
  写进 ``extra["senses_block"]``，由 ``agents/conversational.py`` 注入。

**为什么要有节流**：一个回合可能连着来好几条消息，每回合都截一张图是浪费磁盘与主线程。
同一进程内 ``MIN_INTERVAL_S`` 秒内复用上一张快照——「这一屏」本来就是"此刻"，20 秒前
的那一屏和此刻的差别远小于多拍十张的成本。
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime
from typing import Any, Callable

from core import features
from core.senses.base import (
    SensesError,
    SensorySnapshot,
    render_snapshot,
)
from core.senses.clipboard import read_clipboard
from core.senses.screen import capture
from core.senses.window import foreground_window
from safelog import warn

log = logging.getLogger(__name__)

#: 同一进程内两张截图的最小间隔（秒）。
MIN_INTERVAL_S = 20.0

_last: tuple[datetime, SensorySnapshot] | None = None


def senses_mode() -> str:
    """三档开关的**唯一读点**。"""
    return str(features.mode(features.SENSORY, features.MODE_OFF) or features.MODE_OFF)


def reset_cache() -> None:
    global _last
    _last = None


def collect(
    *,
    window_fn: Callable[[], Any] | None = None,
    clipboard_fn: Callable[[], str] | None = None,
    grab: Callable[[], Any] | None = None,
    now: datetime | None = None,
    write: bool = True,
) -> SensorySnapshot:
    """读一次。任何一项失败只丢那一项（notes 里留一句），不抛。"""
    when = now or datetime.now()
    notes: list[str] = []
    win = None
    try:
        win = (window_fn or foreground_window)()
    except Exception as exc:  # noqa: BLE001
        warn("core/senses/snapshot.py:64 Exception", err=repr(exc))
        notes.append(f"窗口读不到（{type(exc).__name__}）")
    clip = ""
    try:
        clip = str((clipboard_fn or read_clipboard)() or "")
    except Exception as exc:  # noqa: BLE001
        warn("core/senses/snapshot.py:69 Exception", err=repr(exc))
        notes.append(f"剪贴板读不到（{type(exc).__name__}）")
    shot = None
    try:
        shot = capture(grab=grab, taken_at=when, write=write)
    except SensesError as exc:
        warn("core/senses/snapshot.py:74 SensesError", err=repr(exc))
        notes.append(f"截图失败（{exc.code}）")
    except Exception as exc:  # noqa: BLE001
        warn("core/senses/snapshot.py:76 Exception", err=repr(exc))
        notes.append(f"截图失败（{type(exc).__name__}）")
    snapshot = SensorySnapshot(
        window=win if win is not None else SensorySnapshot().window,
        screen=shot if shot is not None else SensorySnapshot().screen,
        clipboard=clip,
        notes=tuple(notes),
    )
    return snapshot


def _recent(now: datetime) -> SensorySnapshot | None:
    if _last is None:
        return None
    at, snap = _last
    if (now - at).total_seconds() < MIN_INTERVAL_S:
        return snap
    return None


def collect_once(*, now: datetime | None = None, **kwargs: Any) -> SensorySnapshot:
    """带节流的一次读取（``on`` 档用）。"""
    global _last
    when = now or datetime.now()
    cached = _recent(when)
    if cached is not None:
        return replace(cached)
    snap = collect(now=when, **kwargs)
    _last = (when, snap)
    return snap


def build_block(extra: Any, *, now: datetime | None = None, **kwargs: Any) -> str:
    """按档位产出 ``[现在这一屏]``；``off``/``shadow`` 都返回空串。"""
    mode = senses_mode()
    if mode == features.MODE_OFF:
        return ""
    if mode == features.MODE_SHADOW:
        try:
            win = (kwargs.get("window_fn") or foreground_window)()
            clip = str((kwargs.get("clipboard_fn") or read_clipboard)() or "")
            log.info(
                "senses_shadow title=%r exe=%r clip_chars=%d",
                getattr(win, "title", ""),
                getattr(win, "exe", ""),
                len(clip),
            )
        except Exception:  # noqa: BLE001 - 影子档出问题也不许影响回合
            warn("core/senses/snapshot.py:123 Exception", err=repr(__import__("sys").exc_info()[1]))
        return ""
    try:
        snap = collect_once(now=now, **kwargs)
    except Exception as exc:  # noqa: BLE001 - 感官坏了不许拖垮回合
        log.warning("senses_failed error=%s", type(exc).__name__)
        return ""
    block = render_snapshot(snap)
    if block and isinstance(extra, dict):
        extra["senses_block"] = block
    return block
