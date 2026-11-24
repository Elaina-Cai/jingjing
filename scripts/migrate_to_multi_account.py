# 【历史工具 · 2026-09-21 归档】旧单库 data/yinyue.db 与 meta `memories` 表已按主人指令删除，
# 本脚本已完成使命，仅保留作审计/回溯参考；不要再在生产流程中运行。
"""
数据迁移脚本：从旧的单库 yinyue.db 迁移数据到新的元数据DB + 账号DB架构。

用法：
    python -m scripts.migrate_to_multi_account [--source PATH] [--platform wechat] [--account-id WXID]

默认：
    --source     ./data/yinyue.db
    --platform   wechat
    --account-id 从 config.wechat.yinyue_wxid 或 config.wechat.self_wxid 读取
"""
from __future__ import annotations

import argparse
import sqlite3
import structlog
import json
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # 仅供下方模块级函数的参数类型注解。运行时不需要（本模块启用 PEP 563，
    # 且真正的导入发生在 migrate() 函数内部），放在 TYPE_CHECKING 下可让
    # 注解在静态分析时可解析、同时不改变运行时行为。
    from db.account_repo import AccountRepositoryManager
    from db.meta_repo import MetaRepositoryManager

log = structlog.get_logger()


def migrate(source_db: str, platform: str, account_id: str):
    from core.config.settings import config
    from db.meta_models import init_meta_db
    from db.meta_repo import MetaRepositoryManager
    from db.account_db_manager import AccountDBManager
    from db.account_repo import AccountRepositoryManager
    from db.account_models import init_account_db

    source = Path(source_db)
    if not source.exists():
        log.error("source_db_not_found", path=str(source))
        return

    meta_db_path = str(config.paths.meta_db_path)
    Path(meta_db_path).parent.mkdir(parents=True, exist_ok=True)
    init_meta_db(meta_db_path)
    meta_repo = MetaRepositoryManager(meta_db_path)

    adm = AccountDBManager()
    account_config = config.build_account_config(platform, account_id)
    account_config.resolve_paths(config.paths)
    account_repo = adm.register(platform, account_id, account_config.db_path)

    meta_repo.account_registry.upsert(
        account_id=account_id,
        platform=platform,
        role="yinyue",
        data_dir=str(config.paths.get_account_dir(platform, account_id)),
        enabled=1,
    )

    src_conn = sqlite3.connect(str(source))
    src_conn.row_factory = sqlite3.Row

    _migrate_contacts(src_conn, account_repo, account_id)
    _migrate_messages(src_conn, account_repo, account_id)
    _migrate_analysis(src_conn, account_repo, account_id)
    _migrate_send_logs(src_conn, account_repo, account_id)
    _migrate_action_logs(src_conn, account_repo, account_id)

    _migrate_personas(src_conn, meta_repo)
    _migrate_memories(src_conn, meta_repo, platform, account_id)
    _migrate_conversations(src_conn, meta_repo)
    _migrate_reminders(src_conn, meta_repo)
    _migrate_knowledge(src_conn, meta_repo)

    src_conn.close()
    log.info("migration_complete", platform=platform, account_id=account_id)


def _migrate_contacts(src: sqlite3.Connection, account_repo: AccountRepositoryManager, account_id: str):
    try:
        rows = src.execute("SELECT * FROM contacts").fetchall()
    except Exception:
        log.warning("no_contacts_table")
        return
    count = 0
    for r in rows:
        try:
            account_repo.contacts.upsert_from_dict(dict(r), account_id=account_id)
            count += 1
        except Exception as e:
            log.warning("contact_migrate_error", wxid=r.get("wxid", "?"), error=str(e))
    log.info("contacts_migrated", count=count)


def _migrate_messages(src: sqlite3.Connection, account_repo: AccountRepositoryManager, account_id: str):
    try:
        rows = src.execute("SELECT * FROM messages ORDER BY msg_id").fetchall()
    except Exception:
        log.warning("no_messages_table")
        return
    count = 0
    batch = []
    for r in rows:
        d = dict(r)
        d["account_id"] = account_id
        batch.append(d)
        if len(batch) >= 500:
            try:
                account_repo.messages.bulk_insert(batch)
                count += len(batch)
            except Exception as e:
                log.warning("message_batch_error", error=str(e))
            batch = []
    if batch:
        try:
            account_repo.messages.bulk_insert(batch)
            count += len(batch)
        except Exception as e:
            log.warning("message_batch_error", error=str(e))
    log.info("messages_migrated", count=count)


def _migrate_analysis(src: sqlite3.Connection, account_repo: AccountRepositoryManager, account_id: str):
    try:
        rows = src.execute("SELECT * FROM analysis").fetchall()
    except Exception:
        return
    count = 0
    for r in rows:
        try:
            d = dict(r)
            d["account_id"] = account_id
            account_repo.analysis.add_from_dict(d)
            count += 1
        except Exception:
            pass
    log.info("analysis_migrated", count=count)


def _migrate_send_logs(src: sqlite3.Connection, account_repo: AccountRepositoryManager, account_id: str):
    try:
        rows = src.execute("SELECT * FROM send_logs").fetchall()
    except Exception:
        return
    count = 0
    for r in rows:
        try:
            d = dict(r)
            d["account_id"] = account_id
            account_repo.send_log.add_from_dict(d)
            count += 1
        except Exception:
            pass
    log.info("send_logs_migrated", count=count)


def _migrate_action_logs(src: sqlite3.Connection, account_repo: AccountRepositoryManager, account_id: str):
    try:
        rows = src.execute("SELECT * FROM action_logs").fetchall()
    except Exception:
        return
    count = 0
    for r in rows:
        try:
            d = dict(r)
            d["account_id"] = account_id
            account_repo.action_log.add_from_dict(d)
            count += 1
        except Exception:
            pass
    log.info("action_logs_migrated", count=count)


def _migrate_personas(src: sqlite3.Connection, meta_repo: MetaRepositoryManager):
    try:
        rows = src.execute("SELECT * FROM personas").fetchall()
    except Exception:
        return
    count = 0
    for r in rows:
        try:
            meta_repo.personas.upsert_from_dict(dict(r))
            count += 1
        except Exception:
            pass
    log.info("personas_migrated", count=count)


def _migrate_memories(src: sqlite3.Connection, meta_repo: MetaRepositoryManager, platform: str, account_id: str):
    try:
        rows = src.execute("SELECT * FROM memories").fetchall()
    except Exception:
        return
    count = 0
    for r in rows:
        try:
            d = dict(r)
            d["source_platform"] = platform
            d["source_account_id"] = account_id
            # 2026-09-29：legacy memories 表已随 Migration 49 退役（读入口早已迁 memory_projections）
            continue
        except Exception:
            pass
    log.info("memories_migrated", count=count)


def _migrate_conversations(src: sqlite3.Connection, meta_repo: MetaRepositoryManager):
    try:
        rows = src.execute("SELECT * FROM conversations").fetchall()
    except Exception:
        return
    count = 0
    for r in rows:
        try:
            meta_repo.conversations.add_from_dict(dict(r))
            count += 1
        except Exception:
            pass
    log.info("conversations_migrated", count=count)


def _migrate_reminders(src: sqlite3.Connection, meta_repo: MetaRepositoryManager):
    try:
        rows = src.execute("SELECT * FROM reminders").fetchall()
    except Exception:
        return
    count = 0
    for r in rows:
        try:
            meta_repo.reminders.add_from_dict(dict(r))
            count += 1
        except Exception:
            pass
    log.info("reminders_migrated", count=count)


def _migrate_knowledge(src: sqlite3.Connection, meta_repo: MetaRepositoryManager):
    try:
        rows = src.execute("SELECT * FROM knowledge_docs").fetchall()
    except Exception:
        return
    count = 0
    for r in rows:
        try:
            meta_repo.knowledge.add_from_dict(dict(r))
            count += 1
        except Exception:
            pass
    log.info("knowledge_migrated", count=count)


def main():
    parser = argparse.ArgumentParser(description="迁移单库数据到多账号架构")
    parser.add_argument("--source", default="./data/yinyue.db", help="源数据库路径")
    parser.add_argument("--platform", default="wechat", help="平台标识")
    parser.add_argument("--account-id", default="", help="账号ID，默认从配置读取")
    args = parser.parse_args()

    from core.config.settings import config
    account_id = args.account_id
    if not account_id:
        account_id = config.wechat.yinyue_wxid or config.wechat.self_wxid or ""
    if not account_id:
        log.error("no_account_id", hint="请通过 --account-id 指定或在配置中设置 yinyue_wxid/self_wxid")
        return

    migrate(args.source, args.platform, account_id)


if __name__ == "__main__":
    main()
