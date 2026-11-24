#!/usr/bin/env python
# 【历史工具 · 2026-09-21 归档】旧单库 data/yinyue.db 与 meta `memories` 表已按主人指令删除，
# 本脚本已完成使命，仅保留作审计/回溯参考；不要再在生产流程中运行。
"""Fill the gaps between the legacy single DB and the new layers.

Read-only by default.  With ``--apply`` it first backs up every touched DB and
then copies only the *missing* rows, keeping the new layers a superset of the
legacy data (the precondition for deleting the legacy DB):

* knowledge_docs -> meta DB knowledge_docs   (dedupe by content_hash)
* messages       -> account DB messages      (dedupe by msg_id)
* analysis       -> account DB analysis      (dedupe by chat_id/type/created_at)

WeChat system pseudo-contacts (filehelper / fmessage / weixin / ...) are not
real people and are intentionally not copied; the report lists them.

    python scripts/consolidate_legacy_stores.py
    python scripts/consolidate_legacy_stores.py --apply
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

LEGACY_DB = ROOT / "data" / "yinyue.db"
META_DB = ROOT / "data" / "yinyue_meta.db"
ACCOUNT_DB = (
    ROOT / "data" / "platforms" / "wechat" / "wxid_seed02" / "account.db"
)
BACKUP_ROOT = ROOT / "backups"

SYSTEM_CONTACTS = {
    "notifymessage",
    "weixin",
    "fmessage",
    "medianote",
    "newsapp",
    "filehelper",
    "floatbottle",
    "qqmail",
    "tmessage",
    "qmessage",
}


def _cols(con: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in con.execute(f"pragma table_info({table})")]


def _norm(value) -> str:
    return " ".join(str(value or "").split())


def _plan_knowledge(legacy: sqlite3.Connection, meta: sqlite3.Connection) -> list[dict]:
    existing = {
        str(r[0] or "")
        for r in meta.execute("select content_hash from knowledge_docs")
        if str(r[0] or "")
    }
    meta_persona = meta.execute("select coalesce(min(id), 1) from personas").fetchone()[0]
    cols = _cols(legacy, "knowledge_docs")
    plan = []
    seen: set[str] = set()
    for row in legacy.execute("select * from knowledge_docs"):
        data = dict(zip(cols, row))
        content_hash = str(data.get("content_hash") or "")
        if content_hash and (content_hash in existing or content_hash in seen):
            continue
        seen.add(content_hash)
        data["persona_id"] = int(meta_persona or 1)
        data.pop("id", None)
        plan.append(data)
    return plan


def _plan_messages(legacy: sqlite3.Connection, account: sqlite3.Connection, account_id: str) -> list[dict]:
    existing = {str(r[0]) for r in account.execute("select msg_id from messages")}
    target_cols = _cols(account, "messages")
    source_cols = _cols(legacy, "messages")
    plan = []
    for row in legacy.execute("select * from messages"):
        data = dict(zip(source_cols, row))
        if str(data.get("msg_id")) in existing:
            continue
        item = {key: data.get(key) for key in target_cols if key in data}
        item.pop("id", None)
        item["account_id"] = account_id
        plan.append(item)
    return plan


def _plan_analysis(legacy: sqlite3.Connection, account: sqlite3.Connection) -> list[dict]:
    def keys(con: sqlite3.Connection) -> set[tuple[str, str, str]]:
        return {
            (str(r[0] or ""), str(r[1] or ""), str(r[2] or ""))
            for r in con.execute("select chat_id, analysis_type, created_at from analysis")
        }

    existing = keys(account)
    cols = _cols(legacy, "analysis")
    plan = []
    for row in legacy.execute("select * from analysis"):
        data = dict(zip(cols, row))
        key = (
            str(data.get("chat_id") or ""),
            str(data.get("analysis_type") or ""),
            str(data.get("created_at") or ""),
        )
        if key in existing:
            continue
        data.pop("id", None)
        plan.append(data)
    return plan


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--account-id", default=ACCOUNT_DB.parent.name)
    args = parser.parse_args()

    for path in (LEGACY_DB, META_DB, ACCOUNT_DB):
        if not path.exists():
            print(f"[err] missing DB: {path}")
            return 2

    legacy = sqlite3.connect(str(LEGACY_DB))
    meta = sqlite3.connect(str(META_DB))
    account = sqlite3.connect(str(ACCOUNT_DB))

    knowledge_plan = _plan_knowledge(legacy, meta)
    message_plan = _plan_messages(legacy, account, args.account_id)
    analysis_plan = _plan_analysis(legacy, account)

    account_contacts = {str(r[0]) for r in account.execute("select wxid from contacts")}
    legacy_contacts = {str(r[0]) for r in legacy.execute("select wxid from contacts")}
    missing_contacts = legacy_contacts - account_contacts
    system_only = missing_contacts <= SYSTEM_CONTACTS

    print(f"knowledge_docs to copy : {len(knowledge_plan)}")
    print(f"messages to copy       : {len(message_plan)}")
    print(f"analysis to copy       : {len(analysis_plan)}")
    print(
        f"missing contacts       : {len(missing_contacts)} "
        f"({'all system ids, skip' if system_only else 'NON-SYSTEM!'}) "
        f"{sorted(missing_contacts)}"
    )

    if not args.apply:
        print("\n[dry-run] nothing written. Re-run with --apply.")
        return 0

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = BACKUP_ROOT / f"phase5_legacy_consolidation_{stamp}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    for src in (LEGACY_DB, META_DB, ACCOUNT_DB):
        shutil.copy2(src, backup_dir / src.name)
    print(f"backup -> {backup_dir}")

    if knowledge_plan:
        names = [
            "persona_id", "title", "source_file", "chunk_index", "content",
            "char_count", "tags", "created_at", "source_type", "source_url",
            "original_query", "quality_score", "content_hash", "access_count",
            "last_accessed_at", "updated_at",
        ]
        meta.executemany(
            f"insert into knowledge_docs ({','.join(names)}) "
            f"values ({','.join('?' for _ in names)})",
            [tuple(item.get(name) for name in names) for item in knowledge_plan],
        )
    if message_plan:
        names = [name for name in _cols(account, "messages") if name != "id"]
        account.executemany(
            f"insert into messages ({','.join(names)}) "
            f"values ({','.join('?' for _ in names)})",
            [tuple(item.get(name) for name in names) for item in message_plan],
        )
    if analysis_plan:
        names = ["chat_id", "analysis_type", "result", "schema_version", "created_at", "msg_time"]
        account.executemany(
            f"insert into analysis ({','.join(names)}) "
            f"values ({','.join('?' for _ in names)})",
            [tuple(item.get(name) for name in names) for item in analysis_plan],
        )
    meta.commit()
    account.commit()

    print(
        f"copied knowledge={len(knowledge_plan)} "
        f"messages={len(message_plan)} analysis={len(analysis_plan)}"
    )
    legacy.close()
    meta.close()
    account.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
