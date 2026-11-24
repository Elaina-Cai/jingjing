# -*- coding: utf-8 -*-
r"""task-53 验收：临时目录固定 + CANTOPEN 退避重试（事件不许悄悄丢）。

钉六件事：

1. ``pin_temp_dir`` 把 ``TEMP``/``TMP``/``TMPDIR``/``SQLITE_TMPDIR`` 与
   ``tempfile.tempdir`` 一起钉到第一个**真实可写**的候选（探针文件写完即删）；
2. 首选不可写时按候选顺序回落，全部不可写时返回 ``""`` 且**不改**已有环境变量
   （不阻断启动，只写 ``temp_dir_unusable``）；
3. 生产写入路径**不依赖系统临时目录**：``TEMP`` 指向不存在的路径时，
   ``TerminalEventStore.append`` 仍然落库（``PRAGMA temp_store=MEMORY``）；
4. ``SQLITE_CANTOPEN``（``unable to open database file``）带退避重试
   ``_CANTOPEN_MAX_RETRIES`` 轮，每轮 dispose 换连接，日志带 ``attempt``；
5. 重试耗尽才抛；非 CANTOPEN 的 ``OperationalError``（``database is locked``）**一次都不
   重试**；
6. 接线：``scheduler/main.py`` 在 ``init_logging`` 之后、``get_db_path()``/任何连接之前
   调用 ``_pin_temp_dir()``。

复算：``.\.venv_it\Scripts\python.exe -X utf8 -u tests\test_temp_dir_pin.py``
"""
from __future__ import annotations

import os
import pathlib
import sqlite3
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ISO = ROOT / ".test_tmp" / "temp_pin"
(ISO / "platforms").mkdir(parents=True, exist_ok=True)
(ISO / "data").mkdir(parents=True, exist_ok=True)
os.environ.setdefault("META_DB_PATH", str(ISO / "meta.db"))
os.environ.setdefault("PLATFORMS_DIR", str(ISO / "platforms"))
os.environ.setdefault("DATA_DIR", str(ISO / "data"))

import sqlalchemy as sa  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from core.infra import tempdir as T  # noqa: E402
from core.terminal import event_store_db as E  # noqa: E402
from core.terminal.event_model import Event, EventType  # noqa: E402
from db.meta_models import MetaBase  # noqa: E402

PASSED = 0
FAILED: list[str] = []


def ok(name: str, cond: bool, detail: object = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
    else:
        FAILED.append("%s %r" % (name, detail))


class _Recorder:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def info(self, event, **kw):
        self.events.append((str(event), kw))

    def warning(self, event, **kw):
        self.events.append((str(event), kw))

    def debug(self, event, **kw):
        self.events.append((str(event), kw))

    def error(self, event, **kw):
        self.events.append((str(event), kw))

    def named(self, event):
        return [kw for name, kw in self.events if name == event]


def _restore_env(snapshot: dict[str, str | None]) -> None:
    for key, value in snapshot.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


# ---------------------------------------------------------------------------
# 1) 探针可写性
# ---------------------------------------------------------------------------
def test_usable_dir() -> None:
    good = ISO / "ok" / "nested"
    ok("usable_dir 新建嵌套目录", T.usable_dir(good) is True)
    blocker = ISO / "blocker"
    blocker.write_text("x", encoding="utf-8")
    ok("父路径是文件 ⇒ 不可写", T.usable_dir(blocker / "sub") is False)
    ok("探针文件不残留", list(good.glob(".probe_*")) == [])


# ---------------------------------------------------------------------------
# 2) 钉住 + 回落 + 全不可写
# ---------------------------------------------------------------------------
def test_pin_sets_every_env() -> None:
    snapshot = {k: os.environ.get(k) for k in ("TEMP", "TMP", "TMPDIR", "SQLITE_TMPDIR")}
    old_tempdir = tempfile.tempdir
    rec = _Recorder()
    real_log, T.log = T.log, rec
    try:
        good = ISO / "pinned"
        chosen = T.pin_temp_dir(prefer=good, candidates=[good])
        ok("返回选中目录", chosen == str(good), chosen)
        for key in ("TEMP", "TMP", "TMPDIR", "SQLITE_TMPDIR"):
            ok("env %s 已钉" % key, os.environ.get(key) == str(good), os.environ.get(key))
        ok("tempfile.tempdir 已钉", tempfile.tempdir == str(good), tempfile.tempdir)
        ok("写一行 temp_dir_pinned", len(rec.named("temp_dir_pinned")) == 1)
        ok("temp_dir_pinned 带 path", rec.named("temp_dir_pinned")[0].get("path") == str(good))
    finally:
        T.log = real_log
        tempfile.tempdir = old_tempdir
        _restore_env(snapshot)


def test_pin_falls_back() -> None:
    snapshot = {k: os.environ.get(k) for k in ("TEMP", "TMP", "TMPDIR", "SQLITE_TMPDIR")}
    old_tempdir = tempfile.tempdir
    blocker = ISO / "blocker"
    if not blocker.exists():
        blocker.write_text("x", encoding="utf-8")
    bad = blocker / "sub"
    good = ISO / "fallback_ok"
    rec = _Recorder()
    real_log, T.log = T.log, rec
    try:
        chosen = T.pin_temp_dir(prefer=bad, candidates=[bad, good])
        ok("首选不可写 ⇒ 回落到第二候选", chosen == str(good), chosen)
        ok("回落时 env 跟着改", os.environ.get("TEMP") == str(good))
    finally:
        T.log = real_log
        tempfile.tempdir = old_tempdir
        _restore_env(snapshot)


def test_pin_all_unusable() -> None:
    snapshot = {k: os.environ.get(k) for k in ("TEMP", "TMP", "TMPDIR", "SQLITE_TMPDIR")}
    old_tempdir = tempfile.tempdir
    blocker = ISO / "blocker"
    if not blocker.exists():
        blocker.write_text("x", encoding="utf-8")
    rec = _Recorder()
    real_log, T.log = T.log, rec
    try:
        os.environ["TEMP"] = "keep-me"
        chosen = T.pin_temp_dir(prefer=blocker / "a", candidates=[blocker / "b"])
        ok("全部不可写 ⇒ 返回空串", chosen == "")
        ok("全部不可写不改 env", os.environ.get("TEMP") == "keep-me")
        ok("写一行 temp_dir_unusable", len(rec.named("temp_dir_unusable")) == 1)
    finally:
        T.log = real_log
        tempfile.tempdir = old_tempdir
        _restore_env(snapshot)


# ---------------------------------------------------------------------------
# 3) 落库不依赖系统临时目录
# ---------------------------------------------------------------------------
def _db_engine(name: str):
    path = ISO / name
    for suffix in ("", "-wal", "-shm"):
        target = pathlib.Path(str(path) + suffix)
        if target.exists():
            target.unlink()
    engine = sa.create_engine(f"sqlite:///{path.as_posix()}", future=True)
    MetaBase.metadata.create_all(engine)
    return engine, path


def _event(idx: int) -> Event:
    return Event(
        event_id="",
        occurred_at=None,
        platform="wechat",
        bot_account_id="bot",
        chat_id="g1",
        chat_type="group",
        actor_person_id="p1",
        event_type=EventType.MESSAGE,
        content="hi %d" % idx,
        external_msg_id="m%d" % idx,
    )


def test_append_with_broken_temp_env() -> None:
    snapshot = {k: os.environ.get(k) for k in ("TEMP", "TMP", "TMPDIR", "SQLITE_TMPDIR")}
    old_tempdir = tempfile.tempdir
    engine, path = _db_engine("no_temp.db")
    bad = ISO / "missing" / "deep"
    try:
        for key in ("TEMP", "TMP", "TMPDIR", "SQLITE_TMPDIR"):
            os.environ[key] = str(bad)
        tempfile.tempdir = str(bad)
        store = E.TerminalEventStore(engine)
        first = store.append(_event(1))
        second = store.append(_event(2))
        ok("TEMP 坏也能落库(1)", first.sequence == 1, first.sequence)
        ok("TEMP 坏也能落库(2)", second.sequence == 2, second.sequence)
        with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as con:
            rows = con.execute("select sequence, content from terminal_events order by id").fetchall()
        ok("库里真有两行", rows == [(1, "hi 1"), (2, "hi 2")], rows)
    finally:
        engine.dispose()
        tempfile.tempdir = old_tempdir
        _restore_env(snapshot)


def test_temp_store_memory_pragma() -> None:
    engine, _ = _db_engine("pragma.db")
    try:
        with Session(engine) as session:
            E._prefer_memory_temp(session)
            value = session.execute(text("PRAGMA temp_store")).scalar()
        ok("temp_store=MEMORY(2)", int(value) == 2, value)
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# 4) CANTOPEN 退避重试
# ---------------------------------------------------------------------------
class _FakeStore(E.TerminalEventStore):
    def __init__(self, failures: int):
        super().__init__(engine=None)
        self.failures = failures
        self.calls = 0

    def _append_once(self, event: Event) -> Event:  # noqa: D401 - 替身
        self.calls += 1
        if self.calls <= self.failures:
            raise OperationalError("INSERT ...", {}, Exception("unable to open database file"))
        return event


def test_cantopen_retries_then_succeeds() -> None:
    rec = _Recorder()
    real_log, real_sleep = E.log, E.time.sleep
    E.log, E.time.sleep = rec, lambda _s: None
    try:
        store = _FakeStore(failures=2)
        out = store.append(_event(9))
        ok("重试后成功", out is not None)
        ok("共尝试 3 次", store.calls == 3, store.calls)
        attempts = [kw.get("attempt") for kw in rec.named("terminal_event_store_reconnect")]
        ok("两次重连日志 attempt=1,2", attempts == [1, 2], attempts)
        diag = rec.named("terminal_event_store_reconnect")[0]
        ok("CANTOPEN 日志带 db_path", "db_path" in diag, list(diag))
        ok("CANTOPEN 日志带 cwd", "cwd" in diag, list(diag))
        ok("CANTOPEN 日志带 wal/shm 状态", "wal_exists" in diag and "shm_exists" in diag)
        ok("CANTOPEN 日志带 sqlite 版本", bool(diag.get("sqlite_version")))
    finally:
        E.log, E.time.sleep = real_log, real_sleep


def test_cantopen_budget_exhausted() -> None:
    rec = _Recorder()
    real_log, real_sleep = E.log, E.time.sleep
    E.log, E.time.sleep = rec, lambda _s: None
    try:
        store = _FakeStore(failures=99)
        raised = False
        try:
            store.append(_event(10))
        except OperationalError as exc:
            raised = "unable to open database file" in str(exc)
        ok("预算耗尽后抛出 CANTOPEN", raised)
        ok("尝试次数 = 1 + 预算", store.calls == E._CANTOPEN_MAX_RETRIES + 1, store.calls)
        ok("每次都有 reconnect 日志", len(rec.named("terminal_event_store_reconnect")) == store.calls)
    finally:
        E.log, E.time.sleep = real_log, real_sleep


def test_other_operational_error_no_retry() -> None:
    class _Locked(_FakeStore):
        def _append_once(self, event: Event) -> Event:
            self.calls += 1
            raise OperationalError("UPDATE ...", {}, Exception("database is locked"))

    rec = _Recorder()
    real_log, real_sleep = E.log, E.time.sleep
    E.log, E.time.sleep = rec, lambda _s: None
    try:
        store = _Locked(failures=0)
        raised = False
        try:
            store.append(_event(11))
        except OperationalError:
            raised = True
        ok("非 CANTOPEN 立即抛", raised)
        ok("非 CANTOPEN 只尝试 1 次", store.calls == 1, store.calls)
        ok("非 CANTOPEN 不写 reconnect", rec.named("terminal_event_store_reconnect") == [])
    finally:
        E.log, E.time.sleep = real_log, real_sleep


def test_budget_constants() -> None:
    ok("预算 ≥3", E._CANTOPEN_MAX_RETRIES >= 3, E._CANTOPEN_MAX_RETRIES)
    ok("退避序列非空且单调不减", list(E._CANTOPEN_BACKOFF) == sorted(E._CANTOPEN_BACKOFF)
       and len(E._CANTOPEN_BACKOFF) >= 1, list(E._CANTOPEN_BACKOFF))


# ---------------------------------------------------------------------------
# 5) 接线：scheduler/main.py 的调用位置
# ---------------------------------------------------------------------------
def test_scheduler_wiring() -> None:
    src = (ROOT / "scheduler" / "main.py").read_text(encoding="utf-8")
    pin_at = src.index("_PINNED_TEMP_DIR = _pin_temp_dir()")
    log_at = src.index("init_logging(log_dir=")
    db_at = src.index("DB_PATH = get_db_path()")
    ok("import 在文件头", src.index("from core.infra.tempdir import pin_temp_dir") < log_at)
    ok("pin 在 init_logging 之后", pin_at > log_at)
    ok("pin 在 get_db_path 之前", pin_at < db_at)
    ok("pin 只调用一次", src.count("_pin_temp_dir()") == 1, src.count("_pin_temp_dir()"))


def main() -> int:
    test_usable_dir()
    test_pin_sets_every_env()
    test_pin_falls_back()
    test_pin_all_unusable()
    test_append_with_broken_temp_env()
    test_temp_store_memory_pragma()
    test_cantopen_retries_then_succeeds()
    test_cantopen_budget_exhausted()
    test_other_operational_error_no_retry()
    test_budget_constants()
    test_scheduler_wiring()
    if FAILED:
        print("\nFAILED %d / %d" % (len(FAILED), len(FAILED) + PASSED))
        for name in FAILED:
            print(" -", name)
        return 1
    print("\nALL PASSED (%d)" % PASSED)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
