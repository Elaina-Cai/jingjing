"""扫出「会开 SQLite 但没钉 TEMP」的测试文件（`tests/_tempdirs.py` 纪律）。

为什么需要它
------------
`tests/_tempdirs.py` 的 docstring 记着 2026-10-01 那次教训：子进程 TEMP 不可写时
SQLite 的 `GetTempPath` 直接 `SQLITE_CANTOPEN`，报成
``OperationalError: unable to open database file``，一批测试同时红而代码本身没毛病。
2026-10-17 `tests/test_scheduler_jobs.py` 就是这条（2 failed / 8 passed），修法是
``import _tempdirs`` —— 它把 TEMP/TMP/TMPDIR/SQLITE_TMPDIR 与 ``tempfile.tempdir``
一起钉到 ``ROOT/.test_tmp``。

本仓**不是** pytest 套件（`scripts/run_all_tests.py` 逐文件起子进程），所以没有
conftest 可以统一兜底，只能逐文件自查。本脚本只报告，不改文件。

用法::

    .\\.venv_it\\Scripts\\python.exe -u scripts\\audit_tempdirs_discipline.py
    .\\.venv_it\\Scripts\\python.exe -u scripts\\audit_tempdirs_discipline.py --strict   # 有违规则 exit 1
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"

# 会真正落一个 SQLite 文件（或跑迁移）的写法
_SQLITE_WRITE = re.compile(
    r"sqlite3\.connect\(|create_engine\(|init_meta_db\(|run_migrations\(|MetaRepositoryManager\("
)
_PINNED = re.compile(r"(^|\n)\s*(import _tempdirs|from _tempdirs import)")


def scan() -> tuple[int, list[str]]:
    files = sorted(TESTS.glob("test_*.py"))
    offenders: list[str] = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if _SQLITE_WRITE.search(text) and not _PINNED.search(text):
            offenders.append(path.name)
    return len(files), offenders


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true", help="有违规时 exit 1")
    args = parser.parse_args(argv)

    total, offenders = scan()
    print(f"tests 文件数 = {total}")
    print(f"会开 SQLite 但未 import _tempdirs = {len(offenders)}")
    for name in offenders:
        print(f"  - {name}")
    if offenders:
        print(
            "\n处置：该文件若因 SQLITE_CANTOPEN 变红，在 sys.path 之后加 "
            "`import _tempdirs  # noqa: E402,F401`。"
        )
    if args.strict and offenders:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())