"""闸：新主干不许静默返回（M0）。

规则：`core/turn/**`、`core/intake/**`、`core/verdict/**` 里，任何函数如果
出现 `return` / `return None`，函数体内**必须**有日志调用（`log.` / `logger.`）
或 `raise`——否则就是「悄悄什么都没发生」。

真机教训：`_job_tick` 一条日志都没有，于是「批了没生效」查了整整一轮。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET_DIRS = ("core/turn", "core/intake", "core/verdict")
LOG_ATTRS = ("log", "logger", "LOGGER")


def _has_log_or_raise(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Raise):
            return True
        if isinstance(child, ast.Call):
            fn = child.func
            if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
                if fn.value.id in LOG_ATTRS:
                    return True
    return False


def _silent_returns(fn: ast.AST) -> list[int]:
    lines: list[int] = []
    for child in ast.walk(fn):
        if isinstance(child, ast.Return):
            if child.value is None:
                lines.append(child.lineno)
            elif isinstance(child.value, ast.Constant) and child.value.value is None:
                lines.append(child.lineno)
    return lines


def scan(root: Path = ROOT) -> list[str]:
    problems: list[str] = []
    checked = 0
    for rel_dir in TARGET_DIRS:
        d = root / rel_dir
        if not d.exists():
            continue
        for path in sorted(d.rglob("*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8-sig"))
            except SyntaxError as exc:
                problems.append(f"{path.relative_to(root).as_posix()}: 语法错误 {exc}")
                continue
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                checked += 1
                lines = _silent_returns(node)
                if not lines:
                    continue
                if _has_log_or_raise(node):
                    continue
                rel = path.relative_to(root).as_posix()
                problems.append(
                    f"{rel}:{lines[0]} {node.name}() 静默返回 None（既没日志也没 raise）"
                )
    scan.checked = checked  # type: ignore[attr-defined]
    return problems


def main() -> int:
    problems = scan()
    checked = getattr(scan, "checked", 0)
    if problems:
        print(f"FAILED: {len(problems)} 处静默返回（查了 {checked} 个函数）")
        for item in problems:
            print(" -", item)
        return 1
    print(f"PASSED: 新主干无静默返回（查了 {checked} 个函数）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
