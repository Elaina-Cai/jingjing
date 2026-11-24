"""后台队列多事件循环回归：跨 loop 提交不互杀；首选 loop 不丢任务。

Run directly: python tests/test_background_queue_multiloop.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.terminal.background import BackgroundQueue  # noqa: E402


def _wait_completed(queue: BackgroundQueue, want: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if queue.metrics().get("completed", 0) >= want:
            return True
        time.sleep(0.05)
    return False


def test_cross_loop_submits_do_not_kill_workers() -> None:
    """两个先后 asyncio.run 的 loop 各提交一个任务：都要跑到（旧代码第二个丢）。"""
    queue = BackgroundQueue(workers=1, name="test")
    done: list[int] = []

    async def run_one(value: int) -> None:
        async def job() -> None:
            done.append(value)

        assert queue.submit_nowait(f"job-{value}", job) is True
        await asyncio.sleep(0.2)

    asyncio.run(run_one(1))
    asyncio.run(run_one(2))
    assert set(done) == {1, 2}, done
    print("  OK  跨 loop 提交不再互相打死")


def test_preferred_loop_receives_foreign_submits() -> None:
    """注册首选 loop 后，临时 loop 提交的任务在首选 loop 上执行，不被关闭带走。"""
    queue = BackgroundQueue(workers=1, name="test-pref")
    done: list[int] = []

    loop = asyncio.new_event_loop()

    def run_loop() -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()

    thread = threading.Thread(target=run_loop, daemon=True)
    thread.start()
    queue.set_worker_loop(loop)

    async def from_ephemeral_loop() -> None:
        async def job() -> None:
            done.append(threading.get_ident())

        assert queue.submit_nowait("foreign", job) is True

    asyncio.run(from_ephemeral_loop())  # 临时 loop 提交完立刻关闭
    assert _wait_completed(queue, 1), queue.metrics()
    assert done == [thread.ident], (done, thread.ident)
    loop.call_soon_threadsafe(queue.stop)
    asyncio.run_coroutine_threadsafe(asyncio.sleep(0.05), loop).result(timeout=2)
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=2)
    loop.close()
    print("  OK  首选 loop 接收外部提交且任务不在临时 loop 上丢")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {test.__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
