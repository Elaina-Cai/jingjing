"""盘点「说发出去了，其实没有凭据」的队列行（只读）。

2026-11-04 的教训：`outbound_queue.state = 'sent'` 只是发送工自己说的，
不是微信里真有这条。这个脚本把两者对一遍：

    python scripts/receipts_audit.py            # 人看
    python scripts/receipts_audit.py --json     # 机器读
    python scripts/receipts_audit.py --days 7   # 只看最近 7 天

判据在 `loop/receipts.py`（三层：队列 sent → 有 msg_id → 微信账库真有这个 id）。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _rows(days: float) -> list:
    from db import world_db as W

    since = time.time() - (days * 86400.0)
    c = W.conn()
    try:
        cur = c.execute(
            "select id, chat_id, state, ack, why, coalesce(updated_ts, created_ts, 0) from outbound_queue "
            "where coalesce(updated_ts, created_ts, 0) >= ? order by id", (since,))
        return [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]
    finally:
        try:
            c.close()
        except sqlite3.Error as exc:  # 关连接失败不影响盘点结果，但要说出来
            print("（关连接失败：%s）" % exc, file=sys.stderr)


def _n(v) -> int:
    """口径里的数字可能是计数，也可能是明细列表（历史上两种都出现过）。"""
    if isinstance(v, (list, tuple, dict, set)):
        return len(v)
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def audit(*, days: float = 30.0) -> dict:
    """汇总 + 抽几条给人看。口径只有一处：`loop/receipts.py::summary`（2026-11-04）。"""
    from loop import receipts as R

    got = dict(R.summary(days=days) or {})
    samples = []
    try:
        rows = _rows(days)
    except Exception as exc:  # noqa: BLE001 - 抽样失败不影响汇总
        print("（抽样失败：%s）" % exc, file=sys.stderr)
        rows = []
    for row in rows:
        rec = R.verify_row(row)
        if rec.verified:
            continue
        samples.append({"queue_id": rec.queue_id, "chat": rec.chat_id,
                        "ack": rec.ack, "why": rec.why, "state": rec.state})
    got["samples"] = samples[:10]
    # 2026-11-05（§29.5 第 1 条）：光说「有凭据」不够，还得看那条消息是不是这句话。
    # 读不出来必须说出来，不许当成「全对」。
    try:
        got["reverse"] = dict(R.reverse_check(limit=1000) or {})
    except Exception as exc:  # noqa: BLE001
        got["reverse"] = {"seen": 0, "checked": 0, "ok": 0, "no_msg_id": 0,
                          "problems": [], "error": repr(exc)[:160]}
    return got


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description="盘点说发了但其实没凭据的队列行")
    ap.add_argument("--days", type=float, default=30.0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    out = audit(days=args.days)
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    # 2026-11-05：`summary()` 的口径早就换成了 claimed/verified/unverified/rewritten，
    # 这里却还在读 sent_rows —— 脚本自己一直 KeyError（没人跑过它，所以两年没红）。
    sent_rows = out.get("sent_rows", out.get("claimed", 0))
    sent_rows = out.get("sent_rows", out.get("claimed", 0))
    print("看 %g 天：队列共 %d 行；说「发出去了」的 %d 行"
          % (out["days"], _n(out.get("total")), _n(sent_rows)))
    print("  有凭据（微信账库真有这条）：%d" % _n(out.get("verified")))
    print("  没凭据（sent 但没有 msg_id / 账库查不到）：%d" % _n(out.get("unverified")))
    print("  已回写 unverified（证不出来，2026-11-04 起）：%d" % _n(out.get("rewritten")))
    print("  还在路上：%d" % _n(out.get("pending")))
    print("  压根没发（failed/expired 等）：%d" % _n(out.get("not_sent")))
    print("  凭据率：%.1f%%" % (float(out.get("rate") or 0.0) * 100.0))
    rev = out.get("reverse") or {}
    if rev.get("error"):
        print("  反向核对：读不出来（%s）" % rev["error"])
    else:
        print("  反向核对（微信里那条消息是不是这句话）：查 %d 条，对上 %d 条，对不上 %d 条，没 id 的 %d 条"
              % (_n(rev.get("checked")), _n(rev.get("ok")),
                 _n(rev.get("problems")), _n(rev.get("no_msg_id"))))
        for p in (rev.get("problems") or [])[:5]:
            print("    #%s %s msg=%s %s" % (p.get("queue_id"), p.get("chat_id"),
                                            p.get("msg_id"), p.get("kind")))
    for s in out["samples"]:
        print("    #%s %s ack=%r why=%r" % (s["queue_id"], s["chat"], s["ack"], s["why"][:60]))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
