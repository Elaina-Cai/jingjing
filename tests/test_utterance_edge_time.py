"""utterance_edges 的时间戳口径（2026-10-18 回填事故的回归测试）。

事故：回填 30 天历史时 ``build_edges`` 把 ``created_at`` 一律写成 ``datetime.now()``
⇒ 14170 条边全落在"今天 01:29"，按时间窗消费等于只有 3 小时数据。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # 直接 `python tests/x.py` 时把仓库根补上（阶段 1）
    sys.path.insert(0, str(ROOT))

from datetime import datetime

from core.terminal.utterance_edges import _row_time


def test_keeps_iso_string_time():
    assert _row_time({"created_at": "2026-09-18 08:46:03"}) == datetime(2026, 9, 18, 8, 46, 3)


def test_keeps_iso_t_and_zulu():
    assert _row_time({"created_at": "2026-09-18T08:46:03.500000Z"}) == datetime(
        2026, 9, 18, 8, 46, 3, 500000)


def test_keeps_datetime_object():
    stamp = datetime(2026, 10, 1, 12, 0, 0)
    assert _row_time({"created_at": stamp}) is stamp


def test_falls_back_to_now_when_missing_or_bad():
    before = datetime.now()
    got = _row_time({"created_at": ""})
    assert got >= before
    got2 = _row_time({"created_at": "not-a-time"})
    assert got2 >= before

if __name__ == "__main__":
    import sys as _sys
    from pathlib import Path as _Path

    for _p in (str(_Path(__file__).resolve().parent.parent), str(_Path(__file__).resolve().parent)):
        if _p not in _sys.path:
            _sys.path.insert(0, _p)
    from _mini_pytest import run_all as _run_all

    _sys.exit(0 if _run_all(globals()) else 1)