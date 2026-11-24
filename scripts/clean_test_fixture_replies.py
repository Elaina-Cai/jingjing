# -*- coding: utf-8 -*-
"""一次性维护：清除 2026-10-01/02 那批**测试构造**的 reply 脏数据。

背景（2026-10-10 发现）
----------------------
``terminal_events`` 里 ``event_type='reply'`` 有 126 条 ``content='ok'``，
**全部**带 ``metadata_json.parent_msg_id`` = ``m0``(33) / ``m1``(93)。
真实微信消息 id 不是这个形状；且它们在同一秒内成批出现（18:27:33.012/.022/.030），
只有 reply 一种事件类型带这个特征 —— 是联调构造写进事件流的，不是她说过的话。

危害：把表达层基线整个带偏 —— 长度中位数 41→48、均值 57.7→70.9、P90 125→149、
<=8 字占比 20.2%→1.3%（见 ``scripts/expr_signal_report.py``）。

安全网（本脚本按顺序做，失败即中止）
------------------------------------
1. 把 126 条**完整行**导出到 ``output/clean_test_rows_<ts>.json``；
2. 用 sqlite backup API 做一份**一致性整库快照**到 ``backups/pre_clean_<ts>.db``；
3. 才执行 DELETE（单事务）。

删除前已核对：同表 ``parent_event_id`` / ``turn_id`` 无引用，其余表也无 event-id 列指向它们。

用法::

    python scripts/clean_test_fixture_replies.py --dry-run   # 只看，不动
    python scripts/clean_test_fixture_replies.py             # 真删
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "yinyue_meta.db"
TEST_PARENT = re.compile(r"^m\d+$")

def pick_ids(con: sqlite3.Connection) -> list[int]:
    """按「reply + parent_msg_id 形如 m0/m1」挑 id（与基线脚本同一口径）。"""
    ids: list[int] = []
    for r in con.execute("select id, event_type, metadata_json from terminal_events"):
        if str(r["event_type"]) != "reply":
            continue
        try:
            md = json.loads(r["metadata_json"] or "{}")
        except Exception:
            md = {}
        if TEST_PARENT.match(str(md.get("parent_msg_id") or "")):
            ids.append(int(r["id"]))
    return ids


def main() -> int:
    dry = "--dry-run" in sys.argv
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    try:
        ids = pick_ids(con)
        if not ids:
            print("没有命中，无事可做。")
            return 0
        print(f"命中 {len(ids)} 条测试脏数据（id {min(ids)}-{max(ids)}）")
        if dry:
            print("--dry-run：不导出、不备份、不删除。")
            return 0

        placeholders = ",".join("?" * len(ids))

        # 1) 导出完整行（留档：删了什么、为什么）
        rows = [dict(r) for r in con.execute(
            f"select * from terminal_events where id in ({placeholders})", ids)]
        dump = ROOT / "output" / f"clean_test_rows_{stamp}.json"
        dump.parent.mkdir(parents=True, exist_ok=True)
        dump.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"1/3 已导出 {len(rows)} 行 -> {dump}")

        # 2) 整库一致性快照（sqlite backup API，不是文件拷贝）
        bak = ROOT / "backups" / f"pre_clean_{stamp}.db"
        bak.parent.mkdir(parents=True, exist_ok=True)
        dst = sqlite3.connect(str(bak))
        try:
            con.backup(dst)
        finally:
            dst.close()
        print(f"2/3 已备份整库 -> {bak}  ({bak.stat().st_size / 1024 / 1024:.1f} MB)")

        # 3) 删除（单事务）
        cur = con.execute(
            f"delete from terminal_events where id in ({placeholders})", ids)
        con.commit()
        print(f"3/3 已删除 {cur.rowcount} 行")

        print(f"复核：剩余 {len(pick_ids(con))} 条")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
