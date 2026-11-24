"""调度器卡死防护（2026-10-17 主循环卡死事故的回归钉）：job 超时 + 心跳 + 硬重启判据。

standalone 风格（同 tests/test_recall_chain.py）：`python tests/test_scheduler_watchdog.py`。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import scheduler.main as sm  # noqa: E402
from _tempdirs import make_db_dir


@contextmanager
def _env(name: str, value: str):
    old = os.environ.get(name)
    os.environ[name] = value
    try:
        yield
    finally:
        if old is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = old


@contextmanager
def _unset(name: str):
    old = os.environ.pop(name, None)
    try:
        yield
    finally:
        if old is not None:
            os.environ[name] = old


def test_job_timeout_defaults() -> bool:
    with _unset("YINYUE_JOB_TIMEOUT_SECONDS"):
        assert sm.job_timeout_seconds("run_memory_projection") == 180.0
        assert sm.job_timeout_seconds("run_sync_pipeline") == 360.0  # 同步管线 x2
    with _env("YINYUE_JOB_TIMEOUT_SECONDS", "45"):
        assert sm.job_timeout_seconds("run_daily_digest") == 45.0
        assert sm.job_timeout_seconds("run_sync_pipeline") == 90.0
    with _env("YINYUE_JOB_TIMEOUT_SECONDS", "0"):
        assert sm.job_timeout_seconds("run_daily_digest") == 0.0  # 0 = 回到无限等待
    with _env("YINYUE_JOB_TIMEOUT_SECONDS", "abc"):
        assert sm.job_timeout_seconds("run_daily_digest") == 180.0
    return True


def test_stuck_restart_threshold() -> bool:
    with _unset("YINYUE_JOB_STUCK_RESTART_SECONDS"):
        assert sm.job_stuck_restart_seconds() == 900.0
    with _env("YINYUE_JOB_STUCK_RESTART_SECONDS", "120"):
        assert sm.job_stuck_restart_seconds() == 120.0
    with _env("YINYUE_JOB_STUCK_RESTART_SECONDS", "bad"):
        assert sm.job_stuck_restart_seconds() == 900.0
    return True


def test_heartbeat_payload_shape() -> bool:
    payload = sm.heartbeat_payload()
    for key in (
        "pid",
        "ts",
        "uptime_s",
        "job",
        "job_elapsed_s",
        "main_loop_ticks",
        "main_loop_age_s",
        "heartbeat_interval_s",
    ):
        assert key in payload, key
    assert payload["pid"] > 0
    return True


def test_write_heartbeat_once_is_atomic_json() -> bool:
    tmp = Path(make_db_dir("hb_"))
    path = tmp / "loop_heartbeat.json"
    old = sm._HEARTBEAT_PATH
    sm._HEARTBEAT_PATH = path
    try:
        payload = sm.write_heartbeat_once()
        assert path.exists()
        loaded = json.loads(path.read_text(encoding="utf-8"))
        assert loaded["pid"] == payload["pid"]
        assert not (tmp / "loop_heartbeat.tmp").exists()
    finally:
        sm._HEARTBEAT_PATH = old
    return True


def test_restart_reason_matrix() -> bool:
    assert sm.heartbeat_restart_reason({"job": "", "main_loop_ticks": 5, "main_loop_age_s": 1.0}, 900) == ""
    assert sm.heartbeat_restart_reason(
        {"job": "run_daily_digest", "job_elapsed_s": 901}, 900
    ).startswith("job:")
    assert sm.heartbeat_restart_reason(
        {"job": "", "main_loop_ticks": 5, "main_loop_age_s": 1200}, 900
    ) == "main_loop"
    # 从未跑过主循环（ticks=0）不算「主循环卡住」，避免启动期误杀
    assert sm.heartbeat_restart_reason(
        {"job": "", "main_loop_ticks": 0, "main_loop_age_s": 5000}, 900
    ) == ""
    assert sm.heartbeat_restart_reason({"job": "x", "job_elapsed_s": 5000}, 0) == ""
    return True


def test_run_async_returns_value() -> bool:
    async def quick():
        return 42

    assert sm._run_async(quick(), name="test_quick", timeout=30) == 42
    assert sm.current_job()["name"] == ""
    return True


def test_run_async_timeout_does_not_block_forever() -> bool:
    async def never():
        await asyncio.sleep(3600)

    assert sm._run_async(never(), name="test_never", timeout=0.3) is None
    assert sm.current_job()["name"] == ""
    stacks = list((ROOT / "logs").glob("job_stack_*.txt"))
    assert stacks, "超时必须把全线程栈落到 logs/job_stack_*.txt"
    return True


def main() -> bool:
    names = sorted(name for name in globals() if name.startswith("test_"))
    ok = True
    for name in names:
        fn = globals()[name]
        try:
            result = bool(fn())
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL {name}: {type(exc).__name__}: {exc}")
            result = False
        print(("ok   " if result else "FAIL ") + name)
        ok = ok and result
    print(f"scheduler watchdog: {len(names)} tests, {'all passed' if ok else 'FAILED'}")
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
