"""启动账（`exitlog.record_start`）与「空过的时段」的行为断言。

为什么值得写（2026-11-06，主人「要」做第 1 条）：
2026-11-06 02:54 机器睡了 11.7 小时，账本里只留下 `engine rc=None orphan_cleaned`
与 `scheduler rc=3` —— 读起来像崩溃，真相是「她不在」。这里钉死三件事：
启动行带 `gap_hours`（上一次同类角色到现在空了多久）、启动行**不算退出**、
那张报表把空白单列、不当成一次崩溃。
"""

from __future__ import annotations

import importlib.util
import sys
import time

from core.infra import exitlog


def _load_report():
    path = "scripts/exit_report.py"
    spec = importlib.util.spec_from_file_location("exit_report_start_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


report = _load_report()

BASE = float(int(time.time()))


def test_the_first_start_has_no_gap(tmp_path):
    log = tmp_path / "exits.jsonl"
    got = exitlog.record_start("engine", path=log, now=BASE)
    assert got["kind"] == "start"
    assert got["why"] == "start"
    assert got["gap_hours"] is None
    assert got["ok"] is True


def test_the_gap_is_measured_from_the_previous_exit(tmp_path):
    log = tmp_path / "exits.jsonl"
    exitlog.record("engine", rc=None, why="orphan_cleaned", now=BASE - 11.7 * 3600, path=log)
    got = exitlog.record_start("engine", path=log, now=BASE)
    assert got["gap_hours"] == 11.7
    assert got["gap_from"]
    assert got["ok"] is True


def test_a_start_row_is_not_counted_as_an_exit(tmp_path):
    log = tmp_path / "exits.jsonl"
    exitlog.record("engine", rc=None, why="orphan_cleaned", now=BASE - 11.7 * 3600, path=log)
    exitlog.record_start("engine", path=log, now=BASE)
    got = exitlog.summary(hours=48.0, now=BASE + 60.0, path=log)
    assert got["total"] == 1
    assert got["by_why"] == {"orphan_cleaned": 1}
    assert got["by_role"] == {"engine": 1}
    assert got["starts"] == 1
    assert got["gaps"] == [{"role": "engine", "gap_hours": 11.7,
                            "ts": got["gaps"][0]["ts"], "gap_from": got["gaps"][0]["gap_from"]}]


def test_a_short_pause_is_not_a_gap(tmp_path):
    log = tmp_path / "exits.jsonl"
    exitlog.record("engine", rc=0, why="clean", now=BASE - 600.0, path=log)
    got = exitlog.record_start("engine", path=log, now=BASE)
    assert got["gap_hours"] == 0.17
    assert exitlog.summary(hours=48.0, now=BASE + 60.0, path=log)["gaps"] == []


def test_wrap_main_records_a_start_before_running(tmp_path):
    log = tmp_path / "exits.jsonl"
    out = exitlog.wrap_main(lambda: 7, role="desk", path=log)
    assert out == 7
    got = exitlog.summary(hours=48.0, now=BASE + 60.0, path=log)
    assert got["starts"] == 1
    assert got["total"] == 1
    assert got["by_why"] == {"clean": 1}


def test_the_report_lists_the_gap_instead_of_calling_it_a_crash(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    log = logs / "exits.jsonl"
    exitlog.record("engine", rc=None, why="orphan_cleaned", now=BASE - 11.7 * 3600,
                   detail="父进程已死", path=log)
    exitlog.record_start("engine", path=log, now=BASE)
    got = report.collect(logs_dir=logs, hours=48.0)
    assert got["by_why"] == {"orphan_cleaned": 1}
    assert len(got["rows"]) == 1
    assert got["gaps"][0]["gap_hours"] == 11.7
    assert got["gaps"][0]["role"] == "engine"
    text = report.render(got)
    assert "空了 11.7 小时" in text
    assert "她不在" in text

def _load_supervisor():
    import importlib.util as _il
    import sys as _sys

    path = "scripts/supervisor.py"
    spec = _il.spec_from_file_location("supervisor_start_under_test", path)
    mod = _il.module_from_spec(spec)
    _sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_the_supervisor_records_a_start_before_it_bails_out(monkeypatch):
    """全关状态下监管器一进来就退出，也必须留下一条 start —— 2026-11-06 的错就是把它写在函数末尾。"""
    sup = _load_supervisor()
    calls = []

    class _Fake:
        def record_start(self, role, **kw):
            calls.append(("start", role, kw))
            return {"ok": True}

        def record(self, role, **kw):
            calls.append(("exit", role, kw))
            return True

    monkeypatch.setattr(sup, "_exitlog", lambda: _Fake())
    monkeypatch.setattr(sup, "_all_stop_requested", lambda: True)
    monkeypatch.setattr(sup, "_defuse_stdout", lambda: None)
    monkeypatch.setattr(sup, "_parent_cmdline", lambda: "pytest")
    monkeypatch.setattr(sup, "_log", lambda msg: None)

    assert sup.main() == 0
    kinds = [c[0] for c in calls]
    assert kinds[0] == "start"
    assert calls[0][1] == "supervisor"
    assert ("exit", "supervisor") in [(c[0], c[1]) for c in calls]
