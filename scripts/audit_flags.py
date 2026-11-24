#!/usr/bin/env python
"""Audit three-state flag readers (WP-02).

Two classes of violations:

1. Static: a three-state flag is read with ``features.enabled()`` (which would
   silently read ``shadow`` as ``False``), or ``features.mode()`` is called with
   a flag that is missing from ``core.features.FLAG_REGISTRY``.
2. Registry: a tri-state spec is missing ``retire_trigger``; a migration-type
   spec is missing ``kill_date``; with ``--strict``, a migration flag whose
   ``kill_date`` has passed is still read.

    python scripts/audit_flags.py            # report + exit 1 on violations
    python scripts/audit_flags.py --strict   # also fail on expired kill_date
    python scripts/audit_flags.py --json     # machine-readable

Exit code 0 = clean.
"""
from __future__ import annotations

import argparse
import ast
import datetime
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import features  # noqa: E402

SCAN_DIRS = ("core", "agents", "scheduler", "scripts", "tests", "web")
SKIP_PARTS = frozenset({
    "__pycache__", "_scratch", ".venv", ".venv_it", ".git",
    "backups", "output", "data", "logs", ".tmp", ".test_tmp",
})
SELF_NAME = "audit_flags.py"


def iter_sources() -> list[Path]:
    files: list[Path] = []
    for name in SCAN_DIRS:
        base = ROOT / name
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if path.name == SELF_NAME:
                continue
            if any(part in SKIP_PARTS for part in path.parts):
                continue
            files.append(path)
    return files


def _const_values() -> dict[str, str]:
    out: dict[str, str] = {}
    for attr, value in vars(features).items():
        if isinstance(value, str) and attr.isupper():
            out[attr] = value
    return out


def _resolve_flag(node: ast.AST, consts: dict[str, str]) -> str:
    """Best-effort static resolution of a flag argument to its flag name."""
    if isinstance(node, ast.Attribute):
        if isinstance(node.value, ast.Name) and node.value.id == "features":
            return consts.get(node.attr, "")
        return ""
    if isinstance(node, ast.Name):
        return consts.get(node.id, "")
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return ""


def static_violations() -> list[tuple[str, int, str]]:
    consts = _const_values()
    tri = features.TRI_STATE_FLAGS
    violations: list[tuple[str, int, str]] = []
    for path in iter_sources():
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source)
        except SyntaxError:
            continue
        lines = source.splitlines()
        rel = str(path.relative_to(ROOT))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = node.func
            func_name = ""
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                if func.value.id == "features":
                    func_name = func.attr
            elif isinstance(func, ast.Name):
                func_name = func.id
            if func_name not in ("enabled", "mode"):
                continue
            flag = _resolve_flag(node.args[0], consts)
            if not flag:
                continue
            # 负例/守卫自测允许内联白名单，必须写明理由（同 tests/test_message_attribution.py 纪律）。
            raw_line = lines[node.lineno - 1] if 0 <= node.lineno - 1 < len(lines) else ""
            pragma = "# tri-state-reader-ok:"
            if pragma in raw_line and raw_line.split(pragma, 1)[1].strip():
                continue
            if func_name == "enabled" and flag in tri:
                violations.append((
                    rel, node.lineno,
                    f"features.enabled({flag})：三态 flag 必须用 features.mode() 读",
                ))
            elif func_name == "mode" and flag not in features.FLAG_REGISTRY:
                violations.append((
                    rel, node.lineno,
                    f"features.mode({flag})：三态 flag 未登记进 FLAG_REGISTRY",
                ))
    return sorted(set(violations))


def registry_violations(strict: bool) -> list[tuple[str, int, str]]:
    bad: list[tuple[str, int, str]] = []
    today = datetime.date.today()
    for name, spec in features.FLAG_REGISTRY.items():
        if spec.kind != "tri":
            continue
        if not str(spec.retire_trigger or "").strip():
            bad.append((name, 0, "三态 flag 缺 retire_trigger"))
        if spec.migration and not str(spec.kill_date or "").strip():
            bad.append((name, 0, "migration 型三态缺 kill_date"))
        if spec.kill_date:
            try:
                kill = datetime.date.fromisoformat(spec.kill_date)
            except ValueError:
                bad.append((name, 0, f"kill_date 非法: {spec.kill_date}"))
            else:
                if kill < today:
                    message = f"kill_date 已过期（{spec.kill_date}），flag 仍在读"
                    if strict:
                        bad.append((name, 0, message))
    return bad


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true", help="kill_date 过期也判失败")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    static = static_violations()
    registry = registry_violations(args.strict)
    payload = {
        "tri_state_count": len(features.TRI_STATE_FLAGS),
        "registry_count": len(features.FLAG_REGISTRY),
        "static_violations": static,
        "registry_violations": registry,
        "strict": bool(args.strict),
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(
            f"tri_state={payload['tri_state_count']} "
            f"registry={payload['registry_count']} "
            f"static_violations={len(static)} "
            f"registry_violations={len(registry)}"
        )
        for rel, line, message in static:
            print(f"  [static] {rel}:{line} {message}")
        for name, _line, message in registry:
            print(f"  [registry] {name} {message}")
    return 1 if (static or registry) else 0


if __name__ == "__main__":
    raise SystemExit(main())
