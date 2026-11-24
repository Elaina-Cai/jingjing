"""自发闸被拦时不许每轮轮询都写一条旁证。

2026-11-22 实测：引擎空转、她 37 小时没开口，`blocked.jsonl` 一天 1,476 条全是
`spont:streak=7`，还在每秒级地长。同一停因要在时间窗内只记一次。
"""
from __future__ import annotations

from loop import run


def _reset():
    run._SPONT_BLOCK_LOG.update({"key": None, "at": 0.0})


def test_same_reason_is_logged_once_per_window():
    _reset()
    assert run._spont_block_due(("streak=7", 7), 1000.0) is True
    assert run._spont_block_due(("streak=7", 7), 1001.0) is False
    assert run._spont_block_due(("streak=7", 7), 1599.0) is False
    assert run._spont_block_due(("streak=7", 7), 1600.0) is True


def test_a_changed_reason_is_logged_immediately():
    _reset()
    assert run._spont_block_due(("streak=7", 7), 1000.0) is True
    assert run._spont_block_due(("streak=8", 8), 1001.0) is True
    assert run._spont_block_due(("streak=8", 8), 1002.0) is False
