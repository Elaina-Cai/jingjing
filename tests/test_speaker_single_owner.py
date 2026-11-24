#!/usr/bin/env python
"""WP-03 guard: speaker resolution has exactly one owner.

`core/terminal/speaker.py::resolve_speaker_from_row` is the only place allowed
to turn a message/window row into a speaker string.  `room_state._speaker` and
`threads._speaker` must be thin delegates (no local ``for key in
("sender_name", "sender_id", "speaker")`` loop).  `memory_projection._speaker`
is allowlisted because it maps a different input type (``Event``) straight to
``resolve_speaker``.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OWNER = "core/terminal/speaker.py"
DELEGATES = ("core/context/room_state.py", "core/context/threads.py")
LOCAL_ALLOWLIST = {
    "core/context/room_state.py": "delegates to resolve_speaker_from_row",
    "core/context/threads.py": "delegates to resolve_speaker_from_row",
    "core/terminal/memory_projection.py": "Event -> resolve_speaker (different input)",
}
SCAN_DIRS = ("core", "agents", "scheduler")


def _functions(tree: ast.AST, name: str):
    return [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name]


def _calls(node: ast.AST, name: str) -> bool:
    for n in ast.walk(node):
        if not isinstance(n, ast.Call):
            continue
        func = n.func
        if isinstance(func, ast.Name) and func.id == name:
            return True
        if isinstance(func, ast.Attribute) and func.attr == name:
            return True
    return False


def _has_for(node: ast.AST) -> bool:
    return any(isinstance(n, ast.For) for n in ast.walk(node))


def local_loop_violations(tree: ast.AST, rel: str) -> list[str]:
    """Return violations for one parsed module (used on real and fake sources)."""
    out: list[str] = []
    for fn in _functions(tree, "_speaker"):
        if _has_for(fn):
            out.append(f"{rel}:_speaker 含本地 for 循环（应委托 resolve_speaker_from_row）")
        if rel in DELEGATES and not _calls(fn, "resolve_speaker_from_row"):
            out.append(f"{rel}:_speaker 未调用 resolve_speaker_from_row")
    return out


def main() -> bool:
    ok = True
    owner_path = ROOT / OWNER
    owner_tree = ast.parse(owner_path.read_text(encoding="utf-8"))
    if not _functions(owner_tree, "resolve_speaker_from_row"):
        print(f"FAIL: {OWNER} 缺 resolve_speaker_from_row")
        ok = False
    for rel in DELEGATES:
        path = ROOT / rel
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for message in local_loop_violations(tree, rel):
            print("VIOLATION", message)
            ok = False
    seen: set[str] = set()
    for base_name in SCAN_DIRS:
        base = ROOT / base_name
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            rel = str(path.relative_to(ROOT)).replace("\\", "/")
            if rel in seen:
                continue
            seen.add(rel)
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError:
                continue
            if not _functions(tree, "_speaker"):
                continue
            if rel in LOCAL_ALLOWLIST and LOCAL_ALLOWLIST[rel].strip():
                continue
            print(f"VIOLATION {rel}: def _speaker 未登记（新实现必须先问 owner）")
            ok = False
    # 守卫自测：本地循环必须能被抓出来（否则守卫本身无效）。
    fake = ast.parse(
        "def _speaker(row):\n"
        "    for key in (\"sender_name\", \"sender_id\", \"speaker\"):\n"
        "        value = str(getattr(row, key, \"\") or \"\")\n"
        "        if value:\n"
        "            return value\n"
        "    return \"\"\n"
    )
    if not local_loop_violations(fake, "fake.py"):
        print("FAIL: 守卫自测失败——本地循环没有被识别")
        ok = False
    if ok:
        print("speaker single owner: ok")
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
