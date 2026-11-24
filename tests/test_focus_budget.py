#!/usr/bin/env python
"""Regression: focus clustering must not pin the event loop.

2026-10-17 incident: the scheduler main loop was stuck for minutes inside
``core.context.threads`` clustering, whose innermost frame was
``core.timing._env_float``. Pinned here:

1. ``_beyond_gap`` takes the (constant) gap window as an argument, so one
   clustering pass calls ``core.timing.get()`` **once**.
2. The cluster scan uses an inverted 2-gram index (``_candidate_clusters``)
   instead of ``reversed(clusters)``; only clusters sharing a 2-gram can ever
   become a merge target, so the winner must be identical.
3. ``calculate_focus(..., deadline=...)`` truncates instead of running minutes;
   truncation never raises.
"""
from __future__ import annotations

import random
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for extra in (str(ROOT), str(ROOT / "scripts")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from core.context import threads  # noqa: E402

_VOCAB = (
    "概率论 老师 作业 报告 选课 通知 会议 天气 片儿川 论文 截图 视频 表情包 上课 "
    "考试 复习 图书馆 食堂 宿舍 游戏 加班 猫 狗 咖啡 熬夜 快递 生日 火锅 电影"
).split()


def _synthetic(count: int, *, seed: int = 7, spacing_options=(15, 45, 120, 600)) -> list[dict]:
    rng = random.Random(seed)
    base = datetime(2026, 10, 1, 9, 0, 0)
    stamp = base
    rows = []
    for index in range(count):
        stamp = stamp + timedelta(seconds=rng.choice(spacing_options))
        words = [rng.choice(_VOCAB) for _ in range(rng.randint(2, 5))]
        rows.append(
            {
                "event_id": f"e{index}",
                "event_type": "message",
                "content": " ".join(words),
                "created_at": stamp,
                "sender_id": f"p{rng.randint(0, 6)}",
                "sender_name": f"person{rng.randint(0, 6)}",
            }
        )
    return rows


def _rows(count: int, *, spacing_seconds: int = 120) -> list[dict]:
    base = datetime(2026, 10, 1, 12, 0, 0)
    return [
        {
            "event_id": f"e{index}",
            "event_type": "message",
            "content": f"row {index} unique alpha{index} beta{index}",
            "created_at": base + timedelta(seconds=spacing_seconds * index),
            "sender_id": f"p{index % 5}",
            "sender_name": f"person{index % 5}",
        }
        for index in range(count)
    ]


def _clusters(rows):
    keys = [threads._bigram_key(threads._text(row)) for row in rows]
    return threads._rollup(rows, keys, len(rows) - 1)


def test_gap_is_hoisted() -> bool:
    rows = _synthetic(200)
    calls = {"n": 0}
    real = threads._timing_qa_gap

    def counting() -> float:
        calls["n"] += 1
        return real()

    threads._timing_qa_gap = counting
    try:
        clusters, current = _clusters(rows)
    finally:
        threads._timing_qa_gap = real
    ok = calls["n"] == 1 and len(clusters) > 0
    print(f"[gap hoist] timing calls={calls['n']} clusters={len(clusters)} current={current} -> {ok}")
    return ok


def test_index_scan_matches_full_scan() -> bool:
    rows = _synthetic(400, seed=11)
    keys = [threads._bigram_key(threads._text(row)) for row in rows]
    indexed, indexed_current = threads._rollup(rows, keys, len(rows) - 1)
    real = threads._candidate_clusters
    threads._candidate_clusters = lambda clusters, postings, key: list(reversed(clusters))
    try:
        full, full_current = threads._rollup(rows, keys, len(rows) - 1)
    finally:
        threads._candidate_clusters = real
    same = (
        indexed_current == full_current
        and [c.members for c in indexed] == [c.members for c in full]
        and [c.rep_key for c in indexed] == [c.rep_key for c in full]
    )
    print(f"[index vs full scan] clusters indexed={len(indexed)} full={len(full)} identical={same}")
    return same


def test_explicit_gap_matches_default() -> bool:
    rows = _rows(80)
    keys = [threads._bigram_key(threads._text(row)) for row in rows]
    default_clusters, default_current = threads._rollup(rows, keys, len(rows) - 1)
    explicit_clusters, explicit_current = threads._rollup(
        rows, keys, len(rows) - 1, gap_seconds=60.0
    )
    ok = (
        default_current == explicit_current
        and [c.members for c in default_clusters] == [c.members for c in explicit_clusters]
    )
    print(f"[gap equivalence] clusters={len(default_clusters)} same={ok}")
    return ok


def test_no_timing_call_with_explicit_gap() -> bool:
    cluster = threads._Cluster(thread_id="t")
    cluster.last_at = datetime(2026, 10, 1, 12, 0, 0)
    stamp = datetime(2026, 10, 1, 12, 0, 30)

    def boom() -> float:
        raise AssertionError("_timing_qa_gap() must not be called when gap is given")

    real = threads._timing_qa_gap
    threads._timing_qa_gap = boom
    try:
        inside = threads._beyond_gap(cluster, stamp, 60.0)
        outside = threads._beyond_gap(cluster, stamp, 10.0)
    finally:
        threads._timing_qa_gap = real
    ok = inside is False and outside is True
    print(f"[explicit gap] inside={inside} outside={outside} -> {ok}")
    return ok


def test_large_window_is_fast() -> bool:
    rows = _synthetic(4000, seed=3)
    started = time.monotonic()
    clusters, _ = _clusters(rows)
    elapsed = time.monotonic() - started
    ok = elapsed < 3.0 and len(clusters) > 0
    print(f"[4000 rows] elapsed={elapsed:.3f}s clusters={len(clusters)} -> {ok}")
    return ok


def test_deadline_truncates_fast() -> bool:
    rows = _synthetic(1200, seed=5)
    started = time.monotonic()
    result = threads.calculate_focus(
        threads.FocusInput(chat_id="c", window_rows=rows), deadline=0.0
    )
    elapsed = time.monotonic() - started
    ok = isinstance(result, threads.FocusResult) and elapsed < 5.0
    print(f"[deadline] elapsed={elapsed:.3f}s degraded={result.degraded} -> {ok}")
    return ok


def test_deadline_absent_still_computes() -> bool:
    rows = _synthetic(80, seed=9)
    result = threads.calculate_focus(threads.FocusInput(chat_id="c", window_rows=rows))
    ok = isinstance(result, threads.FocusResult) and not result.degraded
    print(f"[no deadline] degraded={result.degraded} -> {ok}")
    return ok


def main() -> bool:
    checks = [
        test_gap_is_hoisted,
        test_index_scan_matches_full_scan,
        test_explicit_gap_matches_default,
        test_no_timing_call_with_explicit_gap,
        test_large_window_is_fast,
        test_deadline_truncates_fast,
        test_deadline_absent_still_computes,
    ]
    ok = True
    for check in checks:
        try:
            ok = check() and ok
        except Exception as exc:  # noqa: BLE001 - report and keep going
            print(f"FAIL: {check.__name__} raised {type(exc).__name__}: {exc}")
            ok = False
    if ok:
        print("focus budget guard: ok")
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
