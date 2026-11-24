"""scheduler/self_stop.py 的独立用例：主循环被卡住时也必须能停。

跑法：.venv_it/Scripts/python.exe -X utf8 tests/test_self_stop.py
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from _tempdirs import make_db_dir  # noqa: E402

from scheduler import self_stop

#: 隔离临时目录（唯一入口 ``tests/_tempdirs.py``，见该模块 docstring）。
_TMP_DIRS: list[Path] = []


def _tmp_sentinel() -> Path:
    base = make_db_dir("self_stop_test_")
    _TMP_DIRS.append(base)
    path = base / "logs" / "scheduler.stop"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _cleanup_tmp_dirs() -> None:
    for base in _TMP_DIRS:
        shutil.rmtree(base, ignore_errors=True)
    _TMP_DIRS.clear()


def _recorder():
    calls: list[int] = []

    def _exit(code: int = 0) -> None:
        calls.append(code)

    return calls, _exit


def _wait_until(pred, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return bool(pred())


def test_sentinel_fresh_ignores_stale_and_accepts_new() -> bool:
    path = _tmp_sentinel()
    now = time.time()
    path.touch()
    os.utime(path, (now - 3600, now - 3600))
    stale = self_stop.sentinel_fresh(path, started_at=now)
    path.touch()
    fresh = self_stop.sentinel_fresh(path, started_at=now - 5)
    missing = self_stop.sentinel_fresh(path.parent / "nope", started_at=now)
    return stale is False and fresh is True and missing is False


def test_off_state_does_nothing() -> bool:
    self_stop.reset_for_tests()
    self_stop.configure(sentinel=_tmp_sentinel())
    try:
        return (self_stop.is_installed() is False
                and self_stop.stop_requested() is False
                and self_stop.stop_reason() == ""
                and self_stop.watcher_thread() is None)
    finally:
        self_stop.reset_for_tests()


def test_flag_channel_is_idempotent_and_names_reason() -> bool:
    self_stop.reset_for_tests()
    self_stop.configure(sentinel=_tmp_sentinel())
    try:
        first = self_stop.request_stop("unit_test")
        second = self_stop.request_stop("again")
        return (first is True and second is False
                and self_stop.stop_requested() is True
                and self_stop.stop_reason() == "unit_test")
    finally:
        self_stop.reset_for_tests()


def test_blocked_main_loop_still_gets_force_exit() -> bool:
    """事故形态：主线程（这里就是测试线程）从不调用 mark_graceful_exit。"""
    sentinel = _tmp_sentinel()
    calls, recorder = _recorder()
    self_stop.reset_for_tests()
    try:
        self_stop.install(sentinel=sentinel, started_at=time.time(),
                          poll_seconds=0.05, grace_seconds=0.25,
                          exit_func=recorder)
        sentinel.touch()
        started = time.monotonic()
        fired = _wait_until(lambda: bool(calls), timeout=3.0)
        elapsed = time.monotonic() - started
        # 模拟强退：recorder 只记账，进程继续；心跳线程自己退出。
        joined = _wait_until(lambda: not self_stop.is_installed(), timeout=1.0)
        return fired and calls == [0] and elapsed < 3.0 and joined
    finally:
        self_stop.reset_for_tests()


def test_graceful_exit_suppresses_force_exit() -> bool:
    sentinel = _tmp_sentinel()
    calls, recorder = _recorder()
    self_stop.reset_for_tests()
    try:
        self_stop.install(sentinel=sentinel, started_at=time.time(),
                          poll_seconds=0.05, grace_seconds=0.4,
                          exit_func=recorder)
        self_stop.request_stop("graceful")
        self_stop.mark_graceful_exit()
        time.sleep(0.8)
        return calls == [] and self_stop.is_installed() is False
    finally:
        self_stop.reset_for_tests()


def test_install_is_idempotent_and_daemon() -> bool:
    sentinel = _tmp_sentinel()
    calls, recorder = _recorder()
    self_stop.reset_for_tests()
    try:
        first = self_stop.install(sentinel=sentinel, started_at=time.time(),
                                  poll_seconds=0.05, grace_seconds=0.2,
                                  exit_func=recorder)
        second = self_stop.install()
        return (first is second
                and first.daemon is True
                and first.name == "self-stop-watch"
                and self_stop.is_installed() is True)
    finally:
        self_stop.reset_for_tests()


def test_configure_clamps_and_reset_restores_defaults() -> bool:
    self_stop.reset_for_tests()
    try:
        self_stop.configure(poll=0.0, grace=-3.0)
        clamped = (self_stop.poll_seconds() >= 0.05
                   and self_stop.grace_seconds() >= 0.0)
    finally:
        self_stop.reset_for_tests()
    return clamped and self_stop.poll_seconds() == self_stop.DEFAULT_POLL_SECONDS


def main() -> bool:
    tests = [(name, obj) for name, obj in sorted(globals().items())
             if name.startswith("test_") and callable(obj)]
    passed = 0
    try:
        for name, fn in tests:
            try:
                ok = bool(fn())
            except Exception as exc:  # noqa: BLE001
                print(f"FAIL {name}: {type(exc).__name__}: {exc}")
                ok = False
            if ok:
                passed += 1
                print(f"PASS {name}")
            else:
                print(f"FAIL {name}")
    finally:
        _cleanup_tmp_dirs()
    print(f"{passed}/{len(tests)} passed")
    return passed == len(tests)


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
