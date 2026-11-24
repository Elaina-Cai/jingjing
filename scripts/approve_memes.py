# -*- coding: utf-8 -*-
r"""审批 web 梗词条（status pending -> approved / denied）。

用法：
  python scripts/approve_memes.py --list --top 30
  python scripts/approve_memes.py --approve --top 10
  python scripts/approve_memes.py --approve 望砖 专是专本是本
  python scripts/approve_memes.py --deny 某梗
"""
from __future__ import annotations

import argparse
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
INDEX = ROOT / "data/web_memes/memes_index.json"


def norm(text) -> str:
    return "".join(
        ch for ch in str(text or "").lower()
        if ch.isalnum() or 0x4E00 <= ord(ch) <= 0x9FFF
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--top", type=int, default=0)
    ap.add_argument("--approve", nargs="*", default=None)
    ap.add_argument("--deny", nargs="*", default=None)
    args = ap.parse_args()
    data = json.loads(INDEX.read_text(encoding="utf-8"))
    entries = data.get("entries") or []
    if args.list:
        pending = [e for e in entries if e.get("status") != "approved"]
        print("total=" + str(len(entries)) + " pending=" + str(len(pending)))
        for e in pending[: (args.top or 30)]:
            print("  [" + str(e.get("heat", 0)) + "] " + str(e.get("key")) + " | " + str(e.get("meaning", ""))[:70])
        return 0
    if args.approve is not None or args.deny is not None:
        approve = args.approve is not None
        status = "approved" if approve else "denied"
        keys = [norm(k) for k in ((args.approve if approve else args.deny) or [])]
        if approve and not keys and args.top:
            pool = [e for e in entries if e.get("status") != "denied"]
            pool.sort(key=lambda e: -(int(e.get("heat") or 0)))
            keys = [norm(e.get("key")) for e in pool[: args.top]]
        changed = 0
        for e in entries:
            if norm(e.get("key")) in keys:
                e["status"] = status
                changed += 1
        INDEX.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        print(status + ": " + str(changed) + " 条")
        return 0
    ap.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
