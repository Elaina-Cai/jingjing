"""跨窗去重的来源只有一个：台账（2026-10-28 ③ 修“堵水”）。"""

import inspect
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _src(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def test_run_loop_has_no_said_param():
    from loop import brain
    params = inspect.signature(brain.run_loop).parameters
    assert "tick_since" in params
    assert "said" not in params


def test_brain_reads_ledger_not_passed_strings():
    brain = _src("loop/brain.py")
    assert "ledger.said_since(" in brain
    assert "said[:4]" not in brain


def test_run_py_does_not_ship_strings():
    run = _src("loop/run.py")
    assert "_said" not in run
    assert "tick_since=" in run


def test_said_since_shape():
    from loop import ledger
    rows = ledger.said_since("")
    assert isinstance(rows, list)
    for r in rows:
        assert set(r) == {"target", "text"}


def test_said_since_future_is_empty():
    from loop import ledger
    assert ledger.said_since("2099-01-01 00:00:00") == []


def test_said_since_exclude():
    from loop import ledger
    all_rows = ledger.said_since("", limit=50)
    if not all_rows:
        return
    tgt = all_rows[0]["target"]
    left = ledger.said_since("", exclude=tgt, limit=50)
    assert all(r["target"] != tgt for r in left)
