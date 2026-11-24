#!/usr/bin/env python
"""把历史 job 摊开看（只读，S1「先量不写」）。

回答三个问题：
1. 31 件历史 job 是谁开的、什么状态、走到第几步；
2. 那些"该开单"的场合在真实对话里有多少（分母）；
3. job 的发送到底有没有落到 outbound_queue（没有的话，"办完了"就是又一本假账，
   和 E-50 的账本假账同一类）。

用法：
    python scripts/job_audit.py            # 文本
    python scripts/job_audit.py --json
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
META = os.path.join(ROOT, "data", "yinyue_meta.db")
WORLD = os.path.join(ROOT, "data", "world.db")

#: 自检驱动写进 goal 的记号（2026-10-21 那批 `scripts/job_drive.py` 跑的）
SELF_CHECK_MARK = "自检驱动"


def _rows(conn, sql, args=()):
    try:
        return [tuple(r) for r in conn.execute(sql, args)]
    except sqlite3.Error:
        return []


def audit(meta: str = META, world: str = WORLD) -> dict:
    out: dict = {"meta_db": meta, "world_db": world, "exists": os.path.exists(meta)}
    if not out["exists"]:
        return out
    c = sqlite3.connect("file:%s?mode=ro" % meta.replace("\\", "/"), uri=True)
    out["jobs_total"] = (_rows(c, "select count(*) from jobs") or [(0,)])[0][0]
    out["by_state"] = dict(_rows(c, "select state, count(*) from jobs group by state"))
    out["event_kinds"] = dict(_rows(c, "select kind, count(*) from job_events group by kind"))
    out["approvals"] = (_rows(c, "select count(*) from job_approvals") or [(0,)])[0][0]
    out["self_check"] = (_rows(c, "select count(*) from jobs where goal like ?",
                               ("%" + SELF_CHECK_MARK + "%",)) or [(0,)])[0][0]
    out["real_goals"] = [g for (g,) in _rows(
        c, "select goal from jobs where goal not like ? order by updated_at",
        ("%" + SELF_CHECK_MARK + "%",))]
    out["by_chat"] = dict(_rows(c, "select coalesce(chat_id,''), count(*) from jobs group by 1"))
    out["step_hist"] = dict(_rows(c, "select step_n, count(*) from jobs group by 1 order by 1"))

    # 分母：真实对话里「该开单」的场合。
    # `feedback_samples` 在 **data/yinyue_meta.db**（和 jobs 同一个库），不在 world.db、
    # 也不在各账号的 account.db（2026-11-04 连查两处都是 0，差点把"没有分母"当成结论）。
    out["denominator"] = dict(_rows(
        c, "select source, count(*) from feedback_samples group by 1"))
    out["denominator_status"] = dict(_rows(
        c, "select status, count(*) from feedback_samples group by 1"))
    out["denominator_span"] = (_rows(
        c, "select min(created_at), max(created_at) from feedback_samples") or [("", "")])[0]
    fb = os.path.join(ROOT, "data", "world.db")
    if os.path.exists(fb):
        w = sqlite3.connect("file:%s?mode=ro" % fb.replace("\\", "/"), uri=True)
        out["world_tables"] = [t for (t,) in _rows(
            w, "select name from sqlite_master where type='table' order by name")]
        out["outbound_turn_prefix"] = dict(_rows(
            w, "select substr(coalesce(turn_id,''),1,3), count(*) from outbound_queue group by 1"))
        out["outbound_job_rows"] = (_rows(
            w, "select count(*) from outbound_queue where coalesce(turn_id,'') like 'job%'"
        ) or [(0,)])[0][0]
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    r = audit()
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0
    print("jobs 总数：%s" % r.get("jobs_total"))
    print("按状态：%s" % r.get("by_state"))
    print("按会话：%s" % r.get("by_chat"))
    print("步骤分布：%s" % r.get("step_hist"))
    print("事件种类：%s" % r.get("event_kinds"))
    print("审批记录：%s" % r.get("approvals"))
    print("自检驱动开的：%s / %s" % (r.get("self_check"), r.get("jobs_total")))
    print("真实 goal：%d 条" % len(r.get("real_goals") or []))
    for g in (r.get("real_goals") or [])[:12]:
        print("  - %s" % g[:70])
    print("分母（feedback_samples）：%s" % r.get("denominator"))
    print("outbound_queue 的 turn_id 前缀：%s" % r.get("outbound_turn_prefix"))
    print("outbound_queue 里 job 开的行：%s" % r.get("outbound_job_rows"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
