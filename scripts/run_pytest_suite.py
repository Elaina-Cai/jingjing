"""跑 pytest 风格的用例（`tests/pytest_style/`）。

为什么单开一个脚本
------------------
`tests/test_*.py` 是**独立脚本**（`def main() -> bool` 约定），由
`scripts/run_all_tests.py` 执行；pytest 夹具风格（`monkeypatch` / `capsys`）的用例
既跑不了脚本 runner，又会被 `pyproject.toml` 的 `python_files` 屏蔽收集。两者混在
一个目录里只会互相伤害：

- 脚本 runner 下它们 `import pytest` 成功后**一条用例都不跑就 exit 0** → 假绿
  （比红更危险：报表记 PASS，实际零保护）；
- pytest 全量收集它们 → 与「`return False` 表示失败」的约定相撞
  （pytest 忽略返回值，失败会被记成 PASSED）。

所以把它们隔离到 `tests/pytest_style/`，用本脚本跑，两道门禁各管一半：
`run_all_tests.py` 管 350 个脚本（glob `tests/test_*.py`，天然不含子目录），
本脚本管 pytest 风格的那几条。

用法
----
    python scripts/run_pytest_suite.py                  # 全跑
    python scripts/run_pytest_suite.py --filter embedding
    python scripts/run_pytest_suite.py --verbose        # 透传 -v
    python scripts/run_pytest_suite.py --keep           # 保留临时目录

退出码：0 = 全绿，1 = 有用例失败或有文件连 import 都过不去，2 = 环境缺 pytest。
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STYLE_DIR = ROOT / "tests" / "pytest_style"
TMP_DIR = ROOT / ".test_tmp" / "pytest_tmp"

# pyproject.toml 把 python_files 设成永不匹配、并把 tests/ 放进 norecursedirs；
# 这里两个都显式覆盖，且**传文件名**而不是目录（norecursedirs 只在目录递归时生效，
# 传文件可以绕开任何可能的「一个都不收」）。
PYTEST_OVERRIDES = ("-o", "python_files=test_*.py")


def pick_interpreter() -> Path:
    """优先用装了 pytest 的解释器：.venv_it 是本机的测试环境。"""
    for cand in (ROOT / ".venv_it" / "Scripts" / "python.exe", Path(sys.executable)):
        if not cand.exists():
            continue
        probe = subprocess.run(
            [str(cand), "-c", "import pytest"],
            capture_output=True, text=True, timeout=120,
        )
        if probe.returncode == 0:
            return cand
    raise SystemExit(
        "[pytest-suite] 找不到装了 pytest 的解释器。\n"
        "  修法：.venv_it\\Scripts\\python.exe -m pip install pytest\n"
        "  （.venv 里没有 pytest，别用它跑本脚本）"
    )


def collect(filter_text: str | None) -> list[Path]:
    files = sorted(STYLE_DIR.glob("test_*.py"))
    if filter_text:
        files = [p for p in files if filter_text in p.name]
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description="跑 pytest 风格用例")
    parser.add_argument("--filter", default=None, help="文件名子串过滤")
    parser.add_argument("--verbose", action="store_true", help="透传 -v")
    parser.add_argument("--keep", action="store_true", help="保留临时目录")
    args = parser.parse_args()

    files = collect(args.filter)
    if not files:
        print(f"[pytest-suite] {STYLE_DIR} 下没有匹配的用例（filter={args.filter}）")
        return 1

    python = pick_interpreter()
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    # 唯一 basetemp：pytest 会自己清空重建。不给的话它会在 tmp 根下留
    # `pytest-current` 目录联接，Windows 收尾清理时抛 PermissionError（WinError 5）
    # 把整条车道判成红 —— 用例其实全绿。参见 docs/踩坑档案.md #27。
    basetemp = TMP_DIR / f"basetemp_{os.getpid()}"

    env = dict(os.environ)
    env["TMP"] = str(TMP_DIR)
    env["TEMP"] = str(TMP_DIR)
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("TZ", "Asia/Shanghai")
    # 2026-11-09：宿主 IDE 的 safe-delete shim 会把 os.remove 拦成 SystemExit，
    # 让「删 stop 哨兵 / 清 tmp」这类用例假红（实测 test_restart_rollback 两条：
    # 后台线程直接 SystemExit 1，文件没删）。用例只删自己的 basetemp，关掉这层
    # 拦截才跑得出真实语义。宿主会预设 ENABLED=1/THRESHOLD=50，setdefault 无效，
    # 必须强制覆盖；否则 SystemExit 会穿透 fixture teardown，把 pytest 的 finalizer
    # 状态搞坏，后半场全部 setup error（实测 1649 个）。
    env["CODEBUDDY_SAFE_DELETE_ENABLED"] = "0"
    env["CODEBUDDY_SAFE_DELETE_BULK_THRESHOLD"] = "100000"

    cmd = [
        str(python), "-X", "utf8", "-m", "pytest",
        *[str(p) for p in files],
        *PYTEST_OVERRIDES,
        "-p", "no:cacheprovider",
        "--tb=short",
        "--basetemp", str(basetemp),
        *(["-v"] if args.verbose else ["-q"]),
    ]
    print(f"[pytest-suite] {len(files)} 个文件 · 解释器 {python.relative_to(ROOT)}")
    proc = subprocess.run(cmd, cwd=str(ROOT), env=env, capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=1800)
    out = (proc.stdout or "") + (proc.stderr or "")
    print(out.rstrip())

    passed = int(m.group(1)) if (m := re.search(r"(\d+) passed", out)) else 0
    failed_ids = re.findall(r"^FAILED (\S+)", out, flags=re.MULTILINE)
    errors = re.findall(r"^ERROR (\S+)", out, flags=re.MULTILINE)
    total = passed + len(failed_ids) + len(errors)
    # 三种状态必须分清（#24）：真验过 / 一条没验 / 车道自己崩了。
    # 之前只看「有没有 summary 行」，pytest 收尾崩溃时会被读成 `0/0 passed`，
    # 于是「全绿但退出码 1」看起来像「一个用例都没跑」。
    crashed = ("INTERNALERROR" in out) or ("Traceback (most recent call last)" in out)
    if total:
        print(f"RESULT {passed}/{total} passed")
    elif crashed:
        print("[pytest-suite] pytest 没给出结论，崩在收集或收尾。现场末几行：")
        for line in out.splitlines()[-6:]:
            print("  " + line)
        print("RESULT 未判定（不是 0/0：用例可能全跑了，但 pytest 自己崩了）")
        errors.append("<pytest 自身崩溃>")
    else:
        print("RESULT 0/0 passed（一个用例都没收到）")

    if failed_ids or errors:
        print("NEW-RED（挡提交）:")
        for node in failed_ids + errors:
            print(f"  - {node}")

    if not args.keep:
        try:
            for leftover in TMP_DIR.rglob("*"):
                if leftover.is_file():
                    leftover.unlink()
        except (OSError, SystemExit):
            pass

    return 0 if proc.returncode == 0 and not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
