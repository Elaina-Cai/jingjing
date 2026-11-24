# -*- coding: utf-8 -*-
"""门禁的「入口」判据必须真的挡人（2026-10-29，DSH-019 续）。

要守的三件事：
1. `tests/test_*.py` 里不许再有「有 `def test_`、却没有入口」的文件 ——
   独立脚本 runner 跑这种文件是 exit 0、零输出，报表记 PASS，实际零保护。
2. `scripts/run_all_tests.py::_has_entry_point` 的判据本身要对：有 `__main__` /
   `run_all(` 才算入口。
3. 跳过标记（`SKIP_MARK`）与 `tests/_skip.py::MARK` 必须同一个字符串 ——
   两处各写一份就会一边打印一边数不到。
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
TESTS = ROOT / "tests"
RUNNER = ROOT / "scripts" / "run_all_tests.py"


def _load_runner():
    spec = importlib.util.spec_from_file_location("run_all_tests_under_test", RUNNER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_predicate_matches_the_two_real_entry_styles(tmp_path):
    runner = _load_runner()
    has_main = tmp_path / "test_a.py"
    has_main.write_text("def test_x():\n    assert True\n\nif __name__ == \"__main__\":\n    test_x()\n", encoding="utf-8")
    has_run_all = tmp_path / "test_b.py"
    has_run_all.write_text("from _mini_pytest import run_all\n\ndef test_x():\n    assert True\n\nrun_all(globals())\n", encoding="utf-8")
    stray = tmp_path / "test_c.py"
    stray.write_text("import pytest\n\ndef test_x():\n    assert True\n", encoding="utf-8")

    assert runner._has_entry_point(has_main) is True
    assert runner._has_entry_point(has_run_all) is True
    assert runner._has_entry_point(stray) is False


def test_no_stray_pytest_style_file_in_tests_root():
    runner = _load_runner()
    stray = sorted(
        p.name
        for p in TESTS.glob("test_*.py")
        if "def test_" in p.read_text(encoding="utf-8", errors="replace")
        and not runner._has_entry_point(p)
    )
    assert stray == [], (
        "这些文件有 def test_ 却没有入口，runner 会记 NO-RUN："
        f"{stray}；pytest 风格请放 tests/pytest_style/（由 scripts/run_pytest_suite.py 跑）"
    )


def test_skip_mark_is_the_same_string_in_both_places():
    runner = _load_runner()
    sys.path.insert(0, str(TESTS))
    try:
        import _skip
    finally:
        sys.path.pop(0)
    assert runner.SKIP_MARK == _skip.MARK, (
        f"runner 的 SKIP_MARK={runner.SKIP_MARK!r} 与 tests/_skip.py 的 MARK={_skip.MARK!r} 不一致"
    )
