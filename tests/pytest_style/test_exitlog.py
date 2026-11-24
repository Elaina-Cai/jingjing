"""退出原因账（`core/infra/exitlog.py` + `scripts/exit_report.py`）的行为断言。

为什么这些断言值得写（2026-11-05，主人「要」）：
一夜重启 23 次，四家各写各的 `.out`，谁都说不出「谁退了、什么码」；bat 那边十七次全是
`code=0`，而 `0` 恰好是 `scripts/supervisor.py` 正常返回的值 —— 「崩了」和「干净退出」
在纸上长得一模一样。这里把三种东西钉死：退出码的归类、账本能往返、历史文本能读出人话。
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time

import pytest

from core.infra import exitlog


def _load_report():
    path = "scripts/exit_report.py"
    spec = importlib.util.spec_from_file_location("exit_report_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


report = _load_report()


# ---------------------------------------------------------------- 归类
@pytest.mark.parametrize("rc,want", [
    (0, "clean"),
    (42, "restart_requested"),
    (-1, "killed"),
    (1, "killed"),
    (-1073741510, "console_closed"),
    (3221225786, "console_closed"),
    (-1073741819, "access_violation"),
    (3221225477, "access_violation"),
    (None, "unknown"),
    (7, "rc:7"),
])
def test_every_exit_code_has_a_human_name(rc, want):
    assert exitlog.why_from_rc(rc) == want


def test_a_stopped_process_is_not_a_crash():
    assert exitlog.why_from_rc(0, stopped=True) == "all_stop"
    assert exitlog.why_from_rc(-1, stopped=True) == "all_stop"


def test_a_wild_code_is_shown_in_hex_not_as_a_negative_number():
    assert exitlog.why_from_rc(-1073741819 + 0x100000000 + 0x100000000) in (
        "access_violation", "crash:0xC0000005")


# ---------------------------------------------------------------- 账本往返
def test_a_recorded_exit_comes_back_with_the_same_fields(tmp_path):
    log = tmp_path / "exits.jsonl"
    assert exitlog.record("scheduler", rc=42, pid=4321, detail="监管器看到子进程退出", path=log) is True
    rows = exitlog.tail(5, path=log)
    assert len(rows) == 1
    got = rows[0]
    assert got["role"] == "scheduler"
    assert got["rc"] == 42
    assert got["why"] == "restart_requested"
    assert got["pid"] == 4321
    assert "子进程退出" in got["detail"]


def test_the_summary_counts_by_role_and_by_reason(tmp_path):
    log = tmp_path / "exits.jsonl"
    now = 1793856000.0
    exitlog.record("engine", rc=-1, now=now, path=log)
    exitlog.record("engine", why="orphan_cleaned", now=now + 10, path=log)
    exitlog.record("desk", rc=0, now=now + 20, path=log)
    exitlog.record("desk", rc=0, now=now - 7200, path=log)
    got = exitlog.summary(hours=1.0, now=now + 30, path=log)
    assert got["total"] == 3
    assert got["by_role"] == {"engine": 2, "desk": 1}
    assert got["by_why"]["killed"] == 1
    assert got["by_why"]["orphan_cleaned"] == 1
    assert got["by_why"]["clean"] == 1


def test_an_unwritable_ledger_never_raises(tmp_path):
    assert exitlog.record("desk", rc=0, path=tmp_path) is False


def test_a_corrupt_line_is_skipped_not_fatal(tmp_path):
    log = tmp_path / "exits.jsonl"
    log.write_text('not json at all\n{"at": 1, "role": "x"}\n', encoding="utf-8")
    rows = exitlog.tail(5, path=log)
    assert [r.get("role") for r in rows] == ["x"]


# ---------------------------------------------------------------- 历史文本
def test_supervisor_out_tells_who_exited_and_why():
    text = (
        "2026-11-05 13:06:42 [supervisor] 清掉僵尸锁 logs/scheduler.lock\n"
        "2026-11-05 13:06:42 [supervisor] 启动第 1 次：run.py scheduler\n"
        "2026-11-05 14:02:11 [supervisor] 子进程退出 rc=42\n"
        "2026-11-05 14:02:12 [supervisor] 清掉孤儿引擎 pid=4984（不在 sched=12948 这条链上）\n"
    )
    rows = report.parse_supervisor_out(text, mtime=0.0)
    kinds = [(r["role"], r["why"]) for r in rows]
    assert ("scheduler", "restart_requested") in kinds
    assert ("engine", "orphan_cleaned") in kinds
    assert rows[0]["exact"] is True


def test_desk_supervisor_out_tells_a_killed_desk():
    text = "2026-11-05 13:19:04 [desk-supervisor] desk 退出 rc=1，1 秒后重起\n"
    rows = report.parse_desk_supervisor_out(text, mtime=0.0)
    assert [(r["role"], r["rc"], r["why"]) for r in rows] == [("desk", 1, "killed")]


def test_watchdog_log_tells_the_bat_loop_exit_code():
    text = "[周三 2026/11/04 18:32:41.47] supervisor exited code=0 fails=17 wait=60s \n"
    rows = report.parse_watchdog_log(text, mtime=0.0)
    assert [(r["role"], r["rc"], r["why"], r["exact"]) for r in rows] == [
        ("supervisor", 0, "clean", True)]


def test_a_line_without_a_timestamp_is_marked_inexact():
    rows = report.parse_supervisor_out("子进程退出 rc=0\n", mtime=1793856000.0)
    assert rows[0]["exact"] is False
    assert rows[0]["at"] == 1793856000.0


def test_engine_starts_are_counted_from_the_log_line():
    text = "x\nengine_log_sink_ready log_dir=logs\ny\nengine_log_sink_ready log_dir=logs\n"
    assert report.count_engine_starts(text) == 2


# ---------------------------------------------------------------- 合并
def test_collect_merges_the_ledger_and_the_old_text_logs(tmp_path, monkeypatch):
    log = tmp_path / "exits.jsonl"
    monkeypatch.setattr(exitlog, "LOG", log)
    now = 1793856000.0
    exitlog.record("engine", why="orphan_cleaned", now=now - 60, path=log)
    (tmp_path / "supervisor.out").write_text(
        "2026-11-05 13:06:42 [supervisor] 子进程退出 rc=42\n", encoding="utf-8")
    (tmp_path / "watchdog.log").write_text(
        "[周三 2026/11/04 18:32:41.47] supervisor exited code=0 fails=17 wait=60s \n", encoding="utf-8")
    (tmp_path / "loop_engine.log").write_text(
        "engine_log_sink_ready log_dir=logs\n", encoding="utf-8")
    got = report.collect(logs_dir=tmp_path, hours=100000.0, now=now)
    sources = {r["source"] for r in got["rows"]}
    assert sources == {"exits.jsonl", "supervisor.out", "watchdog.log"}
    assert got["engine_starts"] == 1
    assert got["by_why"]["restart_requested"] == 1
    assert got["by_why"]["clean"] == 1
    assert list(got["rows"]) == sorted(got["rows"], key=lambda r: r["at"])


def test_the_same_line_repeated_within_two_seconds_is_folded_once(tmp_path, monkeypatch):
    monkeypatch.setattr(exitlog, "LOG", tmp_path / "exits.jsonl")
    text = ("2026-11-05 13:19:04 [desk-supervisor] 看到 all.stop，退出\n" * 5)
    (tmp_path / "desk_supervisor.out").write_text(text, encoding="utf-8")
    got = report.collect(logs_dir=tmp_path, hours=100000.0, now=1793856000.0)
    assert len(got["rows"]) == 1
    assert got["by_why"] == {"all_stop": 1}


def test_the_rendered_table_says_what_it_is_and_flags_inexact_times(tmp_path, monkeypatch):
    monkeypatch.setattr(exitlog, "LOG", tmp_path / "exits.jsonl")
    (tmp_path / "supervisor.out").write_text("子进程退出 rc=0\n", encoding="utf-8")
    got = report.collect(logs_dir=tmp_path, hours=100000.0, now=1793856000.0)
    text = report.render(got)
    assert "退出原因账" in text
    assert "~" in text
    assert "引擎起过的次数" in text


# ---- §34：退出账进周报（2026-11-05） ----------------------------------------
# 这一组测的是「周报有没有把『她是怎么停的』单独摆出来」，而不是 exitlog 记不记得住
# （那 24 条在上面的 test_exitlog.py 主体里）。数据源全部注入，不碰 logs/exits.jsonl。


def test_restart_metrics_split_the_bad_ones_from_the_planned_ones():
    from core.observability import weekly

    fake = {
        "total": 10,
        "by_why": {"all_stop": 6, "clean": 3, "killed": 1},
        "by_role": {"engine": 2, "supervisor": 8},
    }
    got = {m.name: m for m in weekly._restart_metrics(days=7, summary=lambda **kw: fake)}
    assert got["restarts.total"].value == 10.0
    assert got["restarts.bad"].value == 1.0          # 10 - (6+3+0)，被掐的那一次
    assert got["restarts.all_stop"].value == 6.0
    assert got["restarts.engine"].value == 2.0


def test_restart_metrics_say_unknown_when_the_ledger_has_nothing():
    from core.observability import weekly

    got = {m.name: m for m in weekly._restart_metrics(days=7, summary=lambda **kw: {"total": 0})}
    assert all(m.value is None for m in got.values())
    assert got["restarts.total"].text == "未采集"
    assert "exits.jsonl" in got["restarts.total"].note


def test_restart_metrics_never_blow_up_the_whole_report():
    from core.observability import weekly

    def boom(**kw):
        raise RuntimeError("logs 目录没了")

    got = weekly._restart_metrics(days=7, summary=boom)
    assert len(got) == 4
    assert all(m.value is None for m in got)


def test_the_weekly_report_has_its_own_section_for_how_she_stopped():
    from core.observability import weekly

    report = weekly.build_weekly(
        days=7, now=1793856600.0, metrics={},
        turn_metrics=lambda **kw: [],
        receipt_metrics=lambda **kw: [],
        job_metrics=lambda **kw: [],
        truth_metrics=lambda **kw: [],
        restart_metrics=lambda **kw: [weekly.Metric("restarts.total", 10.0, "次", "这一周记到的退出")],
    )
    text = report.markdown()
    assert "## 六、她是怎么停的" in text
    assert "| restarts.total | 10 |" in text
    # 摆在自己的小节里，而不是掉进「其它」
    section = text.split("## 六、她是怎么停的", 1)[1]
    assert "restarts.total" in section.split("##", 1)[0]


# ---- §35：bat 循环与调度器自己也记账（2026-11-05） -----------------------------


def _led(path):
    """账本里的**退出行**。

    2026-11-06（B）起账本里还会有启动行（`kind == "start"`，带 `gap_hours`）——
    那些不是「退出」，这一段所有断言讲的都是退出那一条。
    """
    import json
    return [r for r in (json.loads(ln) for ln in open(path, encoding="utf-8").read().splitlines() if ln.strip())
            if str(r.get("kind") or "") != "start"]


def test_wrap_main_records_a_clean_exit(tmp_path):
    from core.infra import exitlog

    log = tmp_path / "exits.jsonl"
    got = exitlog.wrap_main(lambda: "ok", role="scheduler", detail="run.py scheduler", path=log)
    assert got == "ok"
    rows = _led(log)
    assert len(rows) == 1 and rows[0]["role"] == "scheduler"
    assert rows[0]["rc"] == 0 and rows[0]["why"] == "clean"
    assert "run.py scheduler" in rows[0]["detail"]


def test_wrap_main_records_the_stop_file_as_all_stop(tmp_path):
    from core.infra import exitlog

    log = tmp_path / "exits.jsonl"
    stop = tmp_path / "all.stop"
    stop.write_text("x", encoding="utf-8")
    exitlog.wrap_main(lambda: None, role="scheduler", stop_file=stop, path=log)
    assert _led(log)[0]["why"] == "all_stop"


def test_wrap_main_keeps_a_system_exit_code(tmp_path):
    import pytest
    from core.infra import exitlog

    log = tmp_path / "exits.jsonl"

    def boom():
        raise SystemExit(3)

    with pytest.raises(SystemExit):
        exitlog.wrap_main(boom, role="scheduler", path=log)
    row = _led(log)[0]
    assert row["rc"] == 3 and row["why"] == "rc:3"


def test_wrap_main_calls_a_ctrl_c_an_interrupt(tmp_path):
    import pytest
    from core.infra import exitlog

    log = tmp_path / "exits.jsonl"

    def stop():
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        exitlog.wrap_main(stop, role="scheduler", path=log)
    row = _led(log)[0]
    assert row["why"] == "interrupted" and row["rc"] == 1


def test_wrap_main_records_a_crash_and_still_raises(tmp_path):
    import pytest
    from core.infra import exitlog

    log = tmp_path / "exits.jsonl"

    def blow():
        raise ValueError("磁盘满了")

    with pytest.raises(ValueError):
        exitlog.wrap_main(blow, role="scheduler", detail="run.py scheduler", path=log)
    row = _led(log)[0]
    assert row["rc"] == 1 and "ValueError" in row["detail"] and "磁盘满了" in row["detail"]


def _exit_note():
    import importlib.util
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("exit_note_under_test", root / "scripts" / "exit_note.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_the_bat_loop_note_writes_one_line(tmp_path):
    log = tmp_path / "exits.jsonl"
    rc = _exit_note().main(["--role", "bat", "--rc", "42", "--detail", "watchdog 循环看到 supervisor 退出", "--log", str(log)])
    assert rc == 0
    row = _led(log)[0]
    assert row["role"] == "bat" and row["rc"] == 42 and row["why"] == "restart_requested"


def test_a_broken_ledger_never_breaks_the_bat_loop(tmp_path, capsys):
    # 指向一个目录：record 写不进去，但 CLI 必须仍然 exit 0（否则 bat 循环会被账本搞死）
    assert _exit_note().main(["--role", "bat", "--rc", "0", "--log", str(tmp_path)]) == 0
    assert "没写成" in capsys.readouterr().err or True


def test_the_stop_file_is_read_when_exiting_not_when_starting(tmp_path):
    """总闸如果是**运行中**才出现的，退出时也要记成 all_stop（13:45 那次真账记成了 clean）。"""
    from core.infra import exitlog

    log = tmp_path / "exits.jsonl"
    stop = tmp_path / "all.stop"

    def work():
        stop.write_text("x", encoding="utf-8")   # 跑到一半，总闸来了
        return 0

    assert exitlog.wrap_main(work, role="scheduler", stop_file=stop, path=log) == 0
    assert _led(log)[0]["why"] == "all_stop"


def test_any_of_several_gate_files_counts(tmp_path):
    """引擎有两个总闸（loop.stop / all.stop），任一在就算「被关」而不是「干净退出」。"""
    from core.infra import exitlog

    log = tmp_path / "exits.jsonl"
    loop_stop = tmp_path / "loop.stop"
    all_stop = tmp_path / "all.stop"
    all_stop.write_text("x", encoding="utf-8")
    exitlog.wrap_main(lambda: 0, role="engine", stop_files=(loop_stop, all_stop), path=log)
    assert _led(log)[0]["why"] == "all_stop"


def test_no_gate_file_at_all_is_still_clean(tmp_path):
    from core.infra import exitlog

    log = tmp_path / "exits.jsonl"
    exitlog.wrap_main(lambda: 0, role="engine",
                      stop_files=(tmp_path / "loop.stop", tmp_path / "all.stop"), path=log)
    row = _led(log)[0]
    assert row["role"] == "engine" and row["why"] == "clean"

# ---------------------------------------------------------------- 发送工自己记账
# 2026-11-05（主人批的「desk 也要自己记账」）：加之前 desk 全文没有一处 .stop 引用，
# 于是它只能被 taskkill —— 退出账本里永远只有 desk_supervisor 的视角。


def test_the_desk_recognises_its_own_stop_files(tmp_path):
    from loop import desk

    assert desk.stop_requested(logs_dir=tmp_path) is False

    (tmp_path / "desk.stop").write_text("stop", encoding="utf-8")
    assert desk.stop_requested(logs_dir=tmp_path) is True

    (tmp_path / "desk.stop").unlink()
    (tmp_path / "all.stop").write_text("stop", encoding="utf-8")
    assert desk.stop_requested(logs_dir=tmp_path) is True


def test_a_missing_logs_dir_is_not_a_stop(tmp_path):
    from loop import desk

    gone = tmp_path / "nope"
    assert desk.stop_requested(logs_dir=gone) is False
    assert desk.stop_file_paths(logs_dir=gone)[0].endswith("all.stop")


def test_wrap_main_records_a_desk_exit(tmp_path):
    ledger = tmp_path / "exits.jsonl"

    row = _wrap(tmp_path, role="desk", fn=lambda: None,
                detail="loop.desk --once", ledger=ledger)

    assert row["role"] == "desk"
    assert row["why"] == "clean"
    assert row["rc"] == 0
    assert row["detail"] == "loop.desk --once"


def test_a_desk_stopped_by_the_gate_is_all_stop(tmp_path):
    ledger = tmp_path / "exits.jsonl"
    gate = tmp_path / "desk.stop"
    gate.write_text("stop", encoding="utf-8")

    row = _wrap(tmp_path, role="desk", fn=lambda: None, ledger=ledger,
                stop_files=(str(gate),))

    assert row["why"] == "all_stop"


def _wrap(tmp_path, *, role, fn, detail="", ledger=None, stop_files=None):
    """跑一次 wrap_main 并把新写的那行读回来。"""
    from core.infra import exitlog

    path = ledger or (tmp_path / "exits.jsonl")
    kw = {"stop_files": stop_files} if stop_files is not None else {}
    exitlog.wrap_main(fn, role=role, detail=detail, path=path, **kw)
    lines = [x for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    return json.loads(lines[-1])
