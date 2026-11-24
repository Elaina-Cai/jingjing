"""隔离旧库的持久化投影停写闸；原始事件和旧记录保留，默认只预检。"""
from __future__ import annotations
import sqlite3
from datetime import datetime
from contextlib import nullcontext
import json
from core.memory.legacy_audit import _hash
from core.memory.legacy_acceptance import verify_import
from core.memory.legacy_import import _target


def _is_belief_reader(target) -> bool:
    from pathlib import Path
    from core.memory.belief_commit import database_path
    return Path(target).resolve() == Path(database_path()).resolve()

MARKER = "legacy_projection_write_owner"
TRIGGERS = {"legacy_projection_" + action.lower() + "_frozen":
    "CREATE TRIGGER legacy_projection_" + action.lower() + "_frozen BEFORE " + action +
    " ON memory_projections BEGIN SELECT RAISE(ABORT,'legacy_projection_writes_frozen_use_cognitive_commit'); END"
    for action in ("INSERT", "UPDATE", "DELETE")}


def verify_freeze_owner(source, *, source_id, scope_key, target) -> bool:
    """只读核对持久化写权及完整触发器；缺闸、错绑不能冒充停写完成。"""
    owner = (source_id, scope_key, _hash(str(target.resolve())))
    conn = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (MARKER,)).fetchone()
        triggers = {name: conn.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)).fetchone() for name in TRIGGERS}
        if not exists:
            if any(triggers.values()):
                raise ValueError("legacy_projection_freeze_owner_missing")
            return False
        if conn.execute("SELECT source_id,scope_key,target_fingerprint FROM legacy_projection_write_owner WHERE singleton=1").fetchone() != owner:
            raise ValueError("legacy_projection_freeze_owner_conflict")
        if any(row is None or row[0].strip() != TRIGGERS[name] for name, row in triggers.items()):
            raise ValueError("legacy_projection_freeze_incomplete")
        return True
    finally:
        conn.close()


class LegacyProjectionFrozen(ValueError):
    """只有完整、持久化的旧投影停写闸能产生此状态。"""


def projection_retired(repository) -> bool:
    checker = getattr(repository, "assert_writable", None)
    if not callable(checker):
        return False
    try:
        checker()
    except LegacyProjectionFrozen:
        return True
    return False


def assert_projection_writable(engine, *, connection=None) -> None:
    """上层预检；可复用写事务连接，避免游标检查与提交之间插入停写。"""
    with (nullcontext(connection) if connection is not None else engine.connect()) as conn:
        if not conn.exec_driver_sql("SELECT 1 FROM sqlite_master WHERE type='table' AND name='legacy_projection_write_owner'").first():
            if conn.exec_driver_sql("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name IN (?,?,?)", tuple(TRIGGERS)).first():
                raise ValueError("legacy_projection_freeze_owner_missing")
            return
        owner = conn.exec_driver_sql("SELECT source_id,scope_key,target_fingerprint FROM legacy_projection_write_owner WHERE singleton=1").first()
        if owner is None or not all(isinstance(value, str) and value for value in owner):
            raise ValueError("legacy_projection_freeze_owner_invalid")
        scope = json.loads(owner[1])
        if not isinstance(scope, list) or len(scope) != 3 or not all(isinstance(value, str) and value for value in scope):
            raise ValueError("legacy_projection_freeze_owner_invalid")
        for name, sql in TRIGGERS.items():
            row = conn.exec_driver_sql("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)).first()
            if row is None or row[0].strip() != sql:
                raise ValueError("legacy_projection_freeze_incomplete")
        raise LegacyProjectionFrozen("legacy_projection_writes_frozen_use_cognitive_commit")


def _source_path(binding):
    from pathlib import Path
    source = Path(binding.source).resolve(strict=True)
    target = Path(binding.target).resolve()
    if source == target or (target.exists() and source.samefile(target)):
        raise ValueError("source_and_target_must_differ")
    return source


def freeze_projection_writes(binding, *, dry_run=True) -> dict:
    source = _source_path(binding)
    from pathlib import Path
    from core.context import cognitive_commit as cc
    from core.memory.belief_commit import database_path
    if Path(database_path()).resolve() != binding.target:
        raise ValueError("legacy_freeze_belief_target_changed")
    stat = source.stat()
    if (stat.st_dev, stat.st_ino) != binding.file_identity:
        raise ValueError("legacy_freeze_source_replaced")
    from core.memory.legacy_ownership import binding_scope
    if binding_scope(source, binding.target, binding.source_id):
        raise ValueError("legacy_freeze_target_memory_retired")
    # 对象本身不能作为授权凭据；重新核对当前源数据及隔离导入日志。
    report = verify_import(source, source_id=binding.source_id, target=binding.target,
                           production=_is_belief_reader(binding.target))
    scope = report["scope"]
    if cc._scope_key(cc.Scope(scope["platform"], scope["account_id"], scope["chat_id"])) != binding.scope_key:
        raise ValueError("legacy_freeze_scope_conflict")
    if not report["integrity_ok"]:
        raise ValueError("legacy_freeze_import_integrity_failed")
    owner = (binding.source_id, binding.scope_key, _hash(str(binding.target.resolve())))
    conn = sqlite3.connect(source.as_uri() + ("?mode=ro" if dry_run else "?mode=rw"), uri=True)
    try:
        conn.execute("PRAGMA query_only=" + ("ON" if dry_run else "OFF"))
        conn.execute("BEGIN" if dry_run else "BEGIN IMMEDIATE")
        # 正式停写前持有源库写锁重验；其他写者不能在检查与建闸之间插入尾差。
        locked_report = verify_import(source, source_id=binding.source_id, target=binding.target,
                                      production=_is_belief_reader(binding.target))
        if not locked_report["integrity_ok"]:
            raise ValueError("legacy_freeze_source_changed")
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (MARKER,)).fetchone()
        if exists:
            if conn.execute("SELECT source_id,scope_key,target_fingerprint FROM legacy_projection_write_owner WHERE singleton=1").fetchone() != owner:
                raise ValueError("legacy_freeze_owner_conflict")
        existing_triggers = 0
        for name, sql in TRIGGERS.items():
            old = conn.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)).fetchone()
            if old is not None and old[0].strip() != sql:
                raise ValueError("legacy_freeze_trigger_conflict")
            existing_triggers += old is not None
        rows = conn.execute("SELECT COUNT(*) FROM memory_projections").fetchone()[0]
        if not dry_run:
            conn.execute("CREATE TABLE IF NOT EXISTS legacy_projection_write_owner(singleton INTEGER PRIMARY KEY CHECK(singleton=1),source_id TEXT NOT NULL,scope_key TEXT NOT NULL,target_fingerprint TEXT NOT NULL)")
            conn.execute("INSERT OR IGNORE INTO legacy_projection_write_owner VALUES(1,?,?,?)", owner)
            for name, sql in TRIGGERS.items():
                if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?", (name,)).fetchone():
                    conn.execute(sql)
            conn.commit()
        return {"dry_run": dry_run, "frozen": not dry_run or (bool(exists) and existing_triggers == len(TRIGGERS)), "already_marked": bool(exists),
                "source_id": binding.source_id, "preserved_projection_rows": rows,
                "notice": "隔离旧库整张记忆投影表停写，未导入记录仍保留只读；原始事件继续由事件库承载。不代表生产迁移已完成，不自动解冻。"}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


UNFREEZE_LOG = "legacy_projection_unfreeze_log"
UNFREEZE_LOG_SQL = """
CREATE TABLE IF NOT EXISTS legacy_projection_unfreeze_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    target_fingerprint TEXT NOT NULL,
    reason TEXT NOT NULL,
    at TEXT NOT NULL
)
"""


def unfreeze_projection_writes(binding, *, dry_run=True, reason="", confirm="") -> dict:
    """显式、可审计地撤销隔离旧库的整表停写闸。

    这不是静默回退：必须给出非空 reason，并提交 ``unfreeze:<scope_key>`` 确认串；
    成功时先写审计日志，再删除三个触发器和 owner 表。旧投影行与原始事件保持原样，
    不会恢复整个生产库、重置队列或重发历史动作。
    """
    from pathlib import Path
    from core.memory.belief_commit import database_path

    source = _source_path(binding)
    if Path(database_path()).resolve() != binding.target:
        raise ValueError("legacy_unfreeze_belief_target_changed")
    stat = source.stat()
    if (stat.st_dev, stat.st_ino) != binding.file_identity:
        raise ValueError("legacy_unfreeze_source_replaced")
    text = str(reason or "").strip()
    if not text:
        raise ValueError("legacy_unfreeze_reason_required")
    expected = "unfreeze:" + binding.scope_key
    if str(confirm or "") != expected:
        raise ValueError("legacy_unfreeze_confirm_required")
    owner = (binding.source_id, binding.scope_key, _hash(str(binding.target.resolve())))
    conn = sqlite3.connect(source.as_uri() + ("?mode=ro" if dry_run else "?mode=rw"), uri=True)
    try:
        conn.execute("PRAGMA query_only=" + ("ON" if dry_run else "OFF"))
        if not dry_run:
            conn.execute("ATTACH DATABASE ? AS rollback_target", (str(binding.target),))
            modes = [conn.execute("PRAGMA " + schema + ".journal_mode").fetchone()[0]
                     for schema in ("main", "rollback_target")]
            if any(mode not in {"delete", "truncate", "persist"} for mode in modes):
                raise ValueError("legacy_rollback_requires_rollback_journals")
        conn.execute("BEGIN" if dry_run else "BEGIN IMMEDIATE")
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (MARKER,)
        ).fetchone()
        triggers = {
            name: conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)
            ).fetchone()
            for name in TRIGGERS
        }
        if not exists:
            if any(triggers.values()):
                raise ValueError("legacy_unfreeze_owner_missing")
            return {
                "dry_run": dry_run,
                "unfrozen": False,
                "already_unfrozen": True,
                "removed_triggers": 0,
                "preserved_projection_rows": 0,
                "source_id": binding.source_id,
                "scope_key": binding.scope_key,
                "reason": text,
            }
        row = conn.execute(
            "SELECT source_id,scope_key,target_fingerprint "
            "FROM legacy_projection_write_owner WHERE singleton=1"
        ).fetchone()
        if row != owner:
            raise ValueError("legacy_unfreeze_owner_conflict")
        if any(
            value is None or value[0].strip() != TRIGGERS[name]
            for name, value in triggers.items()
        ):
            raise ValueError("legacy_unfreeze_incomplete")
        rows = conn.execute("SELECT COUNT(*) FROM memory_projections").fetchone()[0]
        if not dry_run:
            from core.memory.legacy_ownership import retire_attached_target
            retire_attached_target(conn, binding, text)
            conn.execute(UNFREEZE_LOG_SQL)
            conn.execute(
                "INSERT INTO legacy_projection_unfreeze_log "
                "(source_id,scope_key,target_fingerprint,reason,at) VALUES(?,?,?,?,?)",
                (
                    binding.source_id,
                    binding.scope_key,
                    owner[2],
                    text,
                    datetime.now().isoformat(sep=" ", timespec="seconds"),
                ),
            )
            for name in TRIGGERS:
                conn.execute("DROP TRIGGER IF EXISTS " + name)
            conn.execute("DROP TABLE legacy_projection_write_owner")
            conn.commit()
        return {
            "dry_run": dry_run,
            "unfrozen": not dry_run,
            "already_unfrozen": False,
            "removed_triggers": len(TRIGGERS) if not dry_run else 0,
            "preserved_projection_rows": rows,
            "source_id": binding.source_id,
            "scope_key": binding.scope_key,
            "reason": text,
            "target_memory_retired": not dry_run,
            "notice": "同一事务退役新库该范围的事实写入，再恢复旧投影写权；保留新日志，旧读取绑定按持久化属主恢复。",
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
