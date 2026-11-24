"""Step D2: unified store port tests (standalone + pytest compatible)."""
from __future__ import annotations

import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.terminal import store_port  # noqa: E402
SKIP_DIRS = {".git", ".venv_it", ".venv", "output", ".test_tmp", "__pycache__", "node_modules", "backups", "scripts", "tests"}
ALLOW = {"core/bootstrap.py", "core/terminal/store_port.py"}
DATA = {"get_meta_repo", "get_repo", "get_account_db_manager", "get_unified_query"}
GETTERS = ("get_meta_repo", "get_repo", "get_account_db_manager", "get_unified_query")


def test_store_digest_shape():
    digest = store_port.store_digest()
    assert digest["surfaces"] == ["meta", "account", "account_db_manager", "unified"]
    assert digest["surfaces_count"] == 4
    assert set(digest["owners"]) == set(store_port.STORE_SURFACES)
    assert len(store_port.STORE_OWNERS) == 4


def test_store_port_passthrough_identity():
    import core.bootstrap as bootstrap

    sentinel = object()
    saved = {name: getattr(bootstrap, name) for name in GETTERS}
    try:
        for name in GETTERS:
            setattr(bootstrap, name, lambda name=name: (name, sentinel))
        assert store_port.get_meta_repo() == ("get_meta_repo", sentinel)
        assert store_port.get_repo() == ("get_repo", sentinel)
        assert store_port.get_account_db_manager() == ("get_account_db_manager", sentinel)
        assert store_port.get_unified_query() == ("get_unified_query", sentinel)
    finally:
        for name, fn in saved.items():
            setattr(bootstrap, name, fn)


def test_store_port_propagates_exceptions():
    import core.bootstrap as bootstrap

    saved = bootstrap.get_meta_repo

    class Boom(RuntimeError):
        pass

    try:
        bootstrap.get_meta_repo = lambda: (_ for _ in ()).throw(Boom("boom"))
        try:
            store_port.get_meta_repo()
        except Boom:
            pass
        else:
            raise AssertionError("store_port must not swallow exceptions")
    finally:
        bootstrap.get_meta_repo = saved


def test_store_port_top_level_imports_are_pure():
    src = (ROOT / "core/terminal/store_port.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    top = [ast.unparse(node) for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
    assert top == ["from __future__ import annotations", "from typing import Any"], top


def test_no_runtime_direct_data_getter_imports():
    offenders = []
    for p in sorted(ROOT.rglob("*.py")):
        rel = p.relative_to(ROOT).as_posix()
        if any(part in SKIP_DIRS for part in p.relative_to(ROOT).parts):
            continue
        if rel in ALLOW:
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "core.bootstrap":
                hit = sorted({a.name for a in node.names} & DATA)
                if hit:
                    offenders.append(f"{rel}:{node.lineno}: {hit}")
    assert offenders == [], "direct data getter imports remain: " + "; ".join(offenders)


def main() -> bool:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in tests:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {exc}")
        else:
            print(f"ok   {fn.__name__}")
    print(f"store-port checks: {len(tests) - failed}/{len(tests)} passed")
    return failed == 0


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
