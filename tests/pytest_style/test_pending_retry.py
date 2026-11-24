"""待办窗口的重试：别让「还有窗口没轮到」变成永久悬空。

旧逻辑：`last_fp = "__pending__"` 想让下一圈重拍，可下一圈的第一句就是
`if stamp == last_stamp ...: continue` —— 待办窗口本身不改世界，所以那个比较
永远轮不到，重试等于不存在。10-30 23:02 那句「在吗」压在第 4 个窗口上，悬了一夜。
"""
from __future__ import annotations

import pytest

from loop import run as R


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(R, "_PEND_TRIES", [0])
    monkeypatch.setattr(R, "_PEND_LAST", [0])
    monkeypatch.setattr(R, "_PEND_GAP_MS", 20_000)
    yield


def test_no_pending_means_no_extra_tick():
    assert R._pend_due(at_ms=10 ** 9) is False


def test_pending_but_still_in_backoff_waits():
    R._PEND_TRIES[0] = 1
    R._PEND_LAST[0] = 1_000
    assert R._pend_due(at_ms=1_000 + 19_999) is False


def test_pending_and_past_backoff_is_due():
    R._PEND_TRIES[0] = 1
    R._PEND_LAST[0] = 1_000
    assert R._pend_due(at_ms=1_000 + 20_000) is True


def test_idle_branch_actually_consults_the_retry():
    """源码级锁：idle 早退条件必须带上 `not _pend_due()`，否则重试仍是死代码。"""
    import inspect

    src = inspect.getsource(R.main)
    assert "and not _pend_due()" in src
    # 2026-11-09：早退条件 11-07 又加了到点复核（`not _goal_reviews()`）。
    # 不再钉整行字面量（每加一个条件就假红），改成"这一行里两个条件都在"。
    early_exit = next(line for line in src.splitlines() if "if stamp == last_stamp" in line)
    assert "not _pend_due()" in early_exit
    assert "not _goal_reviews" in early_exit


def test_pending_marks_the_backoff_clock():
    src = open(R.__file__, encoding="utf-8").read()
    i = src.index("_PEND_TRIES[0] += 1")
    assert "_PEND_LAST[0] = duration.now_ms()" in src[i:i + 200]
