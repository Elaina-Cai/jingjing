#!/usr/bin/env python
"""Audit comments/docstrings for references to retired symbols (WP-01c).

A comment that names a symbol which no longer exists is misleading: readers
grep it, find nothing, and assume the note describes current behaviour.
Retired-symbol history belongs in ``docs/archive/``; the code comment should
either be deleted or rewritten without the dead name.

    python scripts/audit_ghost_comments.py
    python scripts/audit_ghost_comments.py --json

Exit code 1 when a retired symbol is still mentioned in a comment/docstring.
Allowlist entries must carry a non-empty reason (same discipline as
``tests/test_message_attribution.py``).
"""
from __future__ import annotations

import argparse
import ast
import io
import json
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: retired symbol -> metadata.  新增退役项时在对应 WP 的删除提交里追加一行。
RETIRED: dict[str, dict] = {
    "analysis_only": {
        "retired_by": "WP-01a（ReplyMode.ANALYSIS_ONLY 删除）",
        "allow": {
            "scripts/audit_policy_rules.py": "迁移工具必须识别旧值并改写为 never",
        },
    },
    "_check_implicit_trigger": {
        "retired_by": "WP-01d（R-A4 删除五条硬编码话术）",
        "allow": {},
    },
    "topic_similarity": {
        "retired_by": "RET-1 / 架构评估 X2（恒 0 死权重退役）",
        "allow": {
            "tests/test_signal_weights.py": "退役守卫本身",
        },
    },
}

SELF_ALLOW = {
    "scripts/audit_ghost_comments.py": "守卫自身",
    "tests/test_ghost_comments.py": "守卫测试",
}

SCAN_DIRS = ("core", "agents", "scheduler", "scripts", "tests", "web")
SKIP_PARTS = frozenset({
    "__pycache__", "_scratch", ".venv", ".venv_it", ".git",
    "backups", "output", "data", "logs", ".tmp", ".test_tmp",
})


def iter_sources() -> list[Path]:
    files: list[Path] = []
    for name in SCAN_DIRS:
        base = ROOT / name
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if any(part in SKIP_PARTS for part in path.parts):
                continue
            files.append(path)
    return files


def text_chunks(source: str) -> list[tuple[int, str]]:
    """Return (line, text) for every comment and docstring in one module."""
    chunks: list[tuple[int, str]] = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.COMMENT:
                chunks.append((token.start[0], token.string))
    except (tokenize.TokenError, IndentationError):
        pass
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return chunks
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        doc = ast.get_docstring(node, clean=False)
        if doc:
            chunks.append((getattr(node, "lineno", 0), doc))
    return chunks


def scan_source(source: str, rel: str) -> list[tuple[str, int, str]]:
    """Pure scanner (used by the test's negative self-check)."""
    hits: list[tuple[str, int, str]] = []
    rel_norm = rel.replace("\\", "/")
    if rel_norm in SELF_ALLOW and SELF_ALLOW[rel_norm].strip():
        return hits
    for symbol, meta in RETIRED.items():
        if not str(meta.get("retired_by") or "").strip():
            hits.append((rel_norm, 0, f"{symbol}: 退役项缺 retired_by"))
            continue
        needle = symbol.lower()
        for line, chunk in text_chunks(source):
            if needle not in chunk.lower():
                continue
            allowed = meta.get("allow") or {}
            reason = str(allowed.get(rel_norm) or "").strip()
            if reason:
                continue
            hits.append((rel_norm, line, f"{symbol}（{meta['retired_by']}）仍出现在注释/文档字符串"))
    return hits


def scan_all() -> list[tuple[str, int, str]]:
    hits: list[tuple[str, int, str]] = []
    for path in iter_sources():
        rel = str(path.relative_to(ROOT))
        hits.extend(scan_source(path.read_text(encoding="utf-8", errors="replace"), rel))
    return sorted(set(hits))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    hits = scan_all()
    if args.json:
        print(json.dumps({"retired": sorted(RETIRED), "hits": hits}, ensure_ascii=False, indent=2))
    else:
        print(f"retired_symbols={len(RETIRED)} hits={len(hits)}")
        for rel, line, message in hits:
            print(f"  [ghost] {rel}:{line} {message}")
    return 1 if hits else 0


if __name__ == "__main__":
    raise SystemExit(main())
