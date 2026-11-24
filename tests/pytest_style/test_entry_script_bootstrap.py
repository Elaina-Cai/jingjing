"""入口脚本的顺序纪律：``sys.path`` 补丁必须排在第一个项目 import 之前。

2026-10-30：`console/collector.py` 把补丁写在 ``from core.infra import liveness`` 下面，
双击启动时直接 ``ModuleNotFoundError: No module named 'core'`` —— 采集器起不来、端口空着、
面板只好报 "Failed to fetch"。`console/open_window.py` 更彻底：连补丁都没有。

这类错只有"按顺序读代码"才看得出来，所以用锁钉住：只要文件里出现 ``sys.path`` 的插入，
它就必须在第一个项目 import 之前。不写插入的文件不管（有些靠 ``python -m`` 或 cwd）。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PROJECT_TOP = ("core", "console", "db", "loop", "agents", "scheduler", "scripts", "voice", "web")
ENTRY_DIRS = ("console", "scripts")


def _runs_as_script(path: Path) -> bool:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace").lstrip("\ufeff"))
    except SyntaxError:
        return False
    return any(
        isinstance(n, ast.If)
        and isinstance(n.test, ast.Compare)
        and isinstance(n.test.left, ast.Name)
        and n.test.left.id == "__name__"
        for n in tree.body
    )


def _project_imports(tree: ast.Module) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for node in tree.body:
        mods: list[str] = []
        if isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            mods = [node.module]
        for m in mods:
            if m.split(".")[0] in PROJECT_TOP:
                out.append((node.lineno, m))
    return out


def _path_patch_line(tree: ast.Module) -> int | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if (
                isinstance(f, ast.Attribute)
                and f.attr in ("insert", "append")
                and isinstance(f.value, ast.Attribute)
                and f.value.attr == "path"
                and isinstance(f.value.value, ast.Name)
                and f.value.value.id == "sys"
            ):
                return node.lineno
    return None


def _entry_scripts() -> list[Path]:
    out: list[Path] = []
    for d in ENTRY_DIRS:
        for p in sorted((ROOT / d).glob("*.py")):
            if _runs_as_script(p):
                out.append(p)
    return out


def violations() -> list[str]:
    out: list[str] = []
    for p in _entry_scripts():
        tree = ast.parse(p.read_text(encoding="utf-8", errors="replace").lstrip("\ufeff"))
        patch = _path_patch_line(tree)
        if patch is None:
            continue
        imports = _project_imports(tree)
        if not imports:
            continue
        first_line, first_mod = min(imports)
        if first_line < patch:
            out.append(
                "%s:%d 先 import 了 %s，%d 行才补 sys.path —— 双击启动会 ModuleNotFoundError"
                % (p.relative_to(ROOT).as_posix(), first_line, first_mod, patch)
            )
    return out


def test_entry_source_has_patch_before_imports() -> None:
    assert violations() == []


def test_scan_actually_looked_at_something() -> None:
    scripts = _entry_scripts()
    assert len(scripts) >= 5, scripts
    assert any(_path_patch_line(ast.parse(p.read_text(encoding="utf-8", errors="replace").lstrip("\ufeff"))) for p in scripts)


def test_a_broken_file_would_be_caught(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text(
        "from core.infra import liveness\nimport sys, os\nsys.path.insert(0, os.path.dirname(__file__))\n"
        "if __name__ == '__main__':\n    pass\n",
        encoding="utf-8",
    )
    tree = ast.parse(bad.read_text(encoding="utf-8"))
    patch = _path_patch_line(tree)
    assert patch == 3 and min(_project_imports(tree))[0] == 1, (patch, _project_imports(tree))
