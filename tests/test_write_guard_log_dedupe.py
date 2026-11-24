#!/usr/bin/env python
"""Regression: ``production_write_allowed`` must not flood the log.

2026-10-17 实测：`logs/yinyue.log` 8618 行里 8086 行（94%）是这条事件——
每次打开生产库可写句柄都会落一行，把真事件全埋了。Pinned here:

1. 同一个 ``(target, 声明者)`` 进程内只记一次；
2. 不同 target 各记一次（审计仍然知道"谁开了哪几个生产库"）；
3. 重新声明（新入口）后重新计一次，且旧的去重集合被清空。
"""
from __future__ import annotations

import sys
import traceback
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
# 临时目录的唯一入口（见 tests/_tempdirs.py）：不给 dir= 时系统 TEMP 不可写会静默回落到仓库根。
from _tempdirs import make_db_dir  # noqa: E402

import db.write_guard as wg  # noqa: E402
from core import bootstrap  # noqa: E402


@contextmanager
def _production_paths_are(*paths: object):
    saved = bootstrap.production_data_paths
    bootstrap.production_data_paths = lambda: tuple(
        (str(p), "write_guard log dedupe 用例注入") for p in paths
    )
    try:
        yield
    finally:
        bootstrap.production_data_paths = saved


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self._orig = None

    def __enter__(self):
        self._orig = wg.log.info
        recorder = self

        def fake(event, **kw):
            recorder.events.append((event, kw))

        wg.log.info = fake  # type: ignore[assignment]
        return self

    def __exit__(self, *exc):
        wg.log.info = self._orig  # type: ignore[assignment]
        return False

    def count(self, name: str) -> int:
        return sum(1 for event, _ in self.events if event == name)


def test_same_target_logged_once() -> None:
    base = make_db_dir("wg_log_")
    target = base / "yinyue_meta.db"
    wg.declare_writes_allowed(declared_by="tests/test_write_guard_log_dedupe")
    try:
        with _production_paths_are(target), _Recorder() as rec:
            for _ in range(5):
                wg.assert_writes_allowed(target, what="可写句柄")
            assert rec.count("production_write_allowed") == 1, rec.events
            assert rec.count("production_writes_declared") == 0, "声明事件不该因断言重放"
    finally:
        wg.reset_writes_declaration()


def test_different_targets_each_logged() -> None:
    base = make_db_dir("wg_log2_")
    one, two = base / "meta.db", base / "account.db"
    wg.declare_writes_allowed(declared_by="tests/test_write_guard_log_dedupe")
    try:
        with _production_paths_are(one, two), _Recorder() as rec:
            wg.assert_writes_allowed(one, what="x")
            wg.assert_writes_allowed(two, what="y")
            wg.assert_writes_allowed(one, what="z")
            assert rec.count("production_write_allowed") == 2, rec.events
    finally:
        wg.reset_writes_declaration()


def test_redeclare_rearms_the_log() -> None:
    base = make_db_dir("wg_log3_")
    target = base / "meta.db"
    try:
        wg.declare_writes_allowed(declared_by="first")
        with _production_paths_are(target), _Recorder() as rec:
            wg.assert_writes_allowed(target)
            wg.declare_writes_allowed(declared_by="second")
            wg.assert_writes_allowed(target)
            assert rec.count("production_write_allowed") == 2, rec.events
            keys = list(wg._LOGGED_ALLOWED)
            assert len(keys) == 1, keys
            assert keys[0][0] == str(target) and "second" in keys[0][1], keys
    finally:
        wg.reset_writes_declaration()


def main() -> bool:
    print("=" * 64)
    print("write guard log dedupe (db/write_guard.py)")
    print("=" * 64)
    tests = [fn for name, fn in sorted(globals().items()) if name.startswith("test_")]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  ok    {fn.__name__}")
        except Exception:
            failed += 1
            print(f"  FAIL  {fn.__name__}")
            traceback.print_exc()
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return failed == 0


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
