"""Phase 4 background cognition queue.

Analysis / person-memory / dream / feedback work must not block ingestion or the
reply path.  Jobs are submitted as zero-argument async callables; workers run
them with a bounded queue and log-and-drop backpressure so a slow model can
never grow an unbounded memory backlog.

多事件循环（2026-09-23 修复）：调度器里有多个 loop（持久 ``_scheduler_loop``、
web 线程 loop、watcher 线程的临时 ``asyncio.run`` loop）。单例 queue 绑定首个
loop 后，别的 loop 上新建的 worker 一 ``await get()`` 就
``RuntimeError: bound to a different event loop`` 秒死、反复重建（当天 385 次
报错 / 233 次重启、约 12% 后台任务没跑完）。现在：

1. **每个 loop 一份 queue + workers**（键是 loop 对象；关闭的 loop 自动清理，
   未执行任务计入 dropped 并告警）；
2. 可注册一个**首选 loop**（``set_worker_loop``，scheduler 用它的持久 loop）：
   从别的 loop/线程提交时转投过去排队执行，任务不再随临时 loop 关闭被取消；
3. 首选 loop 当前没在跑也没关系——回调会排进它的 _ready，下一次 tick 执行。
"""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

import structlog
from safelog import warn
from core.infra.jsonio import atomic_write_text

log = structlog.get_logger()

Job = Callable[[], Awaitable[None]]


@dataclass
class _LoopState:
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue
    workers: list[asyncio.Task] = field(default_factory=list)


class BackgroundQueue:
    def __init__(self, *, maxsize: int = 200, workers: int = 2, name: str = "terminal"):
        self.name = name
        self.maxsize = max(1, int(maxsize))
        self.worker_count = max(1, int(workers))
        self._states: dict[asyncio.AbstractEventLoop, _LoopState] = {}
        self._worker_loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.RLock()
        self._submitted = 0
        self._completed = 0
        self._failed = 0
        self._dropped = 0

    # ------------------------------------------------------------- public api
    def set_worker_loop(self, loop: asyncio.AbstractEventLoop | None) -> None:
        """注册「首选执行 loop」：其他 loop 的提交会转投到它。"""
        with self._lock:
            self._worker_loop = loop

    def start(self) -> None:
        """确保当前 loop 的 workers 已起（多数调用方直接用 submit_nowait）。"""
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            warn("core/terminal/background.py:68 RuntimeError", err=repr(__import__("sys").exc_info()[1]))
            return
        with self._lock:
            self._purge_closed_locked()
            self._ensure_state_locked(current)

    def submit_nowait(self, job_name: str, job: Job) -> bool:
        """Queue a job; return False when there is no usable loop or it is full."""
        with self._lock:
            self._purge_closed_locked()
            worker_loop = self._worker_loop
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            warn("core/terminal/background.py:81 RuntimeError", err=repr(__import__("sys").exc_info()[1]))
            current_loop = None

        if (
            worker_loop is not None
            and not worker_loop.is_closed()
            and worker_loop is not current_loop
        ):
            try:
                asyncio.run_coroutine_threadsafe(
                    self._enqueue_on_loop(worker_loop, job_name, job), worker_loop
                )
            except Exception as exc:  # noqa: BLE001 - 提交永远不能拖垮调用方
                log.warning(
                    "background_submit_failed",
                    name=self.name,
                    job=job_name,
                    error=str(exc)[:120],
                )
                return False
            return True

        if current_loop is None:
            return False
        with self._lock:
            state = self._ensure_state_locked(current_loop)
            return self._put_locked(state, job_name, job)

    async def join(self) -> None:
        with self._lock:
            queues = [state.queue for state in self._states.values()]
        for queue in queues:
            await queue.join()

    def stop(self) -> None:
        with self._lock:
            for state in self._states.values():
                for task in state.workers:
                    if not task.done():
                        task.cancel()
                state.workers.clear()
            self._states.clear()

    def metrics(self) -> dict:
        with self._lock:
            self._purge_closed_locked()
            size = sum(state.queue.qsize() for state in self._states.values())
            workers = sum(
                1
                for state in self._states.values()
                for task in state.workers
                if not task.done()
            )
            return {
                "name": self.name,
                "size": int(size),
                "maxsize": self.maxsize,
                "workers": workers,
                "loops": len(self._states),
                "submitted": self._submitted,
                "completed": self._completed,
                "failed": self._failed,
                "dropped": self._dropped,
            }

    # -------------------------------------------------------------- internals
    def _ensure_state_locked(self, loop: asyncio.AbstractEventLoop) -> _LoopState:
        """调用方必须持有 ``_lock`` 且运行在目标 loop 上（create_task 线程约束）。"""
        state = self._states.get(loop)
        if state is None:
            state = _LoopState(loop=loop, queue=asyncio.Queue(maxsize=self.maxsize))
            self._states[loop] = state
        if not any(not task.done() for task in state.workers):
            state.workers = [
                loop.create_task(self._worker(state, index))
                for index in range(self.worker_count)
            ]
            log.info(
                "background_queue_started",
                name=self.name,
                workers=self.worker_count,
                maxsize=self.maxsize,
            )
        return state

    async def _enqueue_on_loop(
        self, loop: asyncio.AbstractEventLoop, job_name: str, job: Job
    ) -> None:
        with self._lock:
            state = self._ensure_state_locked(loop)
            self._put_locked(state, job_name, job)

    def _put_locked(self, state: _LoopState, job_name: str, job: Job) -> bool:
        try:
            state.queue.put_nowait((str(job_name), job))
            self._submitted += 1
            log.debug(
                "background_job_queued",
                name=self.name,
                job=job_name,
                size=state.queue.qsize(),
            )
            return True
        except asyncio.QueueFull:
            self._dropped += 1
            log.warning(
                "background_queue_full",
                name=self.name,
                job=job_name,
                maxsize=self.maxsize,
            )
            return False

    def _purge_closed_locked(self) -> None:
        dead = [loop for loop in self._states if loop.is_closed()]
        for loop in dead:
            state = self._states.pop(loop, None)
            if state is None:
                continue
            pending = state.queue.qsize()
            if pending:
                self._dropped += pending
                log.warning(
                    "background_queue_loop_closed",
                    name=self.name,
                    pending=pending,
                )
            for task in state.workers:
                if not task.done():
                    task.cancel()
            state.workers.clear()

    async def _worker(self, state: _LoopState, index: int) -> None:
        log.info("background_worker_started", name=self.name, worker=index)
        while True:
            job_name, job = await state.queue.get()
            try:
                await job()
                with self._lock:
                    self._completed += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - worker must survive jobs
                with self._lock:
                    self._failed += 1
                log.warning(
                    "background_job_failed",
                    name=self.name,
                    job=job_name,
                    error=str(exc)[:200],
                )
            finally:
                state.queue.task_done()


_queue: BackgroundQueue | None = None
_queue_lock = threading.RLock()


def get_background_queue() -> BackgroundQueue:
    global _queue
    with _queue_lock:
        if _queue is None:
            _queue = BackgroundQueue()
        return _queue


def submit_background_job(job_name: str, job: Job) -> bool:
    return get_background_queue().submit_nowait(job_name, job)


def set_background_worker_loop(loop: asyncio.AbstractEventLoop | None) -> None:
    """注册首选执行 loop（scheduler 在启动时注册自己的持久 loop）。"""
    get_background_queue().set_worker_loop(loop)


def background_queue_metrics() -> dict:
    return get_background_queue().metrics()


def reset_background_queue() -> None:
    global _queue
    with _queue_lock:
        if _queue is not None:
            _queue.stop()
        _queue = None


def background_metrics_snapshot_path() -> Path:
    """Cross-process handoff file for the scheduler-owned queue metrics.

    The scheduler and the Web UI run as separate processes, so the Web
    ``/api/status`` endpoint cannot read the scheduler's in-memory queue
    directly.  The scheduler writes this small JSON snapshot; the Web reads it
    best-effort and marks it stale via ``age_seconds``.
    """
    from core.config.settings import config

    return Path(config.paths.log_dir) / "background_queue_metrics.json"


def write_background_queue_metrics_snapshot() -> dict[str, Any]:
    """Persist the current queue metrics; safe to call from any thread."""
    metrics = dict(background_queue_metrics())
    metrics["recorded_at"] = datetime.now().isoformat()
    payload = json.dumps(metrics, ensure_ascii=False, default=str, indent=2)
    target = background_metrics_snapshot_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(target, payload)
    except Exception as exc:  # noqa: BLE001 - metrics are best-effort
        log.debug("background_metrics_snapshot_write_failed", error=str(exc)[:160])
    return metrics


def read_background_queue_metrics_snapshot() -> dict[str, Any]:
    """Read the scheduler-published snapshot; return an unavailable marker."""
    target = background_metrics_snapshot_path()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        warn("core/terminal/background.py:303 Exception", err=repr(__import__("sys").exc_info()[1]))
        return {"available": False}
    if not isinstance(data, dict):
        return {"available": False}
    data["available"] = True
    recorded_at = str(data.get("recorded_at") or "")
    if recorded_at:
        try:
            recorded = datetime.fromisoformat(recorded_at)
            data["age_seconds"] = max(
                0.0, (datetime.now() - recorded).total_seconds()
            )
        except Exception:
            warn("core/terminal/background.py:315 Exception", err=repr(__import__("sys").exc_info()[1]))
            data["age_seconds"] = None
    else:
        data["age_seconds"] = None
    return data
