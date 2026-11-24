"""把「未确认」的发送回执按本地账号库重新核对一遍（默认只报告，--apply 才写库）。

2026-11-24 事故：回读判定只认 sender_id='self'，现役采集写的是账号 wxid，
于是明明发出去的消息被记成「回读追认 5 次都没找到」，她再转告主人「可能没送到」。
判定修好后，这个脚本用来把历史里可核实的回执补成 confirmed；核实不了的保持未确认。

用法：
    python scripts/confirm_sent_by_readback.py --hours 240          # 只报告
    python scripts/confirm_sent_by_readback.py --hours 240 --apply  # 写回 confirmed
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.execution import local_text_receipt as ltr  # noqa: E402

ACCOUNT = "wxid_seed02"


def _account_db(account: str) -> Path:
    return ROOT / "data" / "platforms" / "wechat" / account / "account.db"


def candidates(world: Path, account: str, *, hours: float) -> list[dict]:
    cutoff = time.time() - float(hours) * 3600
    conn = sqlite3.connect(world.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "select r.id receipt_id, r.turn_id, r.text, r.sent_at_utc, r.ack, r.msg_id, "
            "q.id queue_id, q.chat_id, q.platform, q.account_id from outbound_receipt r "
            "join outbound_queue q on r.turn_id = 'queue:' || q.id "
            "where coalesce(r.ack,'') <> 'confirmed' and q.created_ts >= ? "
            "order by q.created_ts", (cutoff,)).fetchall()
    finally:
        conn.close()
    out = []
    for row in rows:
        d = dict(row)
        if str(d.get("account_id") or "") != account:
            continue
        out.append(d)
    return out


def check(row: dict, account_db: Path) -> tuple[str, str]:
    """返回 (状态, msg_id)。状态：UNIQUE / NONE / MULTI / NO_TEXT。"""
    text = str(row.get("text") or "")
    if not text:
        return "NO_TEXT", ""
    sent = str(row.get("sent_at_utc") or "")[:19]
    try:
        at = datetime.strptime(sent, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        at = datetime.now()
    lo = (at - timedelta(seconds=10)).strftime("%Y-%m-%d %H:%M:%S")
    hi = (at + timedelta(seconds=600)).strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(account_db.as_uri() + "?mode=ro", uri=True)
    try:
        hits = conn.execute(
            "select id, msg_id from messages where chat_id=? and content=? and created_at between ? and ?",
            (str(row.get("chat_id") or ""), text, lo, hi)).fetchall()
    finally:
        conn.close()
    ok = [(lid, mid) for lid, mid in hits if ltr.matches(str(account_db), str(lid), str(row.get("chat_id") or ""), text)]
    if len(ok) == 1:
        return "UNIQUE", str(ok[0][1] or "")
    return ("MULTI" if ok else "NONE"), ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--account", default=ACCOUNT)
    ap.add_argument("--world", default=str(ROOT / "data" / "world.db"))
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    account_db = _account_db(args.account)
    rows = candidates(Path(args.world), args.account, hours=args.hours)
    print("候选未确认回执：%d" % len(rows))
    counts = {"UNIQUE": 0, "NONE": 0, "MULTI": 0, "NO_TEXT": 0}
    for row in rows:
        status, msg_id = check(row, account_db)
        counts[status] = counts.get(status, 0) + 1
        print("receipt=%s queue=%s %s %s %s" % (row.get("receipt_id"), row.get("queue_id"),
                                                status, msg_id, str(row.get("text") or "")[:40].replace("\n", " ")))
        if args.apply and status == "UNIQUE" and msg_id:
            from db import world_db

            world_db.confirm_outbound(str(row.get("chat_id") or ""), str(row.get("text") or ""),
                                      msg_id=msg_id, receipt_id=int(row["receipt_id"]),
                                      platform="wechat", account_id=args.account,
                                      queue_id=int(row["queue_id"]), attempt_no=1)
    print("汇总：%s" % counts)
    if not args.apply:
        print("（只报告；要写回 confirmed 加 --apply）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
