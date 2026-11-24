#!/usr/bin/env python
"""稳定链闸：新主干的「下一步」必须只有一条路（回合入口）。

查四件事（2026-10-21 立，起因：能力装在旧入口 generate_turn 上，真人消息却不走它）：

R1 定义唯一：`def _job_tick(` 只能出现在 core/routing/turn_pipeline.py。
R2 调用登记：生产代码里调用 `_job_tick(` 的文件必须在 ALLOWED_CALLERS 里；
   新增一条调用路径 = 当场红（逼人想清楚「这算不算第二条路」）。
R3 旧入口可识别：`def generate_turn(` 必须带 LEGACY 标记。
R4 轨迹可读：agents/conversational.py 里调用 `_job_tick(` 之后 900 字内必须出现
   `_turn.step("act"`（否则「这一步跑了吗」又变成猜）。

只读：不 import 任何 core 模块，纯文本 + ast。
"""
from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEF_FILE = "core/routing/turn_pipeline.py"
ALLOWED_CALLERS = {
    "core/routing/turn_pipeline.py",   # generate_turn：旧入口（LEGACY，待删）
    "agents/conversational.py",        # 真人消息真实路径（唯一生产调用点）
}
SKIP_DIRS = {"__pycache__", ".git", ".venv", ".venv_it", ".test_tmp", "data", "logs",
             "output", "backups", "node_modules"}


def project_files() -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if any(part in SKIP_DIRS for part in rel.split("/")):
            continue
        if rel.startswith("tests/") or rel.startswith("scripts/"):
            continue
        out.append(path)
    return sorted(out)


def main() -> int:
    problems: list[str] = []
    defs: list[str] = []
    callers: dict[str, int] = {}
    for path in project_files():
        rel = path.relative_to(ROOT).as_posix()
        src = path.read_text(encoding="utf-8-sig", errors="replace")
        if "def _job_tick(" in src:
            defs.append(rel)
        hits = sum(1 for line in src.splitlines()
                   if "_job_tick(" in line and not line.strip().startswith("#"))
        if hits:
            callers[rel] = hits
    if defs != [DEF_FILE]:
        problems.append(f"R1 定义必须唯一且只在 {DEF_FILE}，实际 {defs}")
    for rel in callers:
        if rel not in ALLOWED_CALLERS:
            problems.append(f"R2 {rel} 直接调用了 _job_tick（未登记；先问：这算第二条路吗？）")
    pipe = (ROOT / DEF_FILE).read_text(encoding="utf-8-sig", errors="replace")
    tree = ast.parse(pipe)
    legacy_ok = False
    lines = pipe.splitlines()
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == "generate_turn":
            head = ast.get_docstring(node) or ""
            window = "\n".join(lines[max(0, node.lineno - 4):node.lineno + 12])
            legacy_ok = "LEGACY" in head or "LEGACY" in window
    if not legacy_ok:
        problems.append("R3 generate_turn 未标记 LEGACY（旧入口必须可识别）")
    conv = (ROOT / "agents/conversational.py").read_text(encoding="utf-8-sig", errors="replace")
    idx = conv.find("_job_tick(")
    if idx < 0:
        problems.append("R4 agents/conversational.py 里找不到 _job_tick 调用（入口被搬走了？）")
    else:
        window = conv[idx:idx + 900]
        if '_turn.step("act"' not in window and "_turn.step('act'" not in window:
            problems.append('R4 _job_tick 之后 900 字内没有 _turn.step("act")（轨迹不可读）')
    if problems:
        for p in problems:
            print(f"FAILED: {p}")
        return 1
    print(f"PASSED: 回合入口唯一（定义 1 处 / 生产调用 {len(callers)} 处 / 旧入口标 LEGACY / act 轨迹可读）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
