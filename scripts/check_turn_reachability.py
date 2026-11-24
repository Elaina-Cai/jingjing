"""闸：回合入口的可达性与方向（M0）。

三条规则：
A 方向 —— `core/turn/**` 不许 import 旧主干（core.routing / core.speech /
  agents / web / scheduler）。入口只认 `core.turn.*` 与新包。
B 私有函数无孤儿 —— `core/turn/**` 里以下划线开头的模块级 `def` 必须在同包内
  被引用。公开函数是 API（会被 `core/routing/turn.py` 重导出、被测试调用），
  不参与孤儿判定。
C 入口唯一 —— `core/turn/entry.py` 必须有 `async def run_turn(`，且
  `core/turn/__init__.py` 必须导出它。

用法：`python -X utf8 -m scripts.check_turn_reachability`
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TURN_DIR = ROOT / "core" / "turn"
FORBIDDEN = ("core.routing", "core.speech", "agents", "web", "scheduler")
ENTRY_EXPORTS = ("run_turn",)


def _modules(root: Path) -> list[Path]:
    d = root / "core" / "turn"
    return sorted(p for p in d.rglob("*.py") if p.name != "__init__.py" or True)


def _imports(path: Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    except SyntaxError:
        return []
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            out.append(node.module)
    return out


def _tops(path: Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    except SyntaxError:
        return []
    return [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def scan(root: Path = ROOT) -> list[str]:
    problems: list[str] = []
    files = _modules(root)
    if not files:
        return [f"找不到 {root / 'core' / 'turn'}"]

    # A 方向
    for path in files:
        rel = path.relative_to(root).as_posix()
        for mod in _imports(path):
            if any(mod == f or mod.startswith(f + ".") for f in FORBIDDEN):
                problems.append(f"A {rel} 从入口层反向依赖旧主干：{mod}")

    # B 无孤儿
    sources = {p.relative_to(root).as_posix(): p.read_text(encoding="utf-8-sig") for p in files}
    for rel, src in sources.items():
        others = "\n".join(s for k, s in sources.items() if k != rel)
        me = rel.rsplit("/", 1)[-1]
        for name in _tops(Path(root) / rel):
            if name == "main" or not name.startswith("_"):
                continue
            used_here = src.count(name) > 1
            used_elsewhere = name in others
            exported = name in ENTRY_EXPORTS and me == "__init__.py"
            if not (used_here or used_elsewhere or exported):
                problems.append(f"B {rel} 里的私有 {name}() 没有任何调用点（孤儿）")

    # C 入口唯一
    entry = root / "core" / "turn" / "entry.py"
    if not entry.exists():
        problems.append("C 缺 core/turn/entry.py")
    else:
        src = entry.read_text(encoding="utf-8-sig")
        if "async def run_turn(" not in src:
            problems.append("C entry.py 里没有 async def run_turn(")
    init = root / "core" / "turn" / "__init__.py"
    if not init.exists():
        problems.append("C 缺 core/turn/__init__.py")
    else:
        isrc = init.read_text(encoding="utf-8-sig")
        for name in ENTRY_EXPORTS:
            if name not in isrc:
                problems.append(f"C __init__.py 没有导出 {name}")
    return problems


def main() -> int:
    problems = scan()
    if problems:
        print(f"FAILED: {len(problems)} 个问题")
        for item in problems:
            print(" -", item)
        return 1
    print(f"PASSED: 回合入口可达性 / 方向 / 唯一入口（扫 {len(_modules(ROOT))} 个文件）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
