"""闸不能悄悄失效：last_say_at 必须真的读到 sent 行（2026-10-24 三次静默失效的教训）。

2026-11-02 修：这条用例原来 monkeypatch 的是 `ledger.FILE`，而流水行的 I/O 早已
下移到 `core/infra/actions.py`（PIT-111），`FILE` 这个属性根本不存在了 —— 于是它一直
在读**当天真实账本**，`last_say_at("wxid_a")` 当然是 0，长期挂在 known_red 里当"结构漂移"。
改成钉真缝隙 `actions.DIR`：临时目录接上，测的才是判据本身。
"""
from __future__ import annotations

import os
import shutil
import sys
import time

# 单独跑（python tests/xxx.py）也要能找到仓库根与 tests/ 下的 _tempdirs
_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.dirname(_HERE), _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _tempdirs import make_db_dir  # noqa: E402

from core.infra import actions  # noqa: E402
from loop import ledger  # noqa: E402


def test_last_say_at_reads_sent_rows(monkeypatch=None) -> None:
    old = actions.DIR
    tmp = make_db_dir("lastsay_")
    try:
        actions.DIR = tmp
        ledger.append_receipt(act="say", target="wxid_a", why="测试", result="sent")
        got = ledger.last_say_at("wxid_a")
        assert got > 0, "last_say_at 返回 0，说明又被宽 except 吞了"
        assert abs(time.time() - got) < 120
        # 目标不同 / 被拦的不算
        assert ledger.last_say_at("wxid_b") == 0.0
        ledger.append_receipt(act="say", target="wxid_c", why="测试", result="blocked")
        assert ledger.last_say_at("wxid_c") == 0.0
    finally:
        actions.DIR = old
        shutil.rmtree(tmp, ignore_errors=True)


def test_said_this_turn_splits_turns() -> None:
    """同一回合 / 换回合必须分开 —— 多气泡放行靠的就是这一分（见 tests/test_say_pace_turn.py）。"""
    old = actions.DIR
    tmp = make_db_dir("lastsay_")
    try:
        actions.DIR = tmp
        ledger.append_receipt(act="say", target="wxid_a", result="sent", turn_id="t1")
        assert ledger.said_this_turn("wxid_a", "t1") is True
        assert ledger.said_this_turn("wxid_a", "t2") is False
        # 被拦的那条不算「说过」
        ledger.append_receipt(act="say", target="wxid_a", result="blocked", turn_id="t2")
        assert ledger.said_this_turn("wxid_a", "t2") is False
    finally:
        actions.DIR = old
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    import sys as _sys
    from pathlib import Path as _Path

    for _p in (str(_Path(__file__).resolve().parent.parent), str(_Path(__file__).resolve().parent)):
        if _p not in _sys.path:
            _sys.path.insert(0, _p)
    from _mini_pytest import run_all as _run_all

    _sys.exit(0 if _run_all(globals()) else 1)
