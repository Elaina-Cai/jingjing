#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""回填 terminal_events 里 bot 自产事件的 actor 语义（事实层修复）。

背景（2026-09-24）：
  ``core/terminal/shadow.py::_sent_by_bot`` 曾把「对话对方」同时写进
  ``actor_person_id``（正确语义是 bot 账号；对方属于 ``recipient_person_ids``）。
  09-23 已回填过一批，但代码 bug 让此后每条 reply/thought/action 又重新写坏，
  导致 ``scripts/gate_hard_rules.py::check_actor_semantics`` 持续报警。

本脚本做三件事（幂等、可重复跑）：
  1. 打印当前违规分布（按 event_type）；
  2. ``--apply`` 时先复制一份 DB 到 ``backups/`` 再就地修正：
     - ``actor_person_id`` := ``bot_account_id``；
     - ``actor_account_id`` 为空时补成 ``bot_account_id``；
     - 若 ``recipient_person_ids`` 为空且旧 actor 非空，把旧 actor 挪进 recipient
       （对方仍在接收方槽，私聊可见性不受损）。
  3. 复核并打印剩余违规（应为 0）。

用法：
    python scripts/backfill_event_actor_semantics.py                 # 只看（dry-run）
    python scripts/backfill_event_actor_semantics.py --apply
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

META_DB = ROOT / "data" / "yinyue_meta.db"
BACKUP_DIR = ROOT / "backups"
EVENT_TYPES = ("reply", "thought", "action")

_VIOLATION_WHERE = (
    "event_type in ('reply','thought','action') "
    "and bot_account_id is not null and bot_account_id != '' "
    "and coalesce(actor_person_id,'') != bot_account_id"
)


def _count_violations(con: sqlite3.Connection) -> list[tuple[str, int]]:
    return list(con.execute(
        "select event_type, count(*) from terminal_events "
        f"where {_VIOLATION_WHERE} group by event_type order by 2 desc"
    ))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(META_DB))
    parser.add_argument("--apply", action="store_true", help="就地修正（默认只读）")
    args = parser.parse_args()

    path = Path(args.db)
    if not path.exists():
        print(f"[err] meta db not found: {path}")
        return 2

    con = sqlite3.connect(str(path))
    try:
        before = _count_violations(con)
        total_before = sum(count for _, count in before)
        print(f"[dry] 违规分布: {before or '（无）'}  合计 {total_before}")
        if not args.apply:
            print("（只读模式；加 --apply 才会修正）")
            return 0
        if total_before == 0:
            print("无需修正。")
            return 0

        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = BACKUP_DIR / f"{path.stem}.db.{stamp}.bak"
        shutil.copy2(path, backup)
        print(f"[backup] {backup}")

        rows = con.execute(
            "select id, actor_person_id, actor_account_id, recipient_person_ids_json "
            f"from terminal_events where {_VIOLATION_WHERE}"
        ).fetchall()
        fixed = 0
        moved_recipient = 0
        for tid, old_actor, old_account, raw_recipients in rows:
            try:
                recipients = json.loads(raw_recipients or "[]")
            except (TypeError, ValueError):
                recipients = []
            if not isinstance(recipients, list):
                recipients = []
            counterpart = str(old_actor or "").strip()
            if counterpart and not recipients:
                recipients = [counterpart]
                moved_recipient += 1
            con.execute(
                "update terminal_events set actor_person_id = bot_account_id, "
                "recipient_person_ids_json = ? where id = ?",
                (json.dumps(recipients, ensure_ascii=False), tid),
            )
            con.execute(
                "update terminal_events set actor_account_id = bot_account_id "
                "where id = ? and (actor_account_id is null or actor_account_id = '')",
                (tid,),
            )
            fixed += 1
        con.commit()
        print(f"[apply] 修正 {fixed} 条（其中把旧 actor 挪进 recipient 的 {moved_recipient} 条）")

        after = _count_violations(con)
        print(f"[check] 剩余违规: {after or '（无）'}")
        return 0 if not after else 1
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
