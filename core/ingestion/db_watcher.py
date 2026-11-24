from __future__ import annotations

import asyncio
import os
import time
import structlog
from pathlib import Path
from threading import Thread, Event as ThreadEvent, Lock

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileModifiedEvent, FileCreatedEvent
from safelog import warn

log = structlog.get_logger()


class WeChatDbWatcher:
    """
    监听微信数据库文件变化，检测到新消息时立刻触发同步。

    工作原理：
    1. 用 watchdog 监听微信 db_storage 目录
    2. 检测到 .db 文件修改/创建 → 记录变化
    3. 防抖：短时间内多次变化合并为一次同步
    4. 触发同步回调
    """

    def __init__(
        self,
        db_storage_path: str,
        on_change_callback=None,
        debounce_seconds: float = 0.15,
        account_id: str = "",
        platform: str = "wechat",
    ):
        self._db_storage_path = db_storage_path
        self._on_change = on_change_callback
        self._debounce_seconds = debounce_seconds
        self._account_id = account_id
        self._platform = platform
        self._observer: Observer | None = None
        self._pending: bool = False
        self._stop_event = ThreadEvent()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._debounce_lock = Lock()
        self._debounce_timer: Thread | None = None
        self._changed_dbs: set[str] = set()

    def start(self, loop: asyncio.AbstractEventLoop | None = None):
        if not os.path.isdir(self._db_storage_path):
            log.warning("db_watcher_path_not_found", path=self._db_storage_path)
            return

        self._loop = loop
        handler = _DbEventHandler(self._on_file_changed)

        self._observer = Observer()
        self._observer.schedule(handler, self._db_storage_path, recursive=True)
        self._observer.daemon = True
        self._observer.start()
        log.info("db_watcher_started", path=self._db_storage_path)

    def stop(self):
        self._stop_event.set()
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None
        log.info("db_watcher_stopped")

    def _on_file_changed(self, src_path: str):
        if self._stop_event.is_set():
            return

        path = Path(src_path)
        name_lower = path.name.lower()
        if not (name_lower.endswith(".db") or name_lower.endswith(".db-wal") or name_lower.endswith(".db-shm")):
            return

        db_base = name_lower
        for suffix in (".db-wal", ".db-shm"):
            if db_base.endswith(suffix):
                db_base = db_base[: -len(suffix)]
                break
        skip_bases = {"misc.db", "oplog.db", "session.db", "sns.db", "solitaire.db", "third_app_icon.db", "weclaw.db"}
        if db_base in skip_bases:
            return

        with self._debounce_lock:
            self._changed_dbs.add(db_base)
            self._pending = True
            if self._debounce_timer is not None and self._debounce_timer.is_alive():
                return
            self._debounce_timer = Thread(target=self._debounce_fire, daemon=True)
            self._debounce_timer.start()

    def _debounce_fire(self):
        time.sleep(self._debounce_seconds)
        with self._debounce_lock:
            if self._stop_event.is_set():
                return
            if not self._pending:
                return
            self._pending = False
            changed = list(self._changed_dbs)
            self._changed_dbs.clear()

        log.info("db_watcher_change_detected", dbs=changed)

        if self._on_change:
            if self._loop and self._loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    self._on_change(), self._loop
                )
            else:
                try:
                    asyncio.run(self._on_change())
                except RuntimeError:
                    warn("core/ingestion/db_watcher.py:117 RuntimeError", err=repr(__import__("sys").exc_info()[1]))


class _DbEventHandler(FileSystemEventHandler):
    def __init__(self, callback):
        super().__init__()
        self._callback = callback

    def on_modified(self, event):
        if not event.is_directory:
            self._callback(event.src_path)

    def on_created(self, event):
        if not event.is_directory:
            self._callback(event.src_path)
