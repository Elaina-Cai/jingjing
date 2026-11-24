# -*- coding: utf-8 -*-
"""体检：找出「看起来是测试、其实一条用例都没执行」的文件（假绿的极端形态）。

背景：`tests/` 是独立脚本套件（pyproject 把 pytest 配成不收集），所以一个
pytest 风格的 `def test_x(monkeypatch)` 文件被 `python tests/xxx.py` 跑起来时
**exit 0、零输出**，全量里也算 PASS —— 覆盖率凭空存在，删了代码也不红。

判据分两层：
1. 静态：文件里有 ``def test_``、没有 ``__main__``（排除自带 main 的脚本式测试）。
2. 实跑（``--run``）：子进程跑该文件，``stdout+stderr`` 为空且 ``exit 0`` ⇒ 空转。

用法::

    python scripts/audit_vacuous_tests.py            # 只做静态清单
    python scripts/audit_vacuous_tests.py --run      # 静态 + 实跑（会起 12 个子进程）
    python scripts/audit_vacuous_tests.py --run --json out.json

退出码：发现空转文件（``--run`` 下的空转，无 ``--run`` 时的静态命中）⇒ 1，否则 0。
**未接入全量门禁** —— 接不接由 lead 定。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
PY = sys.executable


def static_candidates() -> list[pathlib.Path]:
    hits = []
    for p in sorted(TESTS.glob("*.py")):
        if p.name.startswith("_"):
            continue
        src = p.read_text(encoding="utf-8", errors="replace")
        if "def test_" not in src:
            continue
        if "__main__" in src:
            continue
        hits.append(p)
    return hits


def run_one(p: pathlib.Path) -> dict:
    try:
        r = subprocess.run(
            [PY, str(p)], cwd=str(ROOT), capture_output=True, timeout=180
        )
        text = (r.stdout or b"").decode("utf-8", "replace") + (
            r.stderr or b""
        ).decode("utf-8", "replace")
        return {
            "file": p.name,
            "exit": r.returncode,
            "output_chars": len(text.strip()),
            "vacuous": r.returncode == 0 and not text.strip(),
        }
    except subprocess.TimeoutExpired:
        return {"file": p.name, "exit": "timeout", "output_chars": -1, "vacuous": False}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true", help="实跑一遍确认是否空转")
    ap.add_argument("--json", default="", help="结果另存 JSON 路径")
    args = ap.parse_args()

    cands = static_candidates()
    rows = [{"file": p.name, "exit": None, "output_chars": None, "vacuous": None} for p in cands]
    if args.run:
        rows = [run_one(p) for p in cands]

    vac = [r for r in rows if r["vacuous"]]
    print(f"静态候选 {len(rows)} 个" + ("（已实跑）" if args.run else "（未实跑，加 --run）"))
    for r in rows:
        tag = "空转" if r["vacuous"] else ("-" if r["vacuous"] is None else "有输出")
        print(f"  [{tag}] {r['file']:<46} exit={r['exit']} out={r['output_chars']}")
    if args.run:
        print(f"\n确认空转 {len(vac)} 个：" + (", ".join(r["file"] for r in vac) or "无"))
    if args.json:
        pathlib.Path(args.json).write_text(
            json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return 1 if (vac or (not args.run and rows)) else 0


if __name__ == "__main__":
    raise SystemExit(main())