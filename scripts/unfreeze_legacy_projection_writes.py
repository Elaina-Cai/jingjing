"""Explicitly lift the isolated legacy projection write gate (audited)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.memory.legacy_read_binding import load_read_binding
from core.memory.legacy_write_freeze import unfreeze_projection_writes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binding", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--reason", required=True)
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args(argv)
    try:
        binding = load_read_binding(args.binding)
        result = unfreeze_projection_writes(
            binding,
            dry_run=not args.apply,
            reason=args.reason,
            confirm=args.confirm,
        )
    except Exception as exc:  # noqa: BLE001 - CLI must report the refusal
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=True))
        return 1
    print(json.dumps({"ok": True, **result}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
