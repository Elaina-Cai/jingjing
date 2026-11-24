"""统一进程内自停通道：哨兵文件 + 进程内 shutdown flag + 超时强退。

2026-10-17 事故（lead 实测 + faulthandler 抓栈）
------------------------------------------------
写 ``logs/scheduler.stop`` 后 5 分钟调度器没退；抓栈显示主线程卡在
``scheduler/main.py`` 的 ``_run_async(run_sync_pipeline())``（在 concurrent
``Future.result()`` 上等），而 asyncio 线程正跑在
``core/routing/router.py::_prefetch_group_focus`` → ``core/context/threads.py::
prefetch_focus``。主循环里的哨兵检查排在同步轮**之后**，所以「能不能停」完全
取决于那一轮多久跑完——这一次它一个多小时没跑完（``logs/yinyue.log``：
16:26:43Z sync_done → 17:50:19Z 才有下一条）。

设计
----
* **哨兵文件通道**：``logs/scheduler.stop``（外部/launcher 只能写文件，跨权限）。
  规则与接线前逐字一致：``mtime >= 本进程启动时间 - 1`` 才算数（陈旧哨兵不误停）。
* **进程内 flag 通道**：:func:`request_stop`，同进程任何线程可置位。
* **独立 daemon 心跳线程**：按 ``poll_seconds`` 轮询两条通道；命中即记
  ``self_stop_requested``；主循环有 ``grace_seconds`` 时间优雅收尾
  （:func:`mark_graceful_exit`），超时 ``os._exit(0)`` 强退。**强退是刻意的**：
  「主线程阻塞」这个故障形态下优雅停本就不可达，而进程以管理员权限运行、
  外部 ``taskkill`` 会被拒绝访问（``scripts/yinyue_launcher.py`` 的注释同理）。

默认行为：不调 :func:`install` 就没有线程、不读文件——与接线前逐字一致（off 档）。
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import structlog
from safelog import warn

log = structlog.get_logger()

#: 仓库根（``scheduler/self_stop.py`` → ``parents[1]``）。
ROOT = Path(__file__).resolve().parents[1]
#: 主人自停哨兵（launcher 写它，本进程读它）。
SENTINEL_PATH = ROOT / "logs" / "scheduler.stop"
#: 本进程启动时间（哨兵「陈旧不误停」的比较基准）。
PROCESS_STARTED_AT = time.time()
#: 轮询间隔与优雅收尾窗口的默认值。
DEFAULT_POLL_SECONDS = 2.0
DEFAULT_GRACE_SECONDS = 15.0

_lock = threading.RLock()
_stop_event = threading.Event()
_graceful = threading.Event()
_reason = ""
_thread: threading.Thread | None = None
_sentinel_path: Path = SENTINEL_PATH
_started_at = PROCESS_STARTED_AT
_poll_seconds = DEFAULT_POLL_SECONDS
_grace_seconds = DEFAULT_GRACE_SECONDS
_exit_func = os._exit
def sentinel_fresh(path=None, *, started_at=None) -> bool:
    """哨兵在场、且**晚于本进程启动**才算数（陈旧哨兵不误停新进程）。"""
    target = Path(path) if path is not None else _sentinel_path
    base = _started_at if started_at is None else float(started_at)
    try:
        return target.stat().st_mtime >= base - 1
    except OSError:
        pass  # noqa: silent（哨兵文件本来就可能不存在）
        return False


def stop_requested() -> bool:
    """两条通道任一命中（进程内 flag / 新鲜哨兵）。"""
    return bool(_stop_event.is_set() or sentinel_fresh())


def stop_reason() -> str:
    """哪条通道、什么原因（没有请求过 = 空串）。"""
    if _reason:
        return _reason
    if sentinel_fresh():
        return "stop_sentinel"
    return ""


def request_stop(reason: str = "in_process") -> bool:
    """进程内 shutdown flag（幂等）。返回**本次**是否置位。"""
    global _reason
    with _lock:
        if not _stop_event.is_set():
            _stop_event.set()
            _reason = str(reason or "in_process")
            log.info("self_stop_requested", channel="flag", reason=_reason)
            return True
    return False


def mark_graceful_exit() -> None:
    """主循环已经收尾（心跳线程不再强退）。"""
    _graceful.set()


def poll_seconds() -> float:
    """当前轮询间隔（测试/诊断用）。"""
    return float(_poll_seconds)


def grace_seconds() -> float:
    """当前优雅收尾窗口（测试/诊断用）。"""
    return float(_grace_seconds)


def is_installed() -> bool:
    """心跳线程是否在跑（``off`` 档 = False）。"""
    return _thread is not None and _thread.is_alive()


def watcher_thread():
    """心跳线程句柄（没装 = None）。"""
    return _thread

def configure(*, sentinel=None, started_at=None, poll=None, grace=None) -> None:
    """只改参数、不起线程（测试隔离用；生产由 :func:`install` 统一设置）。"""
    global _sentinel_path, _started_at, _poll_seconds, _grace_seconds
    with _lock:
        if sentinel is not None:
            _sentinel_path = Path(sentinel)
        if started_at is not None:
            _started_at = float(started_at)
        if poll is not None:
            _poll_seconds = max(0.05, float(poll))
        if grace is not None:
            _grace_seconds = max(0.0, float(grace))


def install(*, sentinel=None, started_at=None, poll_seconds=None,
            grace_seconds=None, exit_func=None):
    """装心跳线程（幂等：已在跑就直接返回它）。"""
    global _thread, _exit_func
    configure(sentinel=sentinel, started_at=started_at,
              poll=poll_seconds, grace=grace_seconds)
    with _lock:
        if exit_func is not None:
            _exit_func = exit_func
        if _thread is not None and _thread.is_alive():
            return _thread
        _graceful.clear()
        thread = threading.Thread(target=_watch_loop, name="self-stop-watch",
                                  daemon=True)
        _thread = thread
        thread.start()
        log.info("self_stop_watcher_started", sentinel=str(_sentinel_path),
                 poll_seconds=round(float(_poll_seconds), 3),
                 grace_seconds=round(float(_grace_seconds), 3))
        return thread


def reset_for_tests() -> None:
    """停心跳、清状态、恢复默认参数（测试隔离用；生产无人调用）。"""
    global _thread, _reason, _sentinel_path, _started_at
    global _poll_seconds, _grace_seconds, _exit_func
    _graceful.set()
    thread = _thread
    _thread = None
    if thread is not None and thread.is_alive():
        thread.join(timeout=1.0)
    with _lock:
        _stop_event.clear()
        _reason = ""
        _sentinel_path = SENTINEL_PATH
        _started_at = PROCESS_STARTED_AT
        _poll_seconds = DEFAULT_POLL_SECONDS
        _grace_seconds = DEFAULT_GRACE_SECONDS
        _exit_func = os._exit
        _graceful.clear()


def _watch_loop() -> None:
    """心跳：轮询两通道；命中 → 等优雅收尾 → 超时强退。"""
    global _reason
    while True:
        if _graceful.is_set():
            return
        channel = ""
        if _stop_event.is_set():
            channel = "flag"
        elif sentinel_fresh():
            channel = "sentinel"
        if channel:
            with _lock:
                if not _reason:
                    _reason = ("stop_sentinel" if channel == "sentinel"
                               else "in_process")
                reason = _reason
            if channel == "sentinel":
                log.info("self_stop_requested", channel="sentinel", reason=reason,
                         sentinel=str(_sentinel_path))
            log.info("self_stop_detected", channel=channel, reason=reason,
                     grace_seconds=round(float(_grace_seconds), 3))
            if _graceful.wait(float(_grace_seconds)):
                log.info("self_stop_graceful", reason=reason)
                return
            log.warning("self_stop_timeout", reason=reason,
                        grace_seconds=round(float(_grace_seconds), 3))
            log.info("self_stop_force_exit", reason=reason)
            _exit_func(0)
            return
        if _graceful.wait(float(_poll_seconds)):
            return


__all__ = [
    "ROOT", "SENTINEL_PATH", "PROCESS_STARTED_AT",
    "DEFAULT_POLL_SECONDS", "DEFAULT_GRACE_SECONDS",
    "sentinel_fresh", "stop_requested", "stop_reason", "request_stop",
    "mark_graceful_exit", "poll_seconds", "grace_seconds", "is_installed",
    "watcher_thread", "configure", "install", "reset_for_tests",
]
