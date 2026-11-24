"""节拍：一拍开始。

用法：
    python -m loop.tick            # 干跑（只落账，不发送）
    python -m loop.tick --send     # 真发（走旧 OutboundGateway）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from . import brain, ports, snapshot


async def run_tick(*, dry_run: bool = True, account: str | None = None) -> dict:
    db = ports.pick_db(account)
    if not db:
        return {"ok": False, "error": "找不到账号库"}
    snap = snapshot.build_snapshot(db)
    text = snapshot.render_text(snap)
    llm = ports.get_llm()
    receipts = await brain.run_loop(db, text, llm=llm, dry_run=dry_run)
    return {"ok": True, "db": db, "cells": snap["total"], "snapshot": text,
            "receipts": receipts, "dry_run": dry_run}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true", help="真发（默认干跑）")
    ap.add_argument("--account", default=None)
    args = ap.parse_args(argv)
    out = asyncio.run(run_tick(dry_run=not args.send, account=args.account))
    print(json.dumps(out, ensure_ascii=False, indent=2)[:4000])
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
