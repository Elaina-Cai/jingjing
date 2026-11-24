#!/usr/bin/env python
"""WP-01c guard: comments/docstrings must not name retired symbols."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for extra in (str(ROOT), str(ROOT / "scripts")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import audit_ghost_comments as audit  # noqa: E402


def main() -> bool:
    ok = True
    for rel, line, message in audit.scan_all():
        print(f"GHOST {rel}:{line} {message}")
        ok = False
    fake = "# topic_similarity was retired here\nVALUE = 1\n"
    if not audit.scan_source(fake, "core/fake.py"):
        print("FAIL: 负例未被识别（守卫本身无效）")
        ok = False
    for symbol, meta in audit.RETIRED.items():
        if not str(meta.get("retired_by") or "").strip():
            print(f"FAIL: {symbol} 缺 retired_by")
            ok = False
        for rel, reason in (meta.get("allow") or {}).items():
            if not str(reason or "").strip():
                print(f"FAIL: {symbol} 白名单 {rel} 缺理由")
                ok = False
    if ok:
        print(f"ghost comments: ok ({len(audit.RETIRED)} retired symbols)")
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
