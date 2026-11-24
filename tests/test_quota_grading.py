# -*- coding: utf-8 -*-
"""额度分级（``core/rules/quota_gate.py``）用例：PIT-040 的判据。

守五件事
--------
① 分级边界：``limit`` 取 0/1/2/3/10/20，每个 ``used`` 的等级逐个断言——
   80% 那一档必须**真的存在**（limit=3 时 used=2 是 warn，不是直接 blocked）；
② 跨过 80% **当天只说一次**（同一进程连判 5 次只有 1 次出声；hour 与 day 各一次）；
③ 换一天后重新说得出来；上限改了（同一 key）也重新说一声；
④ 落盘坏掉不影响返回等级（坏文件由 ``jsonio`` 备份成 ``.bad-*``）；
⑤ 结构锁：阈值只在 ``quota_gate.py`` 出现一次，两个调用点都走 ``quota_gate.observe``
   —— 以后再想「顺手写个 0.8」或「再写一遍 >= 上限」会红。

Run directly:
    python tests/test_quota_grading.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ROOT = Path(__file__).resolve().parent.parent

from core.rules import quota_gate                      # noqa: E402

CASES = [
    # limit, used, level, warn_at
    (0, 3, "unlimited", 0),
    (1, 0, "ok", 1),
    (1, 1, "blocked", 1),
    (2, 0, "ok", 1),
    (2, 1, "warn", 1),
    (2, 2, "blocked", 1),
    (3, 1, "ok", 2),
    (3, 2, "warn", 2),
    (3, 3, "blocked", 2),
    (10, 7, "ok", 8),
    (10, 8, "warn", 8),
    (10, 9, "warn", 8),
    (10, 10, "blocked", 8),
    (20, 15, "ok", 16),
    (20, 16, "warn", 16),
    (20, 19, "warn", 16),
    (20, 20, "blocked", 16),
]


def _tmp():
    return Path(tempfile.mkdtemp(prefix="quota_gate_"))


def test_grading_table() -> None:
    for limit, used, level, at in CASES:
        assert quota_gate.warn_at(limit) == at, (limit, quota_gate.warn_at(limit), at)
        assert quota_gate.classify(used, limit) == level, (limit, used, level)


def test_remaining_and_text() -> None:
    assert quota_gate.remaining(3, 20) == 17
    assert quota_gate.remaining(5, 0) is None          # 不限不是 0 次
    assert "还剩 1" in quota_gate.text("day", 2, 3)


def test_once_per_day() -> None:
    d = _tmp()
    try:
        heard = []
        sink = lambda ev, payload: heard.append((ev, payload["window"], payload["level"]))
        now = datetime(2026, 10, 28, 12, 0)
        for _ in range(5):
            assert quota_gate.observe("k1", "day", 8, 10, now=now, path=d / "m.json", sink=sink) == "warn"
        assert [h[0] for h in heard] == ["quota_near_limit"], heard
        # hour 是另一个组合，各自说一次
        quota_gate.observe("k1", "hour", 10, 10, now=now, path=d / "m.json", sink=sink)
        assert len(heard) == 2 and heard[1][0] == "quota_exhausted", heard
        # 另一个 key 各说各的
        quota_gate.observe("k2", "day", 8, 10, now=now, path=d / "m.json", sink=sink)
        assert len(heard) == 3, heard
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_new_day_announces_again() -> None:
    d = _tmp()
    try:
        heard = []
        sink = lambda ev, payload: heard.append(ev)
        day1 = datetime(2026, 10, 28, 12, 0)
        day2 = datetime(2026, 10, 29, 0, 5)
        for now in (day1, day1, day2, day2):
            quota_gate.observe("k1", "day", 8, 10, now=now, path=d / "m.json", sink=sink)
        assert len(heard) == 2, heard          # 每天一次，换天再响
        import json
        state = json.loads((d / "m.json").read_text(encoding="utf-8"))
        assert state["day"] == "2026-10-29" and len(state["seen"]) == 1, state
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_limit_change_announces_again() -> None:
    d = _tmp()
    try:
        heard = []
        sink = lambda ev, payload: heard.append(payload["limit"])
        now = datetime(2026, 10, 28, 12, 0)
        quota_gate.observe("k1", "day", 8, 10, now=now, path=d / "m.json", sink=sink)
        quota_gate.observe("k1", "day", 16, 20, now=now, path=d / "m.json", sink=sink)
        assert heard == [10, 20], heard        # 上限改了要说新口径
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_bad_marker_does_not_break() -> None:
    d = _tmp()
    (d / "m.json").write_text("{ 这不是 json", encoding="utf-8")
    try:
        heard = []
        sink = lambda ev, payload: heard.append(ev)
        level = quota_gate.observe("k1", "day", 10, 10, now=datetime(2026, 10, 28, 12),
                                   path=d / "m.json", sink=sink)
        assert level == "blocked" and heard == ["quota_exhausted"], (level, heard)
        assert list(d.glob("m.json.bad-*")), list(d.iterdir())   # 坏文件被留证
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_one_threshold_only() -> None:
    import ast
    gate_src = (ROOT / "core" / "rules" / "quota_gate.py").read_text(encoding="utf-8")
    doc = ast.get_docstring(ast.parse(gate_src)) or ""
    code = gate_src.replace(doc, "")      # docstring 里故意写着反例 "limit * 0.8"，不算代码
    assert code.count("0.8") == 1, code.count("0.8")
    assert "WARN_RATIO = 0.8" in code
    for rel in ("core/rules/engine.py", "core/terminal/proactive_service.py"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert "0.8" not in src, rel                       # 阈值不许有第二份
        assert "quota_gate.observe(" in src, rel           # 都走同一个入口
    engine = (ROOT / "core/rules/engine.py").read_text(encoding="utf-8")
    assert "used >= quota.per_hour" not in engine
    assert "used >= quota.per_day" not in engine
    pro = (ROOT / "core/terminal/proactive_service.py").read_text(encoding="utf-8")
    assert "hour_count >= max_hour" not in pro
    assert "day_count >= max_day" not in pro


if __name__ == "__main__":
    for fn in (
        test_grading_table,
        test_remaining_and_text,
        test_once_per_day,
        test_new_day_announces_again,
        test_limit_change_announces_again,
        test_bad_marker_does_not_break,
        test_one_threshold_only,
    ):
        fn()
        print("ok", fn.__name__)
    print("ALL OK")
