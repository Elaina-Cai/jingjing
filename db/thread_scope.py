"""线程来源字段的同源迁移；空值保留为未知，不根据当前读取者回填。"""
from __future__ import annotations

from sqlalchemy import text


SOURCE_COLUMNS = {
    "threads": ("platform", "account_id"),
    "thread_msg_ref": ("platform", "account_id", "chat_id"),
}


def source_key(record) -> tuple[str, str, str] | None:
    key = tuple(str(getattr(record, name, "") or "").strip()
                for name in ("platform", "account_id", "chat_id"))
    return key if all(key) else None


def upgrade_thread_sources(connection) -> None:
    """在调用者事务内补来源列；纯账号库没有线程表时不创建它们。"""
    tables = set(connection.execute(text(
        "select name from sqlite_master where type='table'"
    )).scalars())
    for table, names in SOURCE_COLUMNS.items():
        if table not in tables:
            continue
        existing = {row[1] for row in connection.execute(text("PRAGMA table_info(%s)" % table))}
        for name in names:
            if name not in existing:
                connection.execute(text(
                    "ALTER TABLE %s ADD COLUMN %s TEXT NOT NULL DEFAULT ''" % (table, name)))


def require_same_source(*records) -> tuple[str, str, str]:
    """修改前统一校验；未知来源不能当成相同来源。"""
    keys = [source_key(record) for record in records]
    if not keys or keys[0] is None or any(key != keys[0] for key in keys):
        raise ValueError("thread_source_mismatch: 线程与引用必须具有一致的明确来源")
    return keys[0]


def resolve_source_message(unified_query, message_id: int, key):
    """按完整来源读取并复核结果；不进行跨账号或旧消息库回退。"""
    if unified_query is None or not key or len(key) != 3 or not all(key) or not message_id:
        return None
    result = unified_query.get_message_by_id(int(message_id), platform=key[0], account_id=key[1])
    data = getattr(result, "data", None)
    actual = (str(getattr(result, "platform", "") or ""),
              str(getattr(result, "account_id", "") or ""),
              str(getattr(data, "chat_id", "") or ""))
    return data if actual == tuple(key) else None


def resolve_thread_ref(unified_query, thread, ref):
    key = require_same_source(thread, ref)
    if getattr(ref, "thread_id", None) != getattr(thread, "id", None):
        raise ValueError("thread_ref_parent_mismatch")
    return resolve_source_message(unified_query, getattr(ref, "msg_id", 0), key)


def upgrade_thread_summaries(connection) -> None:
    """摘要保存在有来源的父线程，不猜测旧 Topic 的对应关系。"""
    if connection.execute(text("select 1 from sqlite_master where type='table' and name='threads'")).first() is None:
        return
    columns = {row[1] for row in connection.execute(text("PRAGMA table_info(threads)"))}
    if "summary" not in columns:
        connection.execute(text("ALTER TABLE threads ADD COLUMN summary TEXT"))
