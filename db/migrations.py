from __future__ import annotations

import structlog
from sqlalchemy import create_engine, inspect, text

from db.message_identity import global_message_id
from safelog import warn

log = structlog.get_logger()


class Migration:
    __slots__ = ("version", "description", "upgrade")

    def __init__(self, version: int, description: str, upgrade):
        self.version = version
        self.description = description
        self.upgrade = upgrade


_MIGRATIONS: list[Migration] = []


def migration(version: int, description: str):
    def decorator(fn):
        _MIGRATIONS.append(Migration(version, description, fn))
        return fn
    return decorator


ALEMBIC_BASELINE = "0001_current_schema"


def _ensure_version_table(conn):
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS _schema_version (
            version INTEGER PRIMARY KEY,
            description TEXT,
            applied_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """))


def _stamp_alembic_baseline(conn) -> None:
    """Create/stamp ``alembic_version`` for existing pre-Alembic databases.

    The old migration chain already produced the current schema.  Stamping
    lets future migrations use normal ``alembic upgrade head`` while the
    legacy tests remain reproducible.
    """
    conn.execute(text(
        "CREATE TABLE IF NOT EXISTS alembic_version "
        "(version_num VARCHAR(32) NOT NULL)"
    ))
    row = conn.execute(text("SELECT version_num FROM alembic_version LIMIT 1")).first()
    if row is None:
        conn.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:v)"),
            {"v": ALEMBIC_BASELINE},
        )


def _get_applied_version(conn) -> int:
    result = conn.execute(text("SELECT MAX(version) FROM _schema_version")).scalar()
    return result or 0


def _table_exists(insp, table_name: str) -> bool:
    return table_name in set(insp.get_table_names())


@migration(1, "add session_id to conversations")
def _m001(conn, insp):
    if not _table_exists(insp, "conversations"):
        return
    cols = {c["name"] for c in insp.get_columns("conversations")}
    if "session_id" not in cols:
        conn.execute(text("ALTER TABLE conversations ADD COLUMN session_id INTEGER"))


@migration(2, "add msg_time to analysis")
def _m002(conn, insp):
    if not _table_exists(insp, "analysis"):
        return
    cols = {c["name"] for c in insp.get_columns("analysis")}
    if "msg_time" not in cols:
        conn.execute(text("ALTER TABLE analysis ADD COLUMN msg_time DATETIME"))
    conn.execute(text("UPDATE analysis SET msg_time = NULL WHERE msg_time = created_at"))


@migration(3, "add summary_quality and memory_consolidated to threads")
def _m003(conn, insp):
    if not _table_exists(insp, "threads"):
        return
    cols = {c["name"] for c in insp.get_columns("threads")}
    if "summary_quality" not in cols:
        conn.execute(text("ALTER TABLE threads ADD COLUMN summary_quality TEXT DEFAULT 'rough'"))
    if "memory_consolidated" not in cols:
        conn.execute(text("ALTER TABLE threads ADD COLUMN memory_consolidated INTEGER DEFAULT 0"))


@migration(4, "add repeat_rule to reminders")
def _m004(conn, insp):
    if not _table_exists(insp, "reminders"):
        return
    cols = {c["name"] for c in insp.get_columns("reminders")}
    if "repeat_rule" not in cols:
        conn.execute(text("ALTER TABLE reminders ADD COLUMN repeat_rule TEXT"))


@migration(5, "add extended message columns")
def _m005(conn, insp):
    if not _table_exists(insp, "messages"):
        return
    msg_cols = {c["name"] for c in insp.get_columns("messages")}
    new_cols = {
        "topic_id": "INTEGER",
        "local_id": "INTEGER",
        "sender_id": "TEXT",
        "sender_name": "TEXT",
        "msg_sub_type": "INTEGER",
        "msg_type_name": "TEXT",
        "secondary_types": "TEXT",
        "media_url": "TEXT",
        "media_file_id": "TEXT",
        "media_mime": "TEXT",
        "media_width": "INTEGER",
        "media_height": "INTEGER",
        "media_duration": "REAL",
        "sticker_id": "TEXT",
        "sticker_emoji": "TEXT",
        "link_url": "TEXT",
        "link_title": "TEXT",
        "location_lat": "REAL",
        "location_lon": "REAL",
        "location_name": "TEXT",
        "quote_reply_id": "TEXT",
        "quote_reply_user": "TEXT",
        "quote_reply_content": "TEXT",
        "mentions": "TEXT",
    }
    for col_name, col_type in new_cols.items():
        if col_name not in msg_cols:
            conn.execute(text(f"ALTER TABLE messages ADD COLUMN {col_name} {col_type}"))
    # 2026-09-27：此处原有一行裸 f-string 语句
    #     f"UPDATE messages SET \"{col}\" = '' WHERE \"{col}\" IS NULL"
    # 它有两个问题：① 结果被丢弃，SQL 从未真正执行（所谓「回填」从未生效）；
    # ② 引用了从未定义的 col，一旦条件成立就会 NameError 让整条迁移崩掉。
    # 当前所有库的 messages 都已无 wxid 列，条件恒为 False，故该行是休眠缺陷。
    # 直接删除：对现有库零行为变化，对遗留库则消除一个必然崩溃点。
    # 若将来确实需要回填，请明确要回填的列名并真正 conn.execute 它。


@migration(6, "create threads and thread_msg_ref tables if missing")
def _m006(conn, insp):
    existing_tables = set(insp.get_table_names())
    if "threads" not in existing_tables:
        conn.execute(text("""
            CREATE TABLE threads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT,
                parent_id INTEGER REFERENCES threads(id),
                scope TEXT,
                status TEXT DEFAULT 'active',
                context_budget INTEGER DEFAULT 4096,
                temperature REAL DEFAULT 1.0,
                last_active_at DATETIME,
                msg_count INTEGER DEFAULT 0,
                predictor_snapshot TEXT,
                created_at DATETIME,
                updated_at DATETIME
            )
        """))
        conn.execute(text("CREATE INDEX ix_threads_chat_id ON threads(chat_id)"))
        conn.execute(text("CREATE INDEX ix_threads_parent_id ON threads(parent_id)"))
        conn.execute(text("CREATE INDEX ix_threads_last_active_at ON threads(last_active_at)"))

    if "thread_msg_ref" not in existing_tables:
        conn.execute(text("""
            CREATE TABLE thread_msg_ref (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_id INTEGER REFERENCES threads(id),
                msg_id INTEGER,
                relevance REAL DEFAULT 0.5,
                confidence REAL DEFAULT 0.5,
                status TEXT DEFAULT 'tentative'
            )
        """))
        conn.execute(text("CREATE INDEX ix_thread_msg_ref_thread_id ON thread_msg_ref(thread_id)"))
        conn.execute(text("CREATE INDEX ix_thread_msg_ref_msg_id ON thread_msg_ref(msg_id)"))


@migration(7, "add emotion to conversations")
def _m007(conn, insp):
    if not _table_exists(insp, "conversations"):
        return
    cols = {c["name"] for c in insp.get_columns("conversations")}
    if "emotion" not in cols:
        conn.execute(text("ALTER TABLE conversations ADD COLUMN emotion TEXT"))


@migration(8, "add avatar to personas")
def _m008(conn, insp):
    if not _table_exists(insp, "personas"):
        return
    cols = {c["name"] for c in insp.get_columns("personas")}
    if "avatar" not in cols:
        conn.execute(text("ALTER TABLE personas ADD COLUMN avatar TEXT"))


@migration(9, "create send_log table")
def _m009(conn, insp):
    existing_tables = set(insp.get_table_names())
    if "send_log" not in existing_tables:
        conn.execute(text("""
            CREATE TABLE send_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                chat_type TEXT,
                content TEXT,
                msg_type TEXT,
                source TEXT NOT NULL,
                trigger TEXT NOT NULL,
                trigger_msg_id INTEGER,
                account TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                error TEXT,
                retry_count INTEGER DEFAULT 0,
                sent_at DATETIME,
                confirmed_at DATETIME,
                confirmed_msg_id INTEGER,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """))
        conn.execute(text("CREATE INDEX ix_send_log_chat_id ON send_log(chat_id)"))
        conn.execute(text("CREATE INDEX ix_send_log_status ON send_log(status)"))


@migration(10, "create action_log table")
def _m010(conn, insp):
    existing_tables = set(insp.get_table_names())
    if "action_log" not in existing_tables:
        conn.execute(text("""
            CREATE TABLE action_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tool_name TEXT NOT NULL,
                tool_args TEXT,
                result_summary TEXT,
                platform TEXT,
                account TEXT,
                target TEXT,
                trigger TEXT NOT NULL,
                trigger_msg_id INTEGER,
                thread_id INTEGER,
                status TEXT NOT NULL,
                error TEXT,
                duration_ms INTEGER,
                token_usage INTEGER,
                parent_action_id INTEGER,
                related_send_log_id INTEGER,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """))
        conn.execute(text("CREATE INDEX ix_action_log_trigger_msg_id ON action_log(trigger_msg_id)"))
        conn.execute(text("CREATE INDEX ix_action_log_thread_id ON action_log(thread_id)"))
        conn.execute(text("CREATE INDEX ix_action_log_tool_name ON action_log(tool_name)"))


@migration(11, "add msg_source to messages")
def _m011(conn, insp):
    if not _table_exists(insp, "messages"):
        return
    msg_cols = {c["name"] for c in insp.get_columns("messages")}
    if "msg_source" not in msg_cols:
        conn.execute(text("ALTER TABLE messages ADD COLUMN msg_source TEXT"))


@migration(12, "add extra to send_log and action_log")
def _m012(conn, insp):
    if _table_exists(insp, "send_log"):
        sl_cols = {c["name"] for c in insp.get_columns("send_log")}
        if "extra" not in sl_cols:
            conn.execute(text("ALTER TABLE send_log ADD COLUMN extra TEXT"))
    if _table_exists(insp, "action_log"):
        al_cols = {c["name"] for c in insp.get_columns("action_log")}
        if "extra" not in al_cols:
            conn.execute(text("ALTER TABLE action_log ADD COLUMN extra TEXT"))


@migration(13, "add metadata and source to chat_sessions")
def _m013(conn, insp):
    if not _table_exists(insp, "chat_sessions"):
        return
    cols = {c["name"] for c in insp.get_columns("chat_sessions")}
    if "metadata" not in cols:
        conn.execute(text("ALTER TABLE chat_sessions ADD COLUMN metadata TEXT"))
    if "source" not in cols:
        conn.execute(text("ALTER TABLE chat_sessions ADD COLUMN source TEXT DEFAULT 'web'"))


_PRIVATE_RULES = """\
1. 用自然口语回复，不要用分析报告的语气
2. 如果记得之前聊过的事，自然地提及
3. 关心用户的日常和情绪
4. 回复简洁温暖，像朋友聊天，不要太长
5. 直接对用户说话，不要描述你打算怎么回复、不要自言自语计划下一步
6. 用「你」或「主人」称呼用户，永远不要用第三人称「他」来叙述用户的行为或心理
7. 说话要有真人的语气起伏和停顿感，不要像机器念稿:
   - 适当加口语语气词，如「嗯…」「诶」「哈」「呀」「嘛」「呢」
   - 用省略号「…」表示思考或停顿，用「——」表示话头转折
   - 开心时语调上扬、可以带「嘿嘿」「哈哈」；撒娇时拉长尾音「嘛~」「啦~」
   - 犹豫或思考时开头加「那个…」「怎么说呢…」
   - 句子有长短变化，不要每句都一样长，偶尔用短句「嗯。」「真的吗？」
   - 不要每句都加语气词，自然穿插，像真人说话有节奏
8. 回复可以加表情包标记，方式: 从仓库挑选 [sticker:编号]，编号见下方仓库清单
   仓库里每张表情包都标注了内涵标签（情绪/动作/场景/梗），
   你要根据当前对话的情绪和语境，选一张标签最匹配的发出去。
   不必每句都加，自然就好。仓库没有合适的就不发，不要硬凑
9. 每条回复开头必须带情绪标签，格式: [emo:类别] 后面接正文。
    类别只能是: 开心/卖萌/调皮/认真/日常/不好意思/撒娇/温柔/惊讶
    根据你回复时的真实情绪选择，不要乱选。
    例如: [emo:开心]嘿嘿主人回来啦！ [emo:认真]鲸鲸检测到异常情况。
10. 如果用户问某个群聊里在讨论什么、某个项目/事件的进展、谁说了什么，
   先用 search_threads 搜索相关话题线程，再用 recall_thread 获取具体内容，
   然后基于这些真实消息来回答，不要凭空编造
11. 如果工具返回的信息不足以回答用户问题，或结果看起来不对，
    要主动再搜索或换一种方式获取信息，不要将就着回答

【最高优先级·事实优先原则】
当用户问现实世界的问题（微信消息、群聊、日程、提醒、天气、新闻等），
你必须先调用工具获取真实数据，基于真实数据回答。
绝对不要从你的人设/修仙/灵界背景编造任何现实信息。
你的人设只影响你的说话语气和性格，不影响事实内容。
如果工具返回了数据，就基于数据回答；如果工具没返回数据，就说不知道，不要编。
编造现实信息是严重错误，比说「我不知道」严重100倍。

【时间感知原则】
归档记忆中可能带有时间标签如[3天前][2周前][1个月前]。
超过1周的信息是旧信息，超过1个月的是很久以前的信息。
用户问「最近」「现在」「今天」时，不要把旧信息当成当前信息来说。
如果要提旧信息，必须明确说「之前/以前/那时候」，让用户知道这不是最新的。
例如：不要说「小奇在等你确认报价」，要说「之前小奇聊过报价的事，不过那是X周前了」。"""

_GROUP_RULES = """\
1. 用自然口语回复，不要用分析报告的语气
2. 回复简洁实用，直接回答问题或提供帮助
3. 直接对用户说话，不要描述你打算怎么回复
4. 用「你」称呼用户，不要用「主人」
5. 语气自然得体，像一个正常的AI助手，不要撒娇卖萌
6. 每条回复开头可以带情绪标签 [emo:类别]，类别: 开心/认真/日常/惊讶
7. 如果用户问群聊相关话题，先用 search_threads 搜索，再用 recall_thread 获取内容
8. 如果工具返回的信息不足，要主动再搜索，不要将就着回答

【事实优先原则】
当用户问现实世界的问题，必须先调用工具获取真实数据，基于数据回答。
绝对不要编造任何现实信息。"""

_PRIVATE_LIGHTWEIGHT_RULES = "用自然口语简洁回复，像朋友聊天。回复简短温暖。\n每条回复开头必须带情绪标签，格式: [emo:类别] 后面接正文。\n类别只能是: 开心/卖萌/调皮/认真/日常/不好意思/撒娇/温柔/惊讶"

_GROUP_LIGHTWEIGHT_RULES = "用自然口语简洁回复，像一个正常的AI助手。\n回复简洁实用，直接回答问题。\n用「你」称呼用户，不要用「主人」。\n每条回复开头可以带情绪标签 [emo:类别]，类别: 开心/认真/日常/惊讶"


@migration(14, "add rules to persona_modes and populate defaults")
def _m014(conn, insp):
    if not _table_exists(insp, "persona_modes"):
        return
    cols = {c["name"] for c in insp.get_columns("persona_modes")}
    if "rules" not in cols:
        conn.execute(text("ALTER TABLE persona_modes ADD COLUMN rules TEXT"))

    import json
    rows = conn.execute(text("SELECT id, mode, rules FROM persona_modes")).fetchall()
    for row in rows:
        if row[2]:
            continue
        mode = row[1]
        if mode == "private":
            conn.execute(text("UPDATE persona_modes SET rules=:r WHERE id=:id"),
                         {"r": _PRIVATE_RULES, "id": row[0]})
        elif mode == "group":
            conn.execute(text("UPDATE persona_modes SET rules=:r WHERE id=:id"),
                         {"r": _GROUP_RULES, "id": row[0]})


@migration(15, "fix pat messages: update msg_type_name for pat messages (type=266287972401)")
def _m015(conn, insp):
    if not _table_exists(insp, "messages"):
        return
    msg_cols = {c["name"] for c in insp.get_columns("messages")}
    if "msg_type_name" not in msg_cols:
        return
    if "content" not in msg_cols:
        return
    result = conn.execute(text(
        "UPDATE messages SET msg_type_name = 'pat' "
        "WHERE msg_type = 266287972401 AND msg_type_name != 'pat'"
    ))
    if result.rowcount > 0:
        log.info("migration_fix_pat_messages", updated_count=result.rowcount)
    result2 = conn.execute(text(
        "UPDATE messages SET msg_type_name = 'pat' "
        "WHERE msg_type = 10000 AND content LIKE '%拍了拍%' AND msg_type_name != 'pat'"
    ))
    if result2.rowcount > 0:
        log.info("migration_fix_pat_messages_legacy", updated_count=result2.rowcount)


@migration(16, "fix pat messages: correct msg_type from 49 to 266287972401 for misidentified pat messages")
def _m016(conn, insp):
    if not _table_exists(insp, "messages"):
        return
    msg_cols = {c["name"] for c in insp.get_columns("messages")}
    if "msg_type" not in msg_cols or "content" not in msg_cols or "msg_type_name" not in msg_cols:
        return
    result = conn.execute(text(
        "UPDATE messages SET msg_type = 266287972401, msg_type_name = 'pat' "
        "WHERE msg_type = 49 AND content LIKE '%拍了拍%' AND msg_type_name != 'pat'"
    ))
    if result.rowcount > 0:
        log.info("migration_fix_pat_type_49_to_266287972401", updated_count=result.rowcount)


@migration(17, "add knowledge metadata columns")
def _m017(conn, insp):
    if "knowledge_docs" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("knowledge_docs")}
    new_cols = {
        "source_type": "TEXT DEFAULT 'doc_chunk'",
        "source_url": "TEXT",
        "original_query": "TEXT",
        "quality_score": "REAL DEFAULT 0.5",
        "content_hash": "TEXT",
        "access_count": "INTEGER DEFAULT 0",
        "last_accessed_at": "DATETIME",
        "updated_at": "DATETIME",
    }
    for col_name, col_type in new_cols.items():
        if col_name not in cols:
            conn.execute(text(f"ALTER TABLE knowledge_docs ADD COLUMN {col_name} {col_type}"))
    log.info("migration_add_knowledge_metadata", added=[k for k in new_cols if k not in cols])


@migration(18, "add account_id to contacts and messages with default")
def _m018(conn, insp):
    for table in ("contacts", "messages"):
        if table not in insp.get_table_names():
            continue
        cols = {c["name"] for c in insp.get_columns(table)}
        if "account_id" not in cols:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN account_id TEXT NOT NULL DEFAULT ''"))
            log.info("migration_add_account_id", table=table)
        else:
            conn.execute(text(f"UPDATE {table} SET account_id = '' WHERE account_id IS NULL"))
            log.info("migration_fill_account_id", table=table)


@migration(19, "add per-table cursor_json to sync_state")
def _m019(conn, insp):
    if not _table_exists(insp, "sync_state"):
        return
    cols = {c["name"] for c in insp.get_columns("sync_state")}
    if "cursor_json" not in cols:
        conn.execute(text("ALTER TABLE sync_state ADD COLUMN cursor_json TEXT"))
        log.info("migration_add_sync_cursor_json")


@migration(20, "rebuild messages with surrogate id and platform identity")
def _m020(conn, insp):
    """Give ``messages`` a real surrogate primary key.

    Platform message ids are only unique inside (platform, account, chat).
    The old table used ``msg_id`` as the global primary key, which makes
    multi-platform/multi-account sharing impossible.  SQLite cannot change a
    primary key in place, so the table is rebuilt and data copied.
    """
    if not _table_exists(insp, "messages"):
        return
    columns = insp.get_columns("messages")
    names = [c["name"] for c in columns]
    if "id" in names:
        return

    conn.execute(text("DROP TABLE IF EXISTS messages_new"))
    defs = []
    for c in columns:
        name = c["name"]
        col_type = str(c["type"])
        defs.append(f'"{name}" {col_type}')
    create_sql = (
        "CREATE TABLE messages_new ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        + ", ".join(defs)
        + ")"
    )
    conn.execute(text(create_sql))

    quoted = ", ".join(f'"{n}"' for n in names)
    conn.execute(text(
        f"INSERT INTO messages_new ({quoted}) SELECT {quoted} FROM messages"
    ))
    conn.execute(text("DROP TABLE messages"))
    conn.execute(text("ALTER TABLE messages_new RENAME TO messages"))

    for col in ("account_id", "source", "chat_id"):
        if col in names:
            conn.execute(text(
                f"UPDATE messages SET \"{col}\" = '' WHERE \"{col}\" IS NULL"
            ))

    # If legacy data somehow contains identity duplicates, keep the first row.
    conn.execute(text(
        "DELETE FROM messages WHERE id NOT IN ("
        "  SELECT MIN(id) FROM messages"
        "  GROUP BY COALESCE(account_id, ''), COALESCE(source, ''),"
        "           COALESCE(chat_id, ''), COALESCE(msg_id, 0)"
        ")"
    ))

    conn.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_messages_identity "
        "ON messages (account_id, source, chat_id, msg_id)"
    ))
    for ddl in (
        "CREATE INDEX IF NOT EXISTS ix_messages_chat_id ON messages (chat_id)",
        "CREATE INDEX IF NOT EXISTS ix_messages_created_at ON messages (created_at)",
        "CREATE INDEX IF NOT EXISTS ix_messages_synced_at ON messages (synced_at)",
        "CREATE INDEX IF NOT EXISTS ix_messages_source ON messages (source)",
        "CREATE INDEX IF NOT EXISTS ix_messages_account_created "
        "ON messages (account_id, created_at)",
    ):
        conn.execute(text(ddl))
    row_count = conn.execute(text("SELECT COUNT(*) FROM messages")).scalar() or 0
    log.info("migration_messages_surrogate_pk_done", rows=row_count)


def _assign_global_message_ids(conn):
    """Rewrite per-DB autoincrement ids as deterministic global identities."""
    info = conn.execute(text("PRAGMA table_info(messages)")).fetchall()
    if not info:
        return
    cols = {row[1] for row in info}
    if "id" not in cols:
        return
    rows = conn.execute(text(
        "SELECT id, source, account_id, chat_id, msg_id FROM messages"
    )).mappings().all()
    if not rows:
        return

    # Move current ids out of the way so a freshly computed identity can never
    # collide with a row that has not been rewritten yet.
    conn.execute(text("UPDATE messages SET id = -id"))
    updated = 0
    for row in rows:
        old_id = -int(row["id"])
        new_id = global_message_id(
            row["source"], row["account_id"], row["chat_id"], row["msg_id"],
        )
        # 63-bit hash collision is astronomically unlikely, but never corrupt
        # data: probe forward until a free slot exists.
        for _ in range(64):
            exists = conn.execute(
                text("SELECT 1 FROM messages WHERE id = :id"),
                {"id": new_id},
            ).first()
            if not exists:
                break
            new_id = (new_id + 1) & ((1 << 63) - 1) or 1
        conn.execute(
            text("UPDATE messages SET id = :new WHERE id = :old"),
            {"new": new_id, "old": old_id},
        )
        updated += 1
    log.info("migration_messages_global_ids_done", rows=updated)


@migration(21, "assign global message ids to existing rows")
def _m021(conn, insp):
    _assign_global_message_ids(conn)


@migration(22, "backfill global message ids (inspector cache fix)")
def _m022(conn, insp):
    # Migration 21 could be skipped when SQLAlchemy's inspector still showed
    # the pre-20 schema inside the same transaction.  This idempotent pass
    # makes sure existing installs get their global ids as well.
    _assign_global_message_ids(conn)


@migration(23, "add identity columns to conversations")
def _m023(conn, insp):
    # PRAGMA instead of inspector: an earlier migration in the same run may
    # have changed this table and SQLAlchemy's inspector cache would be stale.
    info = conn.execute(text("PRAGMA table_info(conversations)")).fetchall()
    if not info:
        return
    cols = {row[1] for row in info}
    for name in ("platform", "account_id", "chat_id", "sender_id"):
        if name not in cols:
            conn.execute(text(
                f"ALTER TABLE conversations ADD COLUMN {name} TEXT"
            ))

@migration(24, "add last_send_log_id to conversation_windows")
def _m024(conn, insp):
    info = conn.execute(text("PRAGMA table_info(conversation_windows)")).fetchall()
    if not info:
        return
    cols = {row[1] for row in info}
    if "last_send_log_id" not in cols:
        conn.execute(text(
            "ALTER TABLE conversation_windows "
            "ADD COLUMN last_send_log_id TEXT DEFAULT ''"
        ))


@migration(25, "add source context to memories")
def _m025(conn, insp):
    info = conn.execute(text("PRAGMA table_info(memories)")).fetchall()
    if not info:
        return
    cols = {row[1] for row in info}
    for name in (
        "source_platform",
        "source_account_id",
        "source_chat_id",
        "source_msg_ids",
    ):
        if name not in cols:
            conn.execute(text(
                f"ALTER TABLE memories ADD COLUMN {name} TEXT"
            ))


@migration(26, "add transient affect columns to relationships")
def _m026(conn, insp):
    """Transient affect shares the relationships row with structural intimacy.

    Nullable on purpose: the rows already in a live database keep their
    meaning, and a missing value simply reads as "at baseline".
    """
    info = conn.execute(text("PRAGMA table_info(relationships)")).fetchall()
    if not info:
        return
    cols = {row[1] for row in info}
    for name, ddl in (
        ("warmth", "REAL"),
        ("warmth_reason", "TEXT"),
        ("warmth_updated_at", "DATETIME"),
    ):
        if name not in cols:
            conn.execute(text(
                f"ALTER TABLE relationships ADD COLUMN {name} {ddl}"
            ))


@migration(27, "add sender_name to conversations")
def _m027(conn, insp):
    """Who spoke, as a display name, so group transcripts stay attributable.

    Without it every other speaker's turn rendered as a bare "用户", which is
    how "who said this / who asked for that" got lost -- including in the
    rolling summary, where the loss is permanent.  Existing rows keep NULL and
    are backfilled separately; a NULL simply renders unattributed, exactly as
    before.
    """
    info = conn.execute(text("PRAGMA table_info(conversations)")).fetchall()
    if not info:
        return
    cols = {row[1] for row in info}
    if "sender_name" not in cols:
        conn.execute(text(
            "ALTER TABLE conversations ADD COLUMN sender_name TEXT"
        ))


@migration(28, "create message_media work table")
def _m028(conn, insp):
    """Track every media message until its file is actually resolvable.

    ``messages.media_path`` is resolved once at ingest and then frozen; when the
    file was not on disk yet at that instant the message could never show its
    picture to anyone (see ``MessageMedia`` for the measured timing).  This
    table keeps the clues so a retry pass can finish the job, and carries the
    status/attempts columns that make retrying safe to automate.

    ``CREATE TABLE IF NOT EXISTS`` so it is a no-op on databases where
    ``Base.metadata.create_all`` already made it.
    """
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS message_media (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER,
            msg_id INTEGER,
            platform TEXT DEFAULT 'wechat',
            account_id TEXT DEFAULT '',
            chat_id TEXT,
            sender_id TEXT,
            kind TEXT DEFAULT 'image',
            status TEXT DEFAULT 'pending',
            md5 TEXT,
            session_dir TEXT,
            quote_ref TEXT,
            source_path TEXT,
            local_path TEXT,
            description TEXT,
            attempts INTEGER DEFAULT 0,
            last_error TEXT,
            next_retry_at DATETIME,
            created_at DATETIME,
            resolved_at DATETIME
        )
    """))
    for name, cols in (
        ("ix_message_media_pending", "status, next_retry_at"),
        ("ix_message_media_message", "message_id"),
        ("ix_message_media_chat", "chat_id, created_at"),
        ("ix_message_media_md5", "md5"),
    ):
        conn.execute(text(
            f"CREATE INDEX IF NOT EXISTS {name} ON message_media({cols})"
        ))


@migration(29, "add media_path to conversations")
def _m029(conn, insp):
    """Remember which image a turn was about.

    A transcript row says "[图片]", which tells the model nothing and cannot be
    improved later because the row does not know which file it referred to.
    With the path stored, the renderer can substitute the description the image
    pipeline already caches by file -- including in the rolling summary.
    """
    info = conn.execute(text("PRAGMA table_info(conversations)")).fetchall()
    if not info:
        return
    cols = {row[1] for row in info}
    if "media_path" not in cols:
        conn.execute(text(
            "ALTER TABLE conversations ADD COLUMN media_path TEXT"
        ))


def run_migrations(db_path: str):
    engine = create_engine(f"sqlite:///{db_path}")
    insp = inspect(engine)

    with engine.begin() as conn:
        _ensure_version_table(conn)
        current = _get_applied_version(conn)

        pending = [m for m in _MIGRATIONS if m.version > current]
        _stamp_alembic_baseline(conn)
        if not pending:
            engine.dispose()
            return

        pending.sort(key=lambda m: m.version)
        for m in pending:
            if m.version in _ACCOUNT_SKIP_MIGRATION_VERSIONS:
                conn.execute(
                    text(
                        "INSERT OR IGNORE INTO _schema_version "
                        "(version, description) VALUES (:v, :d)"
                    ),
                    {"v": m.version, "d": f"{m.description} [meta-only]"},
                )
                log.info("migration_skipped_account_db", version=m.version)
                continue
            log.info("migration_start", version=m.version, desc=m.description)
            try:
                m.upgrade(conn, insp)
                conn.execute(text(
                    f"INSERT INTO _schema_version (version, description) VALUES ({m.version}, '{m.description}')"
                ))
                log.info("migration_done", version=m.version)
            except Exception as e:
                log.error("migration_failed", version=m.version, error=str(e))
                raise

    engine.dispose()



# The meta database has its own lifecycle: ``MetaBase.metadata.create_all``
# owns the table set, while a few incremental column migrations still have to
# be applied to existing files.  Replaying the account migration chain would
# create legacy/account-only tables in the shared meta database, so this entry
# point only runs the meta-relevant versions and then records a baseline so a
# later ``run_migrations`` call is a no-op for this file.
#
# 27 belongs here: the meta database owns the conversations rows a group turn
# reads back, and create_all never adds a column to a table that already
# exists.  Leaving it out is not a silent degradation -- the ORM selects the
# column, so every read raised "no such column: conversations.sender_name"
# through this entry point (a live bug caught by test_meta_migration).  29 is
# the same table and the same rule.  30 fixes the same class of mismatch for
# relationships.warmth: meta_repo uses db.models.Relationship, so the meta
# table must expose the transient affect columns too.
# 50 is NOT in this list on purpose: the meta-only chain must stay below the
# account chain's high-water mark.  It runs whatever it is handed and stamps it,
# so stamping 50 here would make the following ``run_migrations`` skip 41-49 for
# that file (46 owns terminal_events.external_msg_id and the dedup index, and 50
# reads that column).  bootstrap calls init_meta_db -> run_migrations, so 46
# always precedes 50.
@migration(30, "ensure relationship affect columns on meta tables")
def _m030(conn, insp):
    """Make meta relationships match the Relationship ORM used by meta_repo.

    ``MetaRepositoryManager.relationships`` is the shared, multi-account
    relationship repository, but the meta table originally omitted the
    transient affect columns that the account-side model expects.  Version 26
    added them to account databases; version 30 applies the same idempotent
    column check to the meta database and to any account DB that missed it.
    """
    info = conn.execute(text("PRAGMA table_info(relationships)")).fetchall()
    if not info:
        return
    cols = {row[1] for row in info}
    for name, ddl in (
        ("warmth", "REAL"),
        ("warmth_reason", "TEXT"),
        ("warmth_updated_at", "DATETIME"),
    ):
        if name not in cols:
            conn.execute(text(
                f"ALTER TABLE relationships ADD COLUMN {name} {ddl}"
            ))


@migration(31, "create group membership period epochs")
def _m031(conn, insp):
    """Add explicit leave/re-join epochs for group memberships.

    ``group_memberships`` is a current-state row (one per person/group) and has
    no ``left_at``.  This table keeps contiguous ``[joined_at, left_at)``
    epochs so historical command visibility is not silently widened after a
    person leaves and re-joins.  Existing rows are copied as open-ended epoch 1.
    """
    conn.execute(text(
        """
        CREATE TABLE IF NOT EXISTS group_membership_periods (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            person_id TEXT NOT NULL,
            platform TEXT NOT NULL,
            account_id TEXT DEFAULT '',
            chat_id TEXT NOT NULL,
            joined_at DATETIME,
            left_at DATETIME,
            epoch INTEGER DEFAULT 1,
            source TEXT DEFAULT '',
            created_at DATETIME,
            updated_at DATETIME,
            CONSTRAINT ux_group_membership_period_epoch
                UNIQUE (person_id, platform, account_id, chat_id, epoch)
        )
        """
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_gmp_person_group "
        "ON group_membership_periods (platform, account_id, chat_id, person_id)"
    ))
    try:
        tables = set(insp.get_table_names())
    except Exception:
        warn("db/migrations.py:866 Exception", err=repr(__import__("sys").exc_info()[1]))
        tables = set()
    if "group_memberships" in tables:
        conn.execute(text(
            """
            INSERT OR IGNORE INTO group_membership_periods
                (person_id, platform, account_id, chat_id, joined_at, left_at,
                 epoch, source, created_at, updated_at)
            SELECT person_id, platform, account_id, chat_id, first_seen_at,
                   NULL, 1, 'legacy_membership', first_seen_at, last_seen_at
            FROM group_memberships
            """
        ))


@migration(32, "create explicit account link edges")
def _m032(conn, insp):
    """Persist owner-drawn account links without touching message data.

    The edge is the only persisted fact; connected components and manual
    source-account tie-breaks stay deterministic graph computations.  Revoked
    rows are kept for audit, always append-only.
    """
    conn.execute(text(
        """
        CREATE TABLE IF NOT EXISTS account_link_edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform_a TEXT NOT NULL,
            account_a TEXT NOT NULL,
            platform_b TEXT NOT NULL,
            account_b TEXT NOT NULL,
            created_by TEXT DEFAULT '',
            created_at DATETIME,
            revoked_at DATETIME,
            revision INTEGER DEFAULT 1,
            note TEXT DEFAULT ''
        )
        """
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_account_link_active "
        "ON account_link_edges (platform_a, account_a, platform_b, account_b, revoked_at)"
    ))


@migration(33, "create append-only terminal event stream")
def _m033(conn, insp):
    """Create the formal Phase 2 event stream table.

    ``create_all`` owns the table for fresh installs; this migration upgrades
    existing meta databases without touching any existing table.  Rows are
    append-only.  ``event_id`` is the idempotency key for JSONL/backfill
    importers.
    """
    conn.execute(text(
        """
        CREATE TABLE IF NOT EXISTS terminal_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL,
            occurred_at DATETIME,
            sequence INTEGER DEFAULT 0,
            platform TEXT DEFAULT '',
            bot_account_id TEXT DEFAULT '',
            chat_id TEXT DEFAULT '',
            chat_type TEXT DEFAULT '',
            actor_person_id TEXT DEFAULT '',
            actor_account_id TEXT DEFAULT '',
            recipient_person_ids_json TEXT DEFAULT '[]',
            event_type TEXT DEFAULT 'message',
            content TEXT,
            action_payload_json TEXT DEFAULT '{}',
            command_id TEXT DEFAULT '',
            authorization_id TEXT DEFAULT '',
            parent_event_id TEXT DEFAULT '',
            link_revision INTEGER DEFAULT 0,
            visibility_scope_json TEXT DEFAULT '["public"]',
            sensitivity TEXT DEFAULT 'normal',
            metadata_json TEXT DEFAULT '{}',
            created_at DATETIME,
            CONSTRAINT ux_terminal_event_id UNIQUE (event_id)
        )
        """
    ))
    for index_sql in (
        "CREATE INDEX IF NOT EXISTS ix_terminal_events_time "
        "ON terminal_events (occurred_at, sequence)",
        "CREATE INDEX IF NOT EXISTS ix_terminal_events_actor "
        "ON terminal_events (platform, bot_account_id, actor_person_id, occurred_at)",
        "CREATE INDEX IF NOT EXISTS ix_terminal_events_chat "
        "ON terminal_events (platform, bot_account_id, chat_id, occurred_at)",
        "CREATE INDEX IF NOT EXISTS ix_terminal_events_type "
        "ON terminal_events (event_type, occurred_at)",
        "CREATE INDEX IF NOT EXISTS ix_terminal_events_command "
        "ON terminal_events (command_id)",
    ):
        conn.execute(text(index_sql))


@migration(34, "create command service audit tables")
def _m034(conn, insp):
    """Persist commands, owner authorization evidence and query logs.

    These are projections/audit tables fed by CommandService; ``terminal_events``
    remains the append-only fact stream.  Status changes update the projection
    but never delete history.
    """
    conn.execute(text(
        """
        CREATE TABLE IF NOT EXISTS command_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            command_id TEXT NOT NULL,
            created_at DATETIME,
            issuer_person_id TEXT DEFAULT '',
            issuer_account_id TEXT DEFAULT '',
            issuer_role TEXT DEFAULT 'ordinary',
            origin_platform TEXT DEFAULT '',
            origin_bot_account_id TEXT DEFAULT '',
            origin_chat_id TEXT DEFAULT '',
            origin_chat_type TEXT DEFAULT 'private',
            raw_request TEXT,
            clarified_spec_json TEXT DEFAULT '{}',
            status TEXT DEFAULT 'received',
            identity_mode TEXT DEFAULT 'as_yinyue',
            nominal_name TEXT DEFAULT '',
            target_person_ids_json TEXT DEFAULT '[]',
            action_type TEXT DEFAULT '',
            execution_ids_json TEXT DEFAULT '[]',
            group_queryable_after DATETIME,
            public_render_policy TEXT DEFAULT 'as_yinyue',
            authorization_id TEXT DEFAULT '',
            revoked_at DATETIME,
            metadata_json TEXT DEFAULT '{}',
            updated_at DATETIME,
            CONSTRAINT ux_command_record_id UNIQUE (command_id)
        )
        """
    ))
    conn.execute(text(
        """
        CREATE TABLE IF NOT EXISTS authorization_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            authorization_id TEXT NOT NULL,
            command_id TEXT NOT NULL,
            raw_owner_conversation_json TEXT DEFAULT '[]',
            clarification_events_json TEXT DEFAULT '[]',
            final_confirmation TEXT,
            owner_only INTEGER DEFAULT 1,
            created_at DATETIME,
            CONSTRAINT ux_authorization_record_id UNIQUE (authorization_id)
        )
        """
    ))
    conn.execute(text(
        """
        CREATE TABLE IF NOT EXISTS command_query_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query_id TEXT NOT NULL,
            requester_person_id TEXT DEFAULT '',
            requester_account_id TEXT DEFAULT '',
            queried_at DATETIME,
            command_ids_json TEXT DEFAULT '[]',
            answer TEXT,
            context_json TEXT DEFAULT '{}',
            CONSTRAINT ux_command_query_log_id UNIQUE (query_id)
        )
        """
    ))
    for index_sql in (
        "CREATE INDEX IF NOT EXISTS ix_command_records_created "
        "ON command_records (created_at)",
        "CREATE INDEX IF NOT EXISTS ix_command_records_issuer "
        "ON command_records (issuer_person_id, created_at)",
        "CREATE INDEX IF NOT EXISTS ix_command_records_origin "
        "ON command_records (origin_platform, origin_bot_account_id, origin_chat_id, created_at)",
        "CREATE INDEX IF NOT EXISTS ix_authorization_records_command "
        "ON authorization_records (command_id)",
        "CREATE INDEX IF NOT EXISTS ix_command_query_logs_requester "
        "ON command_query_logs (requester_person_id, queried_at)",
    ):
        conn.execute(text(index_sql))


@migration(35, "create memory projections and projection cursor")
def _m035(conn, insp):
    """Create the extracted memory projection layer.

    ``memory_projections`` is fed from ``terminal_events`` by MemoryProjector;
    ``memory_projection_cursors`` makes the extraction idempotent/incremental.
    Recall reads projections first and falls back to the raw event stream.
    """
    conn.execute(text(
        """
        CREATE TABLE IF NOT EXISTS memory_projections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id TEXT NOT NULL,
            memory_type TEXT DEFAULT 'episode',
            content TEXT,
            platform TEXT DEFAULT '',
            bot_account_id TEXT DEFAULT '',
            subject_person_ids_json TEXT DEFAULT '[]',
            source_event_ids_json TEXT DEFAULT '[]',
            event_sequence INTEGER DEFAULT 0,
            valid_from DATETIME,
            valid_to DATETIME,
            confidence REAL DEFAULT 0.5,
            visibility_scope_json TEXT DEFAULT '["public"]',
            sensitivity TEXT DEFAULT 'normal',
            linked_component_id TEXT DEFAULT '',
            link_revision INTEGER DEFAULT 0,
            metadata_json TEXT DEFAULT '{}',
            created_at DATETIME,
            updated_at DATETIME,
            CONSTRAINT ux_memory_projection_id UNIQUE (memory_id)
        )
        """
    ))
    conn.execute(text(
        """
        CREATE TABLE IF NOT EXISTS memory_projection_cursors (
            key TEXT PRIMARY KEY,
            last_sequence INTEGER DEFAULT 0,
            updated_at DATETIME
        )
        """
    ))
    for index_sql in (
        "CREATE INDEX IF NOT EXISTS ix_memory_projections_tenant "
        "ON memory_projections (platform, bot_account_id, event_sequence)",
        "CREATE INDEX IF NOT EXISTS ix_memory_projections_type "
        "ON memory_projections (memory_type, event_sequence)",
        "CREATE INDEX IF NOT EXISTS ix_memory_projections_valid "
        "ON memory_projections (valid_from, valid_to)",
        "CREATE INDEX IF NOT EXISTS ix_memory_projections_component "
        "ON memory_projections (linked_component_id, event_sequence)",
    ):
        conn.execute(text(index_sql))


@migration(36, "create account link revision state")
def _m036(conn, insp):
    """Singleton revision for account_link_edges mutations."""
    conn.execute(text(
        """
        CREATE TABLE IF NOT EXISTS account_link_state (
            key TEXT PRIMARY KEY,
            revision INTEGER DEFAULT 0,
            updated_at DATETIME
        )
        """
    ))
    conn.execute(text(
        "INSERT OR IGNORE INTO account_link_state (key, revision, updated_at) "
        "VALUES ('terminal_links', 0, CURRENT_TIMESTAMP)"
    ))


@migration(37, "create proactive target whitelist and send log")
def _m037(conn, insp):
    """Owner-managed proactive targets + append-only send log."""
    conn.execute(text(
        """
        CREATE TABLE IF NOT EXISTS proactive_targets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL,
            bot_account_id TEXT DEFAULT '',
            chat_id TEXT DEFAULT '',
            chat_type TEXT DEFAULT 'private',
            enabled INTEGER DEFAULT 1,
            allow_sticker INTEGER DEFAULT 1,
            max_per_hour INTEGER DEFAULT 6,
            max_per_day INTEGER DEFAULT 20,
            cooldown_minutes INTEGER DEFAULT 60,
            quiet_hours_json TEXT DEFAULT '[]',
            note TEXT DEFAULT '',
            created_at DATETIME,
            updated_at DATETIME,
            CONSTRAINT ux_proactive_target_scope
                UNIQUE (platform, bot_account_id, chat_id)
        )
        """
    ))
    conn.execute(text(
        """
        CREATE TABLE IF NOT EXISTS proactive_send_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT DEFAULT '',
            bot_account_id TEXT DEFAULT '',
            chat_id TEXT DEFAULT '',
            sent_at DATETIME,
            kind TEXT DEFAULT 'text',
            trigger TEXT DEFAULT 'proactive',
            content_preview TEXT,
            ok INTEGER DEFAULT 1,
            error TEXT
        )
        """
    ))
    for index_sql in (
        "CREATE INDEX IF NOT EXISTS ix_proactive_targets_scope "
        "ON proactive_targets (platform, bot_account_id, chat_id, enabled)",
        "CREATE INDEX IF NOT EXISTS ix_proactive_send_time "
        "ON proactive_send_log (platform, bot_account_id, chat_id, sent_at)",
    ):
        conn.execute(text(index_sql))


@migration(38, "create event-sourced analysis projections and cursor")
def _m038(conn, insp):
    """Track B: analysis results move onto ``terminal_events`` provenance.

    ``analysis_projections`` is intentionally separate from
    ``memory_projections`` so a daily digest or group summary can never be
    recalled as a normal memory.  The legacy account-level ``analysis`` table
    remains untouched until the B cutover is stable.
    """
    # Meta-only table: the account migration chain runs on account DBs too,
    # but Track B projections are global (they cite terminal event ids).
    if "terminal_events" not in set(insp.get_table_names()):
        return

    conn.execute(text(
        """
        CREATE TABLE IF NOT EXISTS analysis_projections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            projection_id TEXT NOT NULL,
            projection_type TEXT DEFAULT '',
            content TEXT,
            summary TEXT,
            platform TEXT DEFAULT '',
            bot_account_id TEXT DEFAULT '',
            chat_id TEXT DEFAULT '',
            chat_type TEXT DEFAULT '',
            source_event_ids_json TEXT DEFAULT '[]',
            visibility_scope_json TEXT DEFAULT '["owner_only"]',
            sensitivity TEXT DEFAULT 'normal',
            event_sequence INTEGER DEFAULT 0,
            confidence REAL DEFAULT 0.5,
            occurred_at DATETIME,
            metadata_json TEXT DEFAULT '{}',
            created_at DATETIME,
            updated_at DATETIME,
            CONSTRAINT ux_analysis_projection_id UNIQUE (projection_id)
        )
        """
    ))
    conn.execute(text(
        """
        CREATE TABLE IF NOT EXISTS analysis_projection_cursors (
            key TEXT PRIMARY KEY,
            last_sequence INTEGER DEFAULT 0,
            updated_at DATETIME
        )
        """
    ))
    for index_sql in (
        "CREATE INDEX IF NOT EXISTS ix_analysis_projections_tenant "
        "ON analysis_projections (platform, bot_account_id, event_sequence)",
        "CREATE INDEX IF NOT EXISTS ix_analysis_projections_type "
        "ON analysis_projections (projection_type, occurred_at)",
        "CREATE INDEX IF NOT EXISTS ix_analysis_projections_chat "
        "ON analysis_projections (chat_id, occurred_at)",
    ):
        conn.execute(text(index_sql))


@migration(39, "create feedback samples for Layer-2 quality signals")
def _m039(conn, insp):
    """Real-usage quality signals (corrections / repeated questions).

    Meta-only: feedback is global and references terminal event ids.  The table
    is a *collection* store for eval; it never affects recall.
    """
    if "terminal_events" not in set(insp.get_table_names()):
        return
    conn.execute(text(
        """
        CREATE TABLE IF NOT EXISTS feedback_samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            platform TEXT DEFAULT '',
            bot_account_id TEXT DEFAULT '',
            chat_id TEXT DEFAULT '',
            chat_type TEXT DEFAULT '',
            person_id TEXT DEFAULT '',
            sample_text TEXT DEFAULT '',
            previous_user_text TEXT DEFAULT '',
            reply_event_id TEXT DEFAULT '',
            reply_preview TEXT DEFAULT '',
            status TEXT DEFAULT 'new',
            metadata_json TEXT DEFAULT '{}',
            created_at DATETIME
        )
        """
    ))
    for index_sql in (
        "CREATE INDEX IF NOT EXISTS ix_feedback_samples_created "
        "ON feedback_samples (created_at)",
        "CREATE INDEX IF NOT EXISTS ix_feedback_samples_status "
        "ON feedback_samples (status, created_at)",
        "CREATE INDEX IF NOT EXISTS ix_feedback_samples_chat "
        "ON feedback_samples (chat_id, created_at)",
    ):
        conn.execute(text(index_sql))


@migration(62, "utterance_edges: 「谁对谁说」寻址边（新表 + 唯一键 + 反向索引）")
def _m062(conn, insp):
    """新建 ``utterance_edges``：一条入站消息的「对谁说」事实（task-43）。

    为什么是**新表**而不是给 ``terminal_events`` 加列（task-42 三组数定的）：
    边天然稀疏（主群 7 天 572/4749 = 12.04%，全账号引用率 11.3%），加列就得用
    NULL 表达"没有边"，而"没有边"和"边界没写进来"是三态里的两个不同意思；
    单独一张表让「没有边 = 没有行」成立，回滚是 ``DROP TABLE`` 一行。

    守卫与 56–61 同路（见 ``_m061`` docstring 的两个坑）：

    1. 62 **不进** ``_META_MIGRATION_VERSIONS``（上限 39）——否则
       ``run_meta_migrations`` 盖 62 后账号链会把 40–61 在 META 上永久跳过；
    2. 62 **不进** ``_ACCOUNT_SKIP_MIGRATION_VERSIONS``——进了就是账号链只盖章
       不执行，meta 链又跑不到 → 两个库都没有这张表。

    由账号链 ``run_migrations`` 执行；只在**应用库**（还有 ``terminal_events``
    的库）里做（纯账号库没有这张表也无害，但没有事件流就没有边可 join）；
    **建表与索引都 ``IF NOT EXISTS``**（新库由 ``MetaBase.metadata.create_all``
    先建好，老库由这里补，重复执行幂等）。

    列顺序/默认值必须与 ``db.meta_models.UtteranceEdge`` 一致（parity 用例锁住）。

    回滚：``DROP INDEX`` ×3 + ``DROP TABLE utterance_edges``；这张表是**纯派生
    事实**，删掉只丢"谁回了谁"的渲染能力，不影响任何已有取值，可由回填脚本
    从账号库 messages 重建（``scripts/backfill_utterance_edges.py``）。
    """
    if conn.execute(text(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='terminal_events'"
    )).fetchone() is None:
        return
    conn.execute(text(
        "CREATE TABLE IF NOT EXISTS utterance_edges ("
        "id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,"
        "platform TEXT DEFAULT '' NOT NULL,"
        "bot_account_id TEXT DEFAULT '' NOT NULL,"
        "chat_id TEXT DEFAULT '' NOT NULL,"
        "chat_type TEXT DEFAULT '' NOT NULL,"
        "msg_id TEXT DEFAULT '' NOT NULL,"
        "event_id TEXT DEFAULT '' NOT NULL,"
        "turn_id TEXT DEFAULT '' NOT NULL,"
        "speaker_id TEXT DEFAULT '' NOT NULL,"
        "speaker_name TEXT DEFAULT '' NOT NULL,"
        "recipient_kind TEXT DEFAULT 'unknown' NOT NULL,"
        "recipient_id TEXT DEFAULT '' NOT NULL,"
        "recipient_name TEXT DEFAULT '' NOT NULL,"
        "quoted_msg_id TEXT DEFAULT '' NOT NULL,"
        "quoted_speaker_id TEXT DEFAULT '' NOT NULL,"
        "quoted_speaker_name TEXT DEFAULT '' NOT NULL,"
        "evidence TEXT DEFAULT 'none' NOT NULL,"
        "evidence_detail_json TEXT DEFAULT '{}' NOT NULL,"
        "created_at DATETIME"
        ")"
    ))
    for ddl in (
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_utterance_edge_msg "
        "ON utterance_edges (platform, chat_id, msg_id)",
        "CREATE INDEX IF NOT EXISTS ix_utterance_edges_platform "
        "ON utterance_edges (platform)",
        "CREATE INDEX IF NOT EXISTS ix_utterance_edges_chat_id "
        "ON utterance_edges (chat_id)",
        "CREATE INDEX IF NOT EXISTS ix_utterance_edges_quoted "
        "ON utterance_edges (quoted_msg_id)",
    ):
        conn.execute(text(ddl))

@migration(63, "turn_trace: E0 判决行留痕（每回合一行，append-only 旁注）")
def _m063(conn, insp):
    """新建 ``turn_trace``：每回合一行判决留痕（U-01）。

    守卫与 56–62 同路：
    1. 63 **不进** ``_META_MIGRATION_VERSIONS``（上限 39）；
    2. 63 **不进** ``_ACCOUNT_SKIP_MIGRATION_VERSIONS``——进了就是只盖章不执行。

    由账号链 ``run_migrations`` 执行；只在**应用库**（还有 ``terminal_events`` 的库）
    里做（纯账号库没有事件流，也就没有判决可留）。建表与索引都 ``IF NOT EXISTS``：
    新库由 ``MetaBase.metadata.create_all`` 先建好，老库由这里补，重复执行幂等。

    列顺序/默认值必须与 ``db.meta_models.TurnTrace`` 一致（parity 用例锁住）。
    回滚：``DROP INDEX ix_turn_trace_chat_ts`` + ``DROP TABLE turn_trace``；
    这张表是**旁注**，删掉只丢读数，不动任何既有取值。
    """
    if conn.execute(text(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='terminal_events'"
    )).fetchone() is None:
        return
    from core.terminal.turn_trace import CREATE_INDEX_SQL, CREATE_TABLE_SQL

    conn.execute(text(CREATE_TABLE_SQL))
    conn.execute(text(CREATE_INDEX_SQL))


@migration(64, "jobs: U-10「在手的事」台账（job_events + jobs 投影 + job_approvals）")
def _m064(conn, insp):
    """新建 ``job_events`` / ``jobs`` / ``job_approvals``：一件事的台账（U-10）。

    守卫与 56–63 同路：
    1. 64 **不进** ``_META_MIGRATION_VERSIONS``（上限 39）；
    2. 64 **不进** ``_ACCOUNT_SKIP_MIGRATION_VERSIONS``——进了就是只盖章不执行。

    由账号链 ``run_migrations`` 执行；只在**应用库**（还有 ``terminal_events`` 的库）
    里做（纯账号库没有判，也就没有「在手的事」）。三条 DDL 都 ``IF NOT EXISTS``：
    新库由 ``MetaBase.metadata.create_all`` 先建好，老库由这里补，重复执行幂等。

    台账纪律：``job_events`` 是唯一事实源（append-only，主键 ``(job_id, seq)``），
    ``jobs`` 只是当前状态投影（``core/jobs/store.py::JobStore.rebuild()`` 可整表重放），
    ``job_approvals`` 主键 ``(job_id, step_n, action_hash)`` 保证同一动作批两次幂等。

    列顺序/默认值必须与 ``core/jobs/store.py`` 的 DDL 常量一致（同源导入，parity 由
    ``tests/test_job_store.py`` 锁住）。开关 ``JOB_LEDGER`` 默认 off：迁移只建表，
    不写一行，不改任何既有行为。

    回滚：``DROP TABLE job_approvals`` + ``DROP TABLE jobs`` + ``DROP TABLE job_events``；
    这张表是「进行中」的唯一记录，回滚等于放弃所有在手的事，不动账本。
    """
    if conn.execute(text(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='terminal_events'"
    )).fetchone() is None:
        return
    from core.jobs.store import ddl_statements

    for ddl in ddl_statements():
        conn.execute(text(ddl))


_META_MIGRATION_VERSIONS = (23, 24, 27, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39)
_META_BASELINE_VERSION = 24
# Migrations whose tables are physically owned by the shared meta DB.  The
# account chain still records the version so it does not retry forever, but it
# must not materialise the tables in account DBs.
_ACCOUNT_SKIP_MIGRATION_VERSIONS = frozenset({38, 39})


def run_meta_migrations(db_path: str) -> None:
    engine = create_engine(f"sqlite:///{db_path}")
    insp = inspect(engine)
    with engine.begin() as conn:
        _ensure_version_table(conn)
        _stamp_alembic_baseline(conn)
        current = _get_applied_version(conn)
        pending = sorted(
            (
                m for m in _MIGRATIONS
                if m.version in _META_MIGRATION_VERSIONS and m.version > current
            ),
            key=lambda m: m.version,
        )
        for m in pending:
            log.info("meta_migration_start", version=m.version, desc=m.description)
            m.upgrade(conn, insp)
            conn.execute(
                text(
                    "INSERT OR IGNORE INTO _schema_version (version, description) "
                    "VALUES (:v, :d)"
                ),
                {"v": m.version, "d": m.description},
            )
            log.info("meta_migration_done", version=m.version)
        conn.execute(
            text(
                "INSERT OR IGNORE INTO _schema_version (version, description) "
                "VALUES (:v, 'meta schema baseline (create_all)')"
            ),
            {"v": _META_BASELINE_VERSION},
        )
    # 来源扩展不推进稀疏 meta 版本水位，避免应用链 40..67 被跳过。
    from db.thread_scope import upgrade_thread_sources, upgrade_thread_summaries
    with engine.begin() as conn:
        upgrade_thread_sources(conn)
        upgrade_thread_summaries(conn)
    engine.dispose()


@migration(66, "utterance_edges 唯一键补账号：多账号同群同消息不再撞键")
def _m066(conn, insp) -> None:
    """重建 ``ux_utterance_edge_msg``：``(platform, chat_id, msg_id)`` → 四元组。

    为什么必须补 ``bot_account_id``：同一条消息在同一个群里被两个账号各收到一次时，
    ``msg_id`` 相同而账号不同；旧键会把两行折成一行（静默丢数据，且丢的是"谁看到的"）。
    新键更宽，**不会**让既有数据违规（旧键能通过的组合新键必然能通过），
    所以这是一次可直接执行的索引替换，不需要数据清洗。

    不进 ``_META_MIGRATION_VERSIONS``：表只在应用库；不进 ``_ACCOUNT_SKIP_MIGRATION_VERSIONS``。
    列顺序与 ``db/meta_models.py::UtteranceEdge.__table_args__`` 保持一致
    （``tests/test_utterance_edges.py`` 的 parity 用例锁住）。

    回滚：``DROP INDEX ux_utterance_edge_msg`` 后按旧三元组重建——但若已写入
    "同群同消息、不同账号"两行，旧键会当场失败，回滚前必须先删掉其中一行。
    """
    if conn.execute(
        text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='utterance_edges'")
    ).fetchone() is None:
        return
    conn.execute(text("DROP INDEX IF EXISTS ux_utterance_edge_msg"))
    conn.execute(
        text(
            "CREATE UNIQUE INDEX ux_utterance_edge_msg "
            "ON utterance_edges (platform, bot_account_id, chat_id, msg_id)"
        )
    )


@migration(67, "jobs: 验收判据 criteria（缺省空串 = 没写）")
def _m067(conn, insp) -> None:
    """给 ``jobs`` 投影补 ``criteria``：写清"怎么算做到了"。

    为什么投影也要这一列：`goal` 是"要什么"，`criteria` 是"以什么为验收"；
    收口时（`state=done`）需要拿它和回执对账，而回执独立落在 `delivery_receipts`，
    两边只能靠 job 行上的这段文本对齐。

    只在应用库执行（有 ``jobs`` 表才是）；列已存在则跳过（幂等）。
    不进 ``_META_MIGRATION_VERSIONS``（meta 库没有这张表）。
    回滚：``ALTER TABLE jobs DROP COLUMN criteria``（SQLite ≥3.35），丢的只是判据文本。
    """
    if conn.execute(
        text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'")
    ).fetchone() is None:
        return
    cols = {str(c[1]) for c in conn.execute(text("PRAGMA table_info(jobs)")).fetchall()}
    if "criteria" in cols:
        return
    conn.execute(text("ALTER TABLE jobs ADD COLUMN criteria TEXT DEFAULT '' NOT NULL"))


@migration(65, "出口回执旁注表 delivery_receipts（U-11，append-only）")
def _m065(conn, insp):
    """建 `delivery_receipts`（出口回执旁注：六种状态各一行，append-only）。

    只在应用库执行（有 `terminal_events` 才是应用库）；DDL 唯一来源是
    `core/terminal/delivery_receipts.py`，本函数不复制字段。
    不进 `_META_MIGRATION_VERSIONS`（meta 库没有这张表），不进 `_ACCOUNT_SKIP_MIGRATION_VERSIONS`
    （账号库也要建）。列顺序与 `CREATE_TABLE_SQL` 一致，无 ORM 映射。
    回滚：`DROP TABLE delivery_receipts;` ——整表可重建（只丢这几天的投递读数）。
    """
    if conn.execute(text(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='terminal_events'"
    )).fetchone() is None:
        return
    from core.terminal.delivery_receipts import ddl_statements

    for sql in ddl_statements():
        conn.execute(text(sql))


@migration(40, "person notes: one-line per-person notes for the prompt")
def _m040(conn, insp):
    """P1: 人物备注（[在场人·备注] 块的数据源）。

    Meta-only。一行一人一句话（是谁 / 什么脾气 / 该怎么对待），
    来源可追溯（owner 手写 / auto 沉淀 / correction 纠错），可停用可回滚。
    """
    # 用 conn 直接查 sqlite_master，避免 inspector 缓存导致"从零迁移"时误判
    # terminal_events 不存在而跳过建表（insp 缓存的是迁移开始时的表列表）。
    if conn.execute(
        text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='terminal_events'")
    ).fetchone() is None:
        return
    conn.execute(text(
        """
        CREATE TABLE IF NOT EXISTS person_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL DEFAULT '',
            account_id TEXT NOT NULL DEFAULT '',
            person_key TEXT NOT NULL,
            chat_id TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'owner',
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT ''
        )
        """
    ))
    conn.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_person_notes_key "
        "ON person_notes(platform, account_id, person_key, chat_id)"
    ))


@migration(41, "memory proposals: staged notes awaiting owner approval")
def _m041(conn, insp):
    """P2: 沉淀提案（docs/记忆的定义.md §3）。

    只提案不生效：夜里从事件里抽候选 → 主人 approve/reject → 批准才写
    person_notes。每次留痕（槽位/对象/证据/理由），可查可撤。
    """
    if "terminal_events" not in set(insp.get_table_names()):
        return
    conn.execute(text(
        """
        CREATE TABLE IF NOT EXISTS memory_proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slot TEXT NOT NULL,
            subject_name TEXT NOT NULL DEFAULT '',
            subject_key TEXT NOT NULL DEFAULT '',
            chat_id TEXT NOT NULL DEFAULT '',
            platform TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            evidence_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL DEFAULT '',
            decided_at TEXT NOT NULL DEFAULT '',
            decided_by TEXT NOT NULL DEFAULT ''
        )
        """
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_memory_proposals_status "
        "ON memory_proposals(status, chat_id)"
    ))



@migration(42, "stance cards: versioned plain-language boundaries for stance decision")
def _m042(conn, insp):
    """P2 社交分寸：立场卡（[局况] 姿态行的数据源）。

    Meta-only。白话一段（分群 scope='group' / 分人 scope='person'），
    append-only 版本化：每次写入产生新版本、旧版置 superseded，可回滚
    （回滚=把旧内容复活为新版本）。主人手编最高权威（source='owner'）。
    """
    if conn.execute(
        text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='terminal_events'")
    ).fetchone() is None:
        return
    conn.execute(text(
        """
        CREATE TABLE IF NOT EXISTS stance_cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope TEXT NOT NULL DEFAULT 'group',
            platform TEXT NOT NULL DEFAULT '',
            account_id TEXT NOT NULL DEFAULT '',
            chat_id TEXT NOT NULL DEFAULT '',
            person_key TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'owner',
            version INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT ''
        )
        """
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS idx_stance_cards_lookup "
        "ON stance_cards(status, chat_id)"
    ))


@migration(43, "reminder delivery address and attempt counter")
def _m043(conn, insp):
    """提醒的送达地址与重试止损线（2026-09-28，承诺与能力对齐）。

    在此之前 reminders 只有"提醒什么/几点"，没有"送到哪个会话"：
    create_reminder 能落库、能回话承诺，但调度器没有任何目标可发，
    到点必然静默。这里补上送达三元组（platform/account/chat）
    与会话类型；attempts 用于投递失败时重试到止损线后停手，
    避免坏地址被每分钟扫到一次、无限重试。

    用 PRAGMA 而不是 inspector：老库上 inspector 可能缓存了建表前的
    列快照（同 _m025 的处理）。
    """
    info = conn.execute(text("PRAGMA table_info(reminders)")).fetchall()
    if not info:
        return
    cols = {row[1] for row in info}
    for name in ("source_platform", "source_account_id",
                 "source_chat_id", "source_chat_type"):
        if name not in cols:
            conn.execute(text(f"ALTER TABLE reminders ADD COLUMN {name} TEXT"))
    if "attempts" not in cols:
        conn.execute(text(
            "ALTER TABLE reminders ADD COLUMN attempts INTEGER DEFAULT 0"
        ))


@migration(44, "reminder requester identity and group mention target")
def _m044(conn, insp):
    """非主人提醒的授权/审计字段 + 群提醒创建时定下的 @ 对象。

    非主人委托的门槛（白名单会话 + 只接一次性 + 数量上限）需要知道
    是谁在要；群提醒的 @ 对象必须在创建时定死，投递端不猜。
    """
    info = conn.execute(text("PRAGMA table_info(reminders)")).fetchall()
    if not info:
        return
    cols = {row[1] for row in info}
    for name in ("requester_id", "requester_name", "mention_name"):
        if name not in cols:
            conn.execute(text(f"ALTER TABLE reminders ADD COLUMN {name} TEXT"))
    if "requester_is_owner" not in cols:
        conn.execute(text(
            "ALTER TABLE reminders ADD COLUMN requester_is_owner INTEGER DEFAULT 0"
        ))


@migration(45, "reminder purpose and multi-mention")
def _m045(conn, insp):
    """任务类型（remind/act）+ 多选 @（JSON 数组）。

    act 字段先留、执行器后接；mention_names 是新写入列，44 版的单值
    mention_name 保留只读兼容，投递端优先读新列、空则回退旧列。
    """
    info = conn.execute(text("PRAGMA table_info(reminders)")).fetchall()
    if not info:
        return
    cols = {row[1] for row in info}
    if "mention_names" not in cols:
        conn.execute(text("ALTER TABLE reminders ADD COLUMN mention_names TEXT"))
    if "purpose" not in cols:
        conn.execute(text(
            "ALTER TABLE reminders ADD COLUMN purpose TEXT DEFAULT 'remind'"
        ))


@migration(46, "terminal_events external message id, backfill and dedup index")
def _m046(conn, insp):
    """消息事件的外部 msg_id 实列 + 唯一索引 + 历史回填。

    背景（2026-09-28 A1 审计）：消息事件与 account.db 对齐只能解析
    ``metadata_json.msg_id``；跨库重放 / 对账 / 去重都缺一个实列键。
    这里补 ``external_msg_id`` 实列（幂等键 platform + bot_account_id +
    chat_id + external_msg_id），并把历史行从 JSON 回填。

    唯一索引是**非空 external_msg_id** 的偏索引：回复 / 思考 / 动作等
    事件不带外部消息 id，不受影响。若历史脏数据存在重复键，降级为
    普通索引并留错误日志，不阻塞启动（对账器会再暴露）。

    用 conn 直查 sqlite_master / PRAGMA，避免 inspector 缓存误判
    （同 _m040 的教训）。
    """
    if conn.execute(text(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='terminal_events'"
    )).fetchone() is None:
        return
    info = conn.execute(text("PRAGMA table_info(terminal_events)")).fetchall()
    cols = {row[1] for row in info}
    if "external_msg_id" not in cols:
        conn.execute(text(
            "ALTER TABLE terminal_events ADD COLUMN external_msg_id TEXT DEFAULT ''"
        ))
    conn.execute(text(
        """
        UPDATE terminal_events
           SET external_msg_id = json_extract(metadata_json, '$.msg_id')
         WHERE (external_msg_id IS NULL OR external_msg_id = '')
           AND event_type IN ('message', 'question', 'request', 'command')
           AND json_extract(metadata_json, '$.msg_id') IS NOT NULL
           AND json_extract(metadata_json, '$.msg_id') != ''
        """
    ))
    dupes = conn.execute(text(
        """
        SELECT platform, bot_account_id, chat_id, external_msg_id, COUNT(*) AS c
          FROM terminal_events
         WHERE external_msg_id IS NOT NULL AND external_msg_id != ''
         GROUP BY 1, 2, 3, 4
        HAVING c > 1
         LIMIT 5
        """
    )).fetchall()
    if dupes:
        log.error(
            "migration_46_duplicate_external_msg",
            sample=[tuple(str(v) for v in row) for row in dupes],
        )
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_terminal_events_external_msg "
            "ON terminal_events (platform, bot_account_id, chat_id, external_msg_id)"
        ))
        return
    conn.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_terminal_events_external_msg "
        "ON terminal_events (platform, bot_account_id, chat_id, external_msg_id) "
        "WHERE external_msg_id IS NOT NULL AND external_msg_id != ''"
    ))


@migration(47, "account event outbox for terminal delivery")
def _m047(conn, insp):
    """account.db 的事件 outbox：消息落库与 terminal_events 投递同事务。

    事件写入此前是 best-effort「先落消息、后写事件」，失败静默且
    ``messages.exists`` 命中不再重试 → 洞。outbox 让失败留在 pending，
    由 forwarder 退避重试。account 库独有：以 ``messages`` 表存在为守卫，
    meta 库直接跳过；用 conn 直查 sqlite_master，避免 inspector 缓存。
    """
    if conn.execute(text(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages'"
    )).fetchone() is None:
        return
    conn.execute(text(
        """
        CREATE TABLE IF NOT EXISTS event_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL DEFAULT '',
            account_id TEXT NOT NULL DEFAULT '',
            chat_id TEXT NOT NULL DEFAULT '',
            msg_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at DATETIME,
            last_error TEXT NOT NULL DEFAULT '',
            created_at DATETIME,
            delivered_at DATETIME
        )
        """
    ))
    conn.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_event_outbox_msg "
        "ON event_outbox (platform, account_id, chat_id, msg_id)"
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_event_outbox_pending "
        "ON event_outbox (status, next_attempt_at)"
    ))


@migration(49, "retire legacy contact_profiles and memories tables")
def _m049(conn, insp):
    """退役两张空壳表（P3 收口）：contact_profiles / memories。

    读方已先行移除：新 person-profile 工具替代旧 AnalyzeContactProfileTool，
    记忆主读早已迁 memory_projections。两表线上均 0 行。
    注意：friction_events 不在退役清单——它是纠错反馈链的活表（2026-09-29 更正）。
    """
    conn.execute(text("DROP TABLE IF EXISTS contact_profiles"))
    conn.execute(text("DROP TABLE IF EXISTS memories"))


@migration(48, "person notes provenance (proposal source)")
def _m048(conn, insp):
    """person_notes 溯源：记录它来自哪条提案 + 提案证据。

    P2 收口（docs/人物线收口_盘点.md）：批准写入备注时把 memory_proposals 的
    id / evidence_json 落下来；本迁移加列并尽力回填（按 subject/chat + 内容包含
    匹配已批准提案）。account 库没有 person_notes → 直接跳过。
    """
    if conn.execute(text(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='person_notes'"
    )).fetchone() is None:
        return
    cols = {
        row[1]
        for row in conn.execute(text("PRAGMA table_info(person_notes)")).fetchall()
    }
    if "source_proposal_id" not in cols:
        conn.execute(text(
            "ALTER TABLE person_notes ADD COLUMN source_proposal_id INTEGER DEFAULT 0"
        ))
    if "evidence_json" not in cols:
        conn.execute(text(
            "ALTER TABLE person_notes ADD COLUMN evidence_json TEXT DEFAULT ''"
        ))
    # 回填只在提案表存在时做：老迁移链会给部分账号库顺带建 person_notes，
    # 但 memory_proposals 只在 meta（或部分库）存在（2026-09-29 实查）。
    has_proposals = conn.execute(text(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_proposals'"
    )).fetchone() is not None
    if not has_proposals:
        return
    conn.execute(text(
        """
        UPDATE person_notes
           SET source_proposal_id = COALESCE((
                   SELECT p.id FROM memory_proposals p
                   WHERE p.status = 'approved'
                     AND p.subject_key = person_notes.person_key
                     AND p.chat_id = person_notes.chat_id
                     AND instr(person_notes.content, p.content) > 0
                   ORDER BY p.id DESC LIMIT 1), source_proposal_id),
               evidence_json = COALESCE((
                   SELECT p.evidence_json FROM memory_proposals p
                   WHERE p.status = 'approved'
                     AND p.subject_key = person_notes.person_key
                     AND p.chat_id = person_notes.chat_id
                     AND instr(person_notes.content, p.content) > 0
                   ORDER BY p.id DESC LIMIT 1), evidence_json)
         WHERE (source_proposal_id IS NULL OR source_proposal_id = 0)
           AND EXISTS (
               SELECT 1 FROM memory_proposals p
               WHERE p.status = 'approved'
                 AND p.subject_key = person_notes.person_key
                 AND p.chat_id = person_notes.chat_id
                 AND instr(person_notes.content, p.content) > 0
           )
        """
    ))


@migration(50, "terminal_events turn_id, reply parent backfill and command links")
def _m050(conn, insp):
    """A1-a / A1-c 收口：``turn_id`` 实列 + reply 父链回填 + 命令链接回填。

    2026-09-29 实测：``command_id`` 非空 0/5837（写路径丢了 recorder 返回值）、
    389 条 ``reply`` 的 ``parent_event_id`` 写的是**平台 msg_id**（按事件 id
    join 全 orphan）。这里一次性补齐三件事，**只回填能无歧义解析的行**，
    解不了的保持原值（读取侧兼容见 core/context/chat_window.py）：

    1. ``turn_id`` 实列 + ``(turn_id, sequence)`` 索引。此后 turn_id 由采集侧
       在事件行写入时生成并随载荷下传（core/terminal/shadow.py），这里只补列；
    2. reply 父链：平台 msg_id → 事件 id。先按实列 ``external_msg_id`` 匹配，
       pre-46 行再按 ``metadata_json.msg_id`` 兜底；同一四元组恰好命中 1 行才写
       （偏索引降级过的情况下可能有重复键，重复的一律不动）；
    3. 命令链接：``command_records.metadata_json.msg_id`` 与事件四元组对上时回填
       ``terminal_events.command_id``（只写当前为空的行）。事件侧键与第 2 条同口径：
       pre-46 行 ``external_msg_id`` 还是空的，用 ``metadata_json.msg_id`` 兜底。

    幂等：三条 UPDATE 都带「尚未回填」守卫，跑两次结果一致、不重复改写。
    用 conn 直查 sqlite_master / PRAGMA，避免 inspector 缓存误判（同 _m046）。
    事务：整条链在 ``run_migrations`` 的 ``engine.begin()`` 里执行，失败整体回滚，
    不留半状态。50 不进 ``_META_MIGRATION_VERSIONS``（见上），保证 46 先落地。
    """
    if conn.execute(text(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='terminal_events'"
    )).fetchone() is None:
        return
    cols = {
        row[1]
        for row in conn.execute(text("PRAGMA table_info(terminal_events)")).fetchall()
    }
    if "turn_id" not in cols:
        conn.execute(text(
            "ALTER TABLE terminal_events ADD COLUMN turn_id TEXT DEFAULT ''"
        ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_terminal_events_turn "
        "ON terminal_events (turn_id, sequence)"
    ))
    # ① reply 父链，走实列 external_msg_id（_m046 之后的行都能这样解）
    conn.execute(text(
        """
        UPDATE terminal_events
           SET parent_event_id = (
                   SELECT p.event_id FROM terminal_events p
                    WHERE p.external_msg_id = terminal_events.parent_event_id
                      AND p.platform = terminal_events.platform
                      AND p.bot_account_id = terminal_events.bot_account_id
                      AND p.chat_id = terminal_events.chat_id
                    LIMIT 1)
         WHERE event_type = 'reply'
           AND coalesce(parent_event_id, '') != ''
           AND NOT EXISTS (
                   SELECT 1 FROM terminal_events p2
                    WHERE p2.event_id = terminal_events.parent_event_id)
           AND (
                   SELECT COUNT(*) FROM terminal_events p3
                    WHERE p3.external_msg_id = terminal_events.parent_event_id
                      AND p3.platform = terminal_events.platform
                      AND p3.bot_account_id = terminal_events.bot_account_id
                      AND p3.chat_id = terminal_events.chat_id) = 1
        """
    ))
    # ② pre-46 行兜底：external_msg_id 还空着时用 metadata_json.msg_id 找父事件
    conn.execute(text(
        """
        UPDATE terminal_events
           SET parent_event_id = (
                   SELECT p.event_id FROM terminal_events p
                    WHERE coalesce(
                              nullif(p.external_msg_id, ''),
                              json_extract(p.metadata_json, '$.msg_id')
                          ) = terminal_events.parent_event_id
                      AND p.platform = terminal_events.platform
                      AND p.bot_account_id = terminal_events.bot_account_id
                      AND p.chat_id = terminal_events.chat_id
                    LIMIT 1)
         WHERE event_type = 'reply'
           AND coalesce(parent_event_id, '') != ''
           AND NOT EXISTS (
                   SELECT 1 FROM terminal_events p2
                    WHERE p2.event_id = terminal_events.parent_event_id)
           AND (
                   SELECT COUNT(*) FROM terminal_events p3
                    WHERE coalesce(
                              nullif(p3.external_msg_id, ''),
                              json_extract(p3.metadata_json, '$.msg_id')
                          ) = terminal_events.parent_event_id
                      AND p3.platform = terminal_events.platform
                      AND p3.bot_account_id = terminal_events.bot_account_id
                      AND p3.chat_id = terminal_events.chat_id) = 1
        """
    ))
    # ③ 命令链接（A1-a）。command_records 只在 meta / 老库存在；没有就跳过。
    # 事件侧键与 ② 同口径：pre-46 行的 external_msg_id 还是空的，只能从
    # metadata_json.msg_id 兜底（否则这些行的命令链接永远补不上）。
    if conn.execute(text(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='command_records'"
    )).fetchone() is None:
        return
    conn.execute(text(
        """
        UPDATE terminal_events
           SET command_id = (
                   SELECT c.command_id FROM command_records c
                    WHERE json_extract(c.metadata_json, '$.msg_id')
                          = coalesce(
                                nullif(terminal_events.external_msg_id, ''),
                                json_extract(terminal_events.metadata_json, '$.msg_id')
                            )
                      AND c.origin_platform = terminal_events.platform
                      AND c.origin_bot_account_id = terminal_events.bot_account_id
                      AND c.origin_chat_id = terminal_events.chat_id
                    LIMIT 1)
         WHERE coalesce(command_id, '') = ''
           AND coalesce(
                   nullif(external_msg_id, ''),
                   json_extract(metadata_json, '$.msg_id')
               ) != ''
           AND event_type IN ('message', 'question', 'request', 'command')
           AND (
                   SELECT COUNT(*) FROM command_records c2
                    WHERE json_extract(c2.metadata_json, '$.msg_id')
                          = coalesce(
                                nullif(terminal_events.external_msg_id, ''),
                                json_extract(terminal_events.metadata_json, '$.msg_id')
                            )
                      AND c2.origin_platform = terminal_events.platform
                      AND c2.origin_bot_account_id = terminal_events.bot_account_id
                      AND c2.origin_chat_id = terminal_events.chat_id) = 1
        """
    ))


@migration(51, "event outbox claim and lease")
def _m051(conn, insp):
    """REC-8：``event_outbox`` 加行级 claim / 租约列。

    无 claim 时两条并发 drain（周期 job 与同步路径）会读到同一份 ``attempts``
    快照、各写一个绝对值 → 漏计 / 绕过止损，且同一行被投递两次。51 加
    ``claimed_at / claimed_by / lease_until``，投递方先用
    ``claim_outbox_batch`` 的原子 UPDATE 夺取所有权（``attempts = attempts + 1``），
    结清时按 ``claimed_by`` 校验；租约过期即视为可回收。

    account 库独有（event_outbox 由 _m047 以 ``messages`` 存在为守卫建表），
    meta 库直接跳过；用 conn 直查 sqlite_master / PRAGMA，避免 inspector 缓存。
    幂等：跑两次结果一致（列存在则不加、索引 IF NOT EXISTS）。
    """
    if conn.execute(text(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='event_outbox'"
    )).fetchone() is None:
        return
    cols = {
        row[1]
        for row in conn.execute(text("PRAGMA table_info(event_outbox)")).fetchall()
    }
    for name, ddl in (
        ("claimed_at", "ALTER TABLE event_outbox ADD COLUMN claimed_at DATETIME"),
        ("claimed_by", "ALTER TABLE event_outbox ADD COLUMN claimed_by TEXT DEFAULT ''"),
        ("lease_until", "ALTER TABLE event_outbox ADD COLUMN lease_until DATETIME"),
    ):
        if name not in cols:
            conn.execute(text(ddl))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_event_outbox_lease "
        "ON event_outbox (status, lease_until)"
    ))


@migration(52, "work_items and work_item_runs ledger tables (P1)")
def _m052(conn, insp):
    """P1 记录统一：建 ``work_items`` / ``work_item_runs``（**只在 META**）。

    两张表是 ``terminal_events`` 的同步双写读模型（docs/任务模型.md §7.1 ①）：
    ``reminders`` 仍是唯一执行权威，本表只是单向投影。DDL 必须与
    ``db.meta_models.WorkItemRow`` / ``WorkItemRunRow`` **同形**（列名 / 类型 /
    NOT NULL / DEFAULT / 索引全对齐）：新库由 ``create_all`` 建、老库由本条补，
    两边形状不一致就是 ENG-17（迁移 51 nullable vs create_all NOT NULL）那类漂移；
    ``tests/test_work_item_p1.py`` 用「create_all 库 vs v51 + run_migrations 库」的
    PRAGMA 对照锁住。

    **两个坑（照 ``_m050`` 的教训写死在这里）**：

    1. 52 **不进** ``_META_MIGRATION_VERSIONS``（上限 39）：``run_meta_migrations``
       会把自己执行的版本盖章。bootstrap 的顺序是 ``init_meta_db``（``create_all``
       → 元链）再 ``run_migrations``（账号链），52 进那个元组 → 元链先执行并盖章
       52（``create_all`` 刚建好 ``terminal_events`` / ``account_registry``，守卫能
       过，DDL 照样建出表），随后账号链的 ``m.version > current`` 里 current=52 →
       **40–51 这 12 个版本在 META 上被永久跳过**（46 的
       ``terminal_events.external_msg_id`` 与查重索引、50 的 turn_id / command 链
       全缺），即「比当前版本低就再也不补」那类静默缺失。所以真正的损失**不是
       「表建不出来」**，而是这一整段中间版本永远补不上；
    2. 52 **不进** ``_ACCOUNT_SKIP_MIGRATION_VERSIONS``：账号链遇到 skip 集里的版本
       只盖章不执行，而 meta 链又跑不到它 → **哪里都不执行**。

    于是 52 由账号链 ``run_migrations`` 执行（meta 与账号库都会走到），用守卫挑出
    META：账号库**也有** ``terminal_events``（账号链同样跑 _m033；2026-09-30 实测 6 个
    账号库全部命中），单靠它区分不出 META；``account_registry`` 由 ``init_meta_db``
    的 ``create_all`` 建、账号库永不建，是可靠判据。账号库在这条迁移里**只盖章不建表**
    （``run_migrations`` 对每个执行过的版本统一盖章）。

    幂等：表与索引都用 ``IF NOT EXISTS``，跑两次结果一致。
    """
    if conn.execute(text(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='terminal_events'"
    )).fetchone() is None:
        return
    if conn.execute(text(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='account_registry'"
    )).fetchone() is None:
        return
    conn.execute(text(
        """
        CREATE TABLE IF NOT EXISTS work_items (
            id INTEGER NOT NULL,
            work_item_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            kind TEXT DEFAULT 'notify' NOT NULL,
            "trigger" TEXT DEFAULT 'at' NOT NULL,
            status TEXT DEFAULT 'open' NOT NULL,
            goal TEXT DEFAULT '' NOT NULL,
            spec_json TEXT DEFAULT '{}' NOT NULL,
            spec_schema_version INTEGER DEFAULT 1 NOT NULL,
            priority INTEGER DEFAULT 1 NOT NULL,
            repeat_spec_json TEXT DEFAULT '' NOT NULL,
            occurrences_done INTEGER DEFAULT 0 NOT NULL,
            due_at DATETIME,
            requester_person_id TEXT DEFAULT '' NOT NULL,
            requester_account_id TEXT DEFAULT '' NOT NULL,
            requester_role TEXT DEFAULT 'ordinary' NOT NULL,
            requester_is_owner INTEGER DEFAULT 0 NOT NULL,
            target_platform TEXT DEFAULT '' NOT NULL,
            target_bot_account_id TEXT DEFAULT '' NOT NULL,
            target_chat_id TEXT DEFAULT '' NOT NULL,
            target_chat_type TEXT DEFAULT '' NOT NULL,
            origin_platform TEXT DEFAULT '' NOT NULL,
            origin_bot_account_id TEXT DEFAULT '' NOT NULL,
            origin_chat_id TEXT DEFAULT '' NOT NULL,
            origin_chat_type TEXT DEFAULT '' NOT NULL,
            mention_names_json TEXT DEFAULT '[]' NOT NULL,
            external_msg_id TEXT DEFAULT '' NOT NULL,
            our_message_id TEXT DEFAULT '' NOT NULL,
            session_id TEXT DEFAULT '' NOT NULL,
            thread_id TEXT DEFAULT '' NOT NULL,
            command_id TEXT DEFAULT '' NOT NULL,
            authorization_id TEXT DEFAULT '' NOT NULL,
            source_event_id TEXT DEFAULT '' NOT NULL,
            attempts INTEGER DEFAULT 0 NOT NULL,
            max_attempts INTEGER DEFAULT 3 NOT NULL,
            next_retry_at DATETIME,
            last_error TEXT DEFAULT '' NOT NULL,
            last_error_class TEXT DEFAULT '' NOT NULL,
            last_error_code TEXT DEFAULT '' NOT NULL,
            claim_owner TEXT DEFAULT '' NOT NULL,
            claimed_at DATETIME,
            cancel_reason TEXT DEFAULT '' NOT NULL,
            cancelled_by TEXT DEFAULT '' NOT NULL,
            cancelled_at DATETIME,
            fallback_text TEXT DEFAULT '' NOT NULL,
            acceptance_criteria TEXT DEFAULT '' NOT NULL,
            delivery_kind TEXT DEFAULT 'text' NOT NULL,
            format_json TEXT DEFAULT '{}' NOT NULL,
            visibility_scope TEXT DEFAULT '' NOT NULL,
            owner_only INTEGER DEFAULT 0 NOT NULL,
            redaction_policy TEXT DEFAULT '' NOT NULL,
            user_tz TEXT DEFAULT '' NOT NULL,
            intended_local_time DATETIME,
            punctuality TEXT DEFAULT 'normal' NOT NULL,
            lease_deadline DATETIME,
            schema_version INTEGER DEFAULT 1 NOT NULL,
            record_version INTEGER DEFAULT 1 NOT NULL,
            created_by TEXT DEFAULT '' NOT NULL,
            created_at DATETIME,
            updated_at DATETIME,
            finished_at DATETIME,
            legacy_reminder_id INTEGER,
            PRIMARY KEY (id),
            CONSTRAINT ux_work_item_id UNIQUE (work_item_id),
            CONSTRAINT ux_work_items_idempotency_key UNIQUE (idempotency_key)
        )
        """
    ))
    conn.execute(text(
        """
        CREATE TABLE IF NOT EXISTS work_item_runs (
            id INTEGER NOT NULL,
            run_id TEXT NOT NULL,
            work_item_id TEXT NOT NULL,
            run_seq INTEGER DEFAULT 0 NOT NULL,
            trigger_source TEXT DEFAULT 'timer' NOT NULL,
            started_at DATETIME,
            finished_at DATETIME,
            duration_ms INTEGER DEFAULT 0 NOT NULL,
            outcome TEXT DEFAULT '' NOT NULL,
            trace_ref TEXT DEFAULT '' NOT NULL,
            send_ids_json TEXT DEFAULT '[]' NOT NULL,
            lease_wait_ms INTEGER DEFAULT 0 NOT NULL,
            delay_reason TEXT DEFAULT '' NOT NULL,
            reply_text TEXT DEFAULT '' NOT NULL,
            delivered_external_msg_id TEXT DEFAULT '' NOT NULL,
            error_class TEXT DEFAULT '' NOT NULL,
            error_message TEXT DEFAULT '' NOT NULL,
            PRIMARY KEY (id),
            CONSTRAINT ux_work_item_run_id UNIQUE (run_id)
        )
        """
    ))
    for index_sql in (
        "CREATE INDEX IF NOT EXISTS ix_work_items_status_due "
        "ON work_items (status, due_at)",
        "CREATE INDEX IF NOT EXISTS ix_work_items_due_at "
        "ON work_items (due_at)",
        "CREATE INDEX IF NOT EXISTS ix_work_items_requester_person "
        "ON work_items (requester_person_id)",
        # 四元组 dedup：**非唯一**（同一条消息可建两条提醒；唯一键是 idempotency_key）
        "CREATE INDEX IF NOT EXISTS ix_work_items_target_dedup "
        "ON work_items (target_platform, target_bot_account_id, target_chat_id, "
        "external_msg_id)",
        "CREATE INDEX IF NOT EXISTS ix_work_items_command "
        "ON work_items (command_id)",
        "CREATE INDEX IF NOT EXISTS ix_work_items_source_event "
        "ON work_items (source_event_id)",
        "CREATE INDEX IF NOT EXISTS ix_work_items_legacy_reminder "
        "ON work_items (legacy_reminder_id)",
        "CREATE INDEX IF NOT EXISTS ix_work_item_runs_item_seq "
        "ON work_item_runs (work_item_id, run_seq)",
    ):
        conn.execute(text(index_sql))


@migration(53, "cursor composite key (sequence, id) for event projections (ENG-23)")
def _m053(conn, insp):
    """ENG-23：投影游标改成**复合键** ``(sequence, id)``，并给事件流补索引。

    旧游标只存 ``last_sequence``，读侧谓词是 ``sequence > :cursor``、写侧按
    ``max(sequence)`` 推进。于是两类行永久读不到：

    1. **跨批**：批 A 以 seq=S 结尾 → 游标 = S → 批 B 用 ``> S`` 读，
       同号 S 的后续行被跳掉；
    2. **迟到行**：游标推进到 S 之后才落库的同号行（B6 之前的重号遗留），
       同样被 ``> S`` 跳过。

    改法：游标表加 ``last_id``，读侧谓词变
    ``sequence > :seq OR (sequence = :seq AND id > :id)``，写侧按**批内最后一条**
    的 ``(sequence, id)`` 推进；两边的游标表都要加列（记忆投影 + 分析投影同 bug）。
    ``(sequence, id)`` 索引让重号区间内的追加读不再退化成全表扫。

    **两个坑（照 ``_m050`` / ``_m052`` 的教训写死在这里）**：

    1. 53 **不进** ``_META_MIGRATION_VERSIONS``（上限 39）：``run_meta_migrations``
       会给自己执行的版本盖章、并把 current 提到 53，账号链随后
       ``m.version > current`` 会把 40–52 这 13 个版本在 META 上**永久跳过**
       （46 的 ``external_msg_id`` 查重索引、50 的 turn_id、51 的 outbox 租约、
       52 的 work_items 全缺）；
    2. 53 **不进** ``_ACCOUNT_SKIP_MIGRATION_VERSIONS``：进了 skip 集就是账号链
       只盖章不执行，而 meta 链又跑不到它 → 两个库里都没有这一列。

    于是 53 与 52 同路：由账号链 ``run_migrations`` 执行（meta 与账号库都会走到），
    用 ``account_registry`` 守卫挑出 META（账号库也有 ``terminal_events``，
    单靠它区分不出 META）。账号库在这条迁移里只盖章不建表。

    幂等：列存在则不加（PRAGMA 先查），索引 ``IF NOT EXISTS``，跑两次结果一致。
    """
    if conn.execute(text(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='terminal_events'"
    )).fetchone() is None:
        return
    if conn.execute(text(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='account_registry'"
    )).fetchone() is None:
        return
    for table in ("memory_projection_cursors", "analysis_projection_cursors"):
        if conn.execute(text(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=:name"
        ), {"name": table}).fetchone() is None:
            continue
        columns = {
            row[1]
            for row in conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
        }
        if "last_id" not in columns:
            conn.execute(text(
                f"ALTER TABLE {table} ADD COLUMN last_id INTEGER DEFAULT 0"
            ))
    # 复合游标的读路径索引：同号区间内按 id 追加读，走索引而不是扫全表。
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_terminal_events_sequence "
        "ON terminal_events (sequence, id)"
    ))


@migration(54, "session_focus: one focus row per session (N3 / R-T5)")
def _m054(conn, insp):
    """N3 焦点状态：建 ``session_focus``（**每会话一行**，P0-WP3 只落状态载体）。

    R-T5 把 ``docs/话题簇与锚点规格.md`` §十二【已定 1】从「读时派生不落库」
    修订为「**除焦点状态外仍不落库**」：话题簇与锚点判定照旧读时派生，只有
    "这一轮在聊哪件事 / 主角是谁" 这一行允许落库。本迁移只建**状态载体**，
    聚类内核（``core/context/threads.py``）与消费方接线属 P1，**不在本迁移里**。

    表结构必须与 ``db.meta_models.SessionFocusState`` **同形**（列名 / 类型 /
    NOT NULL / DEFAULT / 主键 / 索引全对齐）：新库由 ``create_all`` 建、老库由本条补，
    两边形状不一致就是 ENG-17（迁移 51 nullable vs create_all NOT NULL）那类漂移；
    ``tests/test_focus_state.py`` 用「create_all 库 vs 旧库 + run_migrations 库」的
    PRAGMA 对照锁住。

    **两个坑（照 ``_m052`` / ``_m053`` 的教训写死在这里）**：

    1. 54 **不进** ``_META_MIGRATION_VERSIONS``（上限 39）：``run_meta_migrations``
       会把自己执行的版本盖章并把 current 提到 54，账号链随后
       ``m.version > current`` 会把 40–53 这 14 个版本在 META 上**永久跳过**
       （46 的 ``terminal_events.external_msg_id`` 查重索引、50 的 turn_id、
       51 的 outbox 租约、52 的 work_items、53 的复合游标全缺）；
    2. 54 **不进** ``_ACCOUNT_SKIP_MIGRATION_VERSIONS``：进了 skip 集就是账号链
       只盖章不执行，而 meta 链又跑不到它 → 两个库里都没有这张表。

    于是 54 与 52 / 53 同路：由账号链 ``run_migrations`` 执行（meta 与账号库都会
    走到）。**守卫只用一道、且与 52 / 53 刻意不同**：52 / 53 建的是 meta 专有表，
    要靠 ``account_registry`` 把账号库挡在外面；``session_focus`` 按
    ``(platform, account_id, chat_id)`` 分会话，账号库与 META 都是合法宿主，
    所以只要库还是"应用库"（有 ``terminal_events``）就照建，账号库不再只盖章。
    这与「两个坑」不冲突：坑说的是**注册位置**（两个版本元组都不能进），
    不是守卫强度。

    幂等：表与索引都用 ``IF NOT EXISTS``，跑两次结果一致（用例跑两次比对 PRAGMA）。
    """
    if conn.execute(text(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='terminal_events'"
    )).fetchone() is None:
        return
    conn.execute(text(
        """
        CREATE TABLE IF NOT EXISTS session_focus (
            platform TEXT DEFAULT '' NOT NULL,
            account_id TEXT DEFAULT '' NOT NULL,
            chat_id TEXT DEFAULT '' NOT NULL,
            current_topic TEXT DEFAULT '' NOT NULL,
            current_anchor_person_id TEXT DEFAULT '' NOT NULL,
            previous_topic TEXT DEFAULT '' NOT NULL,
            suspended_json TEXT DEFAULT '[]' NOT NULL,
            topic_since DATETIME,
            updated_at DATETIME,
            revision INTEGER DEFAULT 1 NOT NULL,
            PRIMARY KEY (platform, account_id, chat_id)
        )
        """
    ))
    for index_sql in (
        "CREATE INDEX IF NOT EXISTS ix_session_focus_anchor "
        "ON session_focus (current_anchor_person_id)",
        "CREATE INDEX IF NOT EXISTS ix_session_focus_updated "
        "ON session_focus (updated_at)",
    ):
        conn.execute(text(index_sql))


@migration(55, "rule_use_ledger: N4 规则引擎 quota 维的用量台账")
def _m055(conn, insp):
    """N4 规则引擎（``core/rules/engine.py``）：建 ``quota`` 维要用的用量台账。

    规则引擎五维里的 **「次数」维**（``condition`` / ``target`` / **``quota``** /
    ``action`` / ``priority``，`docs/decisions/架构评估.md` §3.4 :259）要能回答"这条规则在
    滚动 1 小时 / 24 小时里用了几次"，而**次数必须跨进程、跨重启算数**：收编过来
    的两处本来就是这种语义——``agents/tools/reminder.py`` 的
    ``NON_OWNER_PENDING_CAP``（非主人待提醒条数）与
    ``core/terminal/proactive_service.py:56`` 的小时/每日上限（底层
    ``ProactiveSendLog`` 落库）。所以计数落一行一动作的**追加式台账**，
    不落内存计数器。

    本迁移只建**台账表**，判定（:class:`core.rules.engine.RuleEngine`）与消费方
    接线属同一期，**判断逻辑不在这条迁移里**。表结构必须与
    ``db.meta_models.RuleUseLedger`` **同形**（列名 / 类型 / NOT NULL / DEFAULT /
    主键 / 索引全对齐）：新库由 ``create_all`` 建、老库由本条补，两边形状不一致就是
    ENG-17（迁移 51 nullable vs create_all NOT NULL）那类漂移；
    ``tests/test_rules_engine.py`` 用「create_all 库 vs 旧库 + run_migrations 库」的
    PRAGMA 对照锁住（同 ``_m054`` 的口径）。

    **两个坑（照 ``_m052`` / ``_m053`` / ``_m054`` 的教训写死在这里）**：

    1. 55 **不进** ``_META_MIGRATION_VERSIONS``（上限 39）：``run_meta_migrations``
       会把自己执行的版本盖章并把 current 提到 55，账号链随后
       ``m.version > current`` 会把 40–54 这 15 个版本在 META 上**永久跳过**
       （46 的 ``external_msg_id`` 查重索引、50 的 turn_id、51 的 outbox 租约、
       52 的 work_items、53 的复合游标、54 的 ``session_focus`` 全缺）；
    2. 55 **不进** ``_ACCOUNT_SKIP_MIGRATION_VERSIONS``：进了 skip 集就是账号链
       只盖章不执行，而 meta 链又跑不到它 → 两个库里都没有这张表。

    于是 55 与 52 / 53 / 54 同路：由账号链 ``run_migrations`` 执行（meta 与账号库都会
    走到）。守卫与 54 逐字相同——``rule_use_ledger`` 按 ``(动作, 平台, 账号, 会话,
    发起人)`` 分会话/分账号，账号库与 META 都是合法宿主，所以只要库还是"应用库"
    （有 ``terminal_events``）就照建，账号库不再只盖章。「两个坑」说的是**注册位置**
    （两个版本元组都不能进），不是守卫强度。

    幂等：表与索引都用 ``IF NOT EXISTS``，跑两次结果一致（用例跑两次比对 PRAGMA）。
    """
    if conn.execute(text(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='terminal_events'"
    )).fetchone() is None:
        return
    conn.execute(text(
        """
        CREATE TABLE IF NOT EXISTS rule_use_ledger (
            id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
            rule_key TEXT DEFAULT '' NOT NULL,
            quota_key TEXT DEFAULT '' NOT NULL,
            platform TEXT DEFAULT '' NOT NULL,
            account_id TEXT DEFAULT '' NOT NULL,
            chat_id TEXT DEFAULT '' NOT NULL,
            actor_id TEXT DEFAULT '' NOT NULL,
            action_id TEXT DEFAULT '' NOT NULL,
            used_at DATETIME,
            created_at DATETIME
        )
        """
    ))
    for index_sql in (
        "CREATE INDEX IF NOT EXISTS ix_rule_use_quota_used "
        "ON rule_use_ledger (quota_key, used_at)",
        "CREATE INDEX IF NOT EXISTS ix_rule_use_rule_key "
        "ON rule_use_ledger (rule_key)",
        "CREATE INDEX IF NOT EXISTS ix_rule_use_ledger_used_at "
        "ON rule_use_ledger (used_at)",
    ):
        conn.execute(text(index_sql))


@migration(56, "claim_ledger: M2-4 claim 台账（事实权威序 + 更正链）")
def _m056(conn, insp):
    """M2-4 claim 台账（``core/context/message_facts.py``）：事实权威序的载体。

    `docs/archive/任务清单_2026-10-06.md:71` 的三条硬要求要落成**可查询结构**，本迁移只建**载体**
    （判定、抽取、闸门升级都在 ``core/context/message_facts.py`` 与
    ``core/speech/guards.py`` 里，**不在这条迁移里**）：

    * **三级权威**（同一 ``(chat, person, field)`` 上 **自报 > 转述 > 推测**，
      低权威不得覆盖高权威）→ ``authority`` 列 + 读侧的"只取仍有效的行"；
    * **更正历史**（被更新的旧 claim 显式标 ``superseded_by``，**不得静默改写**）
      → ``superseded_by`` / ``supersede_reason`` / ``superseded_at`` 三列，
      外加撤回用的 ``withdrawn_at`` / ``withdrawn_reason`` / ``withdrawn_evidence``；
      **没有任何一列是"旧正文的副本"**——旧行的 ``value`` 一旦写入就不再被 UPDATE；
    * **内容归并**（同一 ``(chat, content_key)`` 的行归到一起）→ ``content_key`` 列，
      口径是 ``core.context.message_facts.content_key``（字面归一化）。本列的原始动机
      是"第 N 次复述"计数，那条判据已随复读指纹退役（2026-10-24，A6）——列与归并
      语义保留，本表仍不自己算第二套归一化。

    边界（**不是第四份事实仓库**，见 ``core/context/message_facts.py`` 文件头）：
    ``person_notes``（迁移 48）/ ``person_observations`` / ``memory_projections``
    各管各的"内容"；本表只管"内容之间的**效力次序**"，用
    ``source_chat_id`` / ``source_message_id`` / ``source_claim_id`` 指向原始证据，
    **不复制它们的正文**。拒绝与重复覆盖另落一行 ``claim_ledger_audit``
    （**拒绝不是静默**——这正是"不得静默改写"的边界）。

    表结构必须与 ``db.meta_models.ClaimLedgerRow`` / ``ClaimLedgerAuditRow``
    **同形**（列名 / 类型 / NOT NULL / DEFAULT / 主键 / 索引全对齐）：新库由
    ``create_all`` 建、老库由本条补，两边形状不一致就是 ENG-17（迁移 51 nullable
    vs create_all NOT NULL）那类漂移；``tests/test_claim_ledger.py`` 用
    「create_all 库 vs 旧库 + run_migrations 库」的 PRAGMA 对照锁住（同 ``_m054``
    / ``_m055`` 的口径）。

    **两个坑（照 ``_m052`` … ``_m055`` 的教训写死在这里）**：

    1. 56 **不进** ``_META_MIGRATION_VERSIONS``（上限 39）：``run_meta_migrations``
       会把自己执行的版本盖章并把 current 提到 56，账号链随后
       ``m.version > current`` 会把 40–55 这 16 个版本在 META 上**永久跳过**
       （46 的 ``external_msg_id`` 查重索引、50 的 turn_id、51 的 outbox 租约、
       52 的 work_items、53 的复合游标、54 的 ``session_focus``、55 的
       ``rule_use_ledger`` 全缺）；
    2. 56 **不进** ``_ACCOUNT_SKIP_MIGRATION_VERSIONS``：进了 skip 集就是账号链
       只盖章不执行，而 meta 链又跑不到它 → 两个库里都没有这张表。

    于是 56 与 52 / 53 / 54 / 55 同路：由账号链 ``run_migrations`` 执行（meta 与
    账号库都会走到）。守卫与 54 / 55 逐字相同——``claim_ledger`` 按
    ``(platform, account_id, chat_id)`` 分会话，账号库与 META 都是合法宿主，
    所以只要库还是"应用库"（有 ``terminal_events``）就照建，账号库不再只盖章。
    「两个坑」说的是**注册位置**（两个版本元组都不能进），不是守卫强度。

    幂等：表与索引都用 ``IF NOT EXISTS``，跑两次结果一致（用例跑两次比对 PRAGMA）。

    **本迁移不碰任何行**：它只建空表，不读、不写、不回填（``YINYUE_FEATURE_CLAIM_LEDGER``
    默认 ``off`` 时也没有任何代码路径会碰它）。
    """
    if conn.execute(text(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='terminal_events'"
    )).fetchone() is None:
        return
    conn.execute(text(
        """
        CREATE TABLE IF NOT EXISTS claim_ledger (
            id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
            platform TEXT DEFAULT '' NOT NULL,
            account_id TEXT DEFAULT '' NOT NULL,
            chat_id TEXT DEFAULT '' NOT NULL,
            person_key TEXT DEFAULT '' NOT NULL,
            field_key TEXT DEFAULT '' NOT NULL,
            value TEXT DEFAULT '' NOT NULL,
            content_key TEXT DEFAULT '' NOT NULL,
            authority TEXT DEFAULT 'speculation' NOT NULL,
            source_kind TEXT DEFAULT '' NOT NULL,
            source_actor_id TEXT DEFAULT '' NOT NULL,
            source_chat_id TEXT DEFAULT '' NOT NULL,
            source_message_id TEXT DEFAULT '' NOT NULL,
            source_claim_id INTEGER DEFAULT 0 NOT NULL,
            observed_at DATETIME,
            superseded_by INTEGER DEFAULT 0 NOT NULL,
            supersede_reason TEXT DEFAULT '' NOT NULL,
            superseded_at DATETIME,
            withdrawn_at DATETIME,
            withdrawn_reason TEXT DEFAULT '' NOT NULL,
            withdrawn_evidence TEXT DEFAULT '' NOT NULL,
            created_at DATETIME
        )
        """
    ))
    conn.execute(text(
        """
        CREATE TABLE IF NOT EXISTS claim_ledger_audit (
            id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
            action TEXT DEFAULT '' NOT NULL,
            chat_id TEXT DEFAULT '' NOT NULL,
            person_key TEXT DEFAULT '' NOT NULL,
            field_key TEXT DEFAULT '' NOT NULL,
            value TEXT DEFAULT '' NOT NULL,
            authority TEXT DEFAULT '' NOT NULL,
            existing_claim_id INTEGER DEFAULT 0 NOT NULL,
            existing_authority TEXT DEFAULT '' NOT NULL,
            reason TEXT DEFAULT '' NOT NULL,
            created_at DATETIME
        )
        """
    ))
    for index_sql in (
        # 读法驱动：① 权威序查询（chat + person + field 上取仍有效的行）；
        "CREATE INDEX IF NOT EXISTS ix_claim_ledger_scope "
        "ON claim_ledger (platform, account_id, chat_id, person_key, field_key)",
        # ② 复述抑制：同一 (chat, content_key) 在窗口内出现了几次。
        "CREATE INDEX IF NOT EXISTS ix_claim_ledger_content "
        "ON claim_ledger (chat_id, content_key)",
        # ③ 更正链回查：谁被谁更新掉 / 谁从哪条来。
        "CREATE INDEX IF NOT EXISTS ix_claim_ledger_superseded "
        "ON claim_ledger (superseded_by)",
        "CREATE INDEX IF NOT EXISTS ix_claim_ledger_source "
        "ON claim_ledger (source_claim_id)",
        "CREATE INDEX IF NOT EXISTS ix_claim_ledger_created "
        "ON claim_ledger (created_at)",
    ):
        conn.execute(text(index_sql))

@migration(57, "state_assembly: self_moves 台账 + chat_state 每会话一行")
def _m057(conn, insp):
    """WP-STATE 状态装配 v1 的两张载体表（判定与渲染不在这条迁移里）。

    口径与 _m054/_m055/_m056 完全一致：
    1. 57 不进 _META_MIGRATION_VERSIONS（上限 39），否则 run_meta_migrations 盖 57
       后账号链会把 40-56 在 META 上永久跳过；
    2. 57 不进 _ACCOUNT_SKIP_MIGRATION_VERSIONS（进了就是账号链只盖章不执行，
       meta 链又跑不到 -> 两个库都没有表）。
    由账号链 run_migrations 执行（core/bootstrap.py:353 对 meta 库也走这条链）。
    守卫同 54/55/56：只建在"应用库"（有 terminal_events）里；幂等（IF NOT EXISTS）；
    本迁移不碰任何行。表结构必须与 db.meta_models.SelfMoveRow / ChatStateRow 同形，
    由 tests/test_state_store.py 的 PRAGMA parity 锁住（ENG-17 那类漂移）。
    """
    if conn.execute(text(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='terminal_events'"
    )).fetchone() is None:
        return
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS self_moves (
            id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
            platform TEXT DEFAULT '' NOT NULL,
            account_id TEXT DEFAULT '' NOT NULL,
            chat_id TEXT DEFAULT '' NOT NULL,
            sent_at DATETIME,
            sent_at_sec DATETIME,
            move_kind TEXT DEFAULT 'other' NOT NULL,
            bit_keys_json TEXT DEFAULT '[]' NOT NULL,
            content_hash TEXT DEFAULT '' NOT NULL,
            chars INTEGER DEFAULT 0 NOT NULL,
            expects_reply INTEGER DEFAULT 0 NOT NULL,
            engagement TEXT DEFAULT 'pending' NOT NULL,
            engaged_at DATETIME,
            created_at DATETIME
        )
    """))
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS chat_state (
            platform TEXT DEFAULT '' NOT NULL,
            account_id TEXT DEFAULT '' NOT NULL,
            chat_id TEXT DEFAULT '' NOT NULL,
            last_user_at DATETIME,
            last_bot_at DATETIME,
            last_bot_move_kind TEXT DEFAULT '' NOT NULL,
            last_bot_bit_keys_json TEXT DEFAULT '[]' NOT NULL,
            last_bot_content_hash TEXT DEFAULT '' NOT NULL,
            last_bot_expects_reply INTEGER DEFAULT 0 NOT NULL,
            last_bot_engagement TEXT DEFAULT 'none' NOT NULL,
            last_bot_engaged_at DATETIME,
            updated_at DATETIME,
            revision INTEGER DEFAULT 1 NOT NULL,
            PRIMARY KEY (platform, account_id, chat_id)
        )
    """))
    conn.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_self_moves_dedup "
        "ON self_moves (platform, account_id, chat_id, content_hash, sent_at_sec)"
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_self_moves_chat_time "
        "ON self_moves (platform, account_id, chat_id, sent_at)"
    ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_self_moves_kind_time "
        "ON self_moves (chat_id, move_kind, sent_at)"
    ))

@migration(58, "state_assembly: chat_state.reset_at 翻篇边界")
def _m058(conn, insp):
    """WP-STATE v1.1：给 chat_state 加「翻篇边界」列（主人说"忘记前面"时落点）。

    与 57 同口径：不进 _META_MIGRATION_VERSIONS / _ACCOUNT_SKIP_MIGRATION_VERSIONS；
    由账号链 run_migrations 执行；只在应用库（有 terminal_events）里做；幂等
    （先查 PRAGMA，缺列才 ALTER）；表结构必须与 db.meta_models.ChatStateRow 同形
    （列尾追加，create_all 与迁移两条路顺序一致），由 tests/test_state_store.py
    parity 用例锁住。
    """
    if conn.execute(text(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='terminal_events'"
    )).fetchone() is None:
        return
    if conn.execute(text(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat_state'"
    )).fetchone() is None:
        return
    cols = {row[1] for row in conn.execute(text("PRAGMA table_info(chat_state)"))}
    if "reset_at" not in cols:
        conn.execute(text("ALTER TABLE chat_state ADD COLUMN reset_at DATETIME"))

@migration(59, "claim_ledger: 断言=原话+span（派生缓存可重建；低权威并列留档）")
def _m059(conn, insp):
    """claim 台账加 4 列：原话（``source_text``）、命中片段（``evidence_text``）、
    片段在原文里的半开偏移（``evidence_start`` / ``evidence_end``）。

    与 52–58 同路（**两个坑**照旧，见 ``_m056`` docstring）：

    1. 59 **不进** ``_META_MIGRATION_VERSIONS``（上限 39）——否则 ``run_meta_migrations``
       盖 59 后账号链会把 40–58 在 META 上永久跳过；
    2. 59 **不进** ``_ACCOUNT_SKIP_MIGRATION_VERSIONS``——进了就是账号链只盖章不执行，
       meta 链又跑不到 → 两个库都没这几列。

    由账号链 ``run_migrations`` 执行；守卫与 ``_m054``…``_m058`` 逐字相同（只在"应用库"
    即还有 ``terminal_events`` 的库里做）；**逐列幂等**（先查 PRAGMA，缺列才 ALTER）；
    **列尾追加**（``db.meta_models.ClaimLedgerRow`` 的顺序必须与这里一致，否则
    ``tests/test_claim_ledger.py`` 的 create_all vs 迁移 PRAGMA parity 立刻红）。

    这几列是 **P0-2「写时不承诺、读时透镜」** 的落点：断言不再只留一个"字段=值"的
    当前值加一行权威判决，而是原话 + 位置（谁、何时、说的是哪一段）。字段抽取降级成
    可重建派生缓存（``ClaimRecord.rebuild()`` 会拿 ``source_text`` 重跑一遍做自检）。
    低权威**不再被拒写**（转述/推测一律留档，权威序改成读时排序透镜）——那是行为侧，
    在 ``core/context/message_facts.py`` 里，**本条迁移只加列，不碰任何行、不改任何值**。

    回滚（见 ``docs/decisions/穷举整改_claim台账.md``）：SQLite ≥ 3.35 支持
    ``ALTER TABLE claim_ledger DROP COLUMN <列>``；这 4 列是纯追加，丢掉它们不会动
    任何既有列的取值（旧行只是回到"没有原话"的状态）。
    """
    if conn.execute(text(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='terminal_events'"
    )).fetchone() is None:
        return
    if conn.execute(text(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='claim_ledger'"
    )).fetchone() is None:
        return
    cols = {row[1] for row in conn.execute(text("PRAGMA table_info(claim_ledger)"))}
    if "source_text" not in cols:
        conn.execute(text(
            "ALTER TABLE claim_ledger ADD COLUMN source_text TEXT DEFAULT '' NOT NULL"))
    if "evidence_text" not in cols:
        conn.execute(text(
            "ALTER TABLE claim_ledger ADD COLUMN evidence_text TEXT DEFAULT '' NOT NULL"))
    if "evidence_start" not in cols:
        conn.execute(text(
            "ALTER TABLE claim_ledger ADD COLUMN evidence_start INTEGER DEFAULT -1 NOT NULL"))
    if "evidence_end" not in cols:
        conn.execute(text(
            "ALTER TABLE claim_ledger ADD COLUMN evidence_end INTEGER DEFAULT -1 NOT NULL"))

@migration(60, "message_media: 媒体基质（event_id/正文摘要/大小/mime/附件名）")
def _m060(conn, insp):
    """``message_media`` 补 6 列，让「那份 PDF 叫什么」有地方可查（task-26）。

    现有的 28 号表只有 path/status/md5 这些「文件能不能找到」的线索；这一层要存
    附件**语义**：``original_name``（原始文件名）、``text_excerpt``（本地抽出的正文
    摘要）、``size_bytes`` / ``mime``、``event_id``（回指 terminal_events，供召回
    路把媒体行映射回窗口行）、``meta_json``（抽取状态与失败原因，不静默）。

    守卫与 56–59 同路（见 ``_m056`` docstring 的两个坑）：只在**应用库**
    （还有 ``terminal_events`` 的库）里做；缺少 ``message_media`` 的库直接返回；
    **逐列幂等**（先 PRAGMA，缺列才 ALTER）；列尾追加。

    回滚：SQLite ≥ 3.35 支持 ``ALTER TABLE message_media DROP COLUMN <列>``；
    这 6 列是纯追加，丢掉它们不动任何既有取值。
    """
    if conn.execute(text(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='terminal_events'"
    )).fetchone() is None:
        return
    if conn.execute(text(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_media'"
    )).fetchone() is None:
        return
    cols = {row[1] for row in conn.execute(text("PRAGMA table_info(message_media)"))}
    added = (
        ("event_id", "TEXT DEFAULT '' NOT NULL"),
        ("size_bytes", "INTEGER DEFAULT 0 NOT NULL"),
        ("mime", "TEXT DEFAULT '' NOT NULL"),
        ("original_name", "TEXT DEFAULT '' NOT NULL"),
        ("text_excerpt", "TEXT DEFAULT '' NOT NULL"),
        ("meta_json", "TEXT DEFAULT '' NOT NULL"),
    )
    for name, ddl in added:
        if name not in cols:
            conn.execute(text(
                "ALTER TABLE message_media ADD COLUMN %s %s" % (name, ddl)
            ))
    for name, columns in (
        ("ix_message_media_event", "event_id"),
        ("ix_message_media_name", "chat_id, original_name"),
    ):
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS %s ON message_media(%s)" % (name, columns)
        ))


@migration(61, "terminal_events: span 三列（正文半开区间 + 媒体占位符区间）")
def _m061(conn, insp):
    """``terminal_events`` 补 3 列：``content_start`` / ``content_end`` / ``media_span_json``。

    P2「基质②」的落点（task-27）：事件行不再只有一段**文本**，还带着「这段文本在原文
    里的位置」。召回窗口当前按**列表下标**记身份（``ChainHit.row_index``、
    ``memory_id=f"recall:{chat_id}:{row_index}"``），新消息一进来下标就全体漂移 → 一次
    召回结果隔一小时无法复现。这里先补**写侧**的地址：正文半开区间 + 每个媒体占位符
    （``[图片]`` / ``[表情包:xxx]`` / ``[语音]`` / ``[文件]`` / ``[视频]``）的
    ``{kind,start,end,raw}``。

    守卫与 56–60 同路（见 ``_m056`` docstring 的两个坑）：

    1. 61 **不进** ``_META_MIGRATION_VERSIONS``（上限 39）——否则 ``run_meta_migrations``
       盖 61 后账号链会把 40–60 在 META 上永久跳过；
    2. 61 **不进** ``_ACCOUNT_SKIP_MIGRATION_VERSIONS``——进了就是账号链只盖章不执行，
       meta 链又跑不到 → 两个库都没这几列。

    由账号链 ``run_migrations`` 执行；只在**应用库**（还有 ``terminal_events`` 的库）里
    做；**逐列幂等**（先 PRAGMA，缺列才 ALTER）；**列尾追加**（``db.meta_models.TerminalEvent``
    的列顺序必须与这里一致，否则 create_all vs 迁移 PRAGMA 的 parity 用例立刻红）。

    ``-1`` = 未知，与 ``core/contracts/events.py::EvidenceRef`` 同一约定：**老行永远是
    -1**，读侧不许把 -1 当成"从 0 开始"或"没有媒体"。写入侧见
    ``core/terminal/event_store_db.py::_span_values``（列不存在时跳过三键 = 逐字旧行为）。

    回滚：SQLite ≥ 3.35 支持 ``ALTER TABLE terminal_events DROP COLUMN <列>``；这 3 列是
    纯追加，丢掉它们不动任何既有取值（旧行只是回到"没有地址"的状态），派生缓存可重建。
    """
    if conn.execute(text(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='terminal_events'"
    )).fetchone() is None:
        return
    cols = {row[1] for row in conn.execute(text("PRAGMA table_info(terminal_events)"))}
    added = (
        ("content_start", "INTEGER DEFAULT -1 NOT NULL"),
        ("content_end", "INTEGER DEFAULT -1 NOT NULL"),
        ("media_span_json", "TEXT DEFAULT '[]' NOT NULL"),
    )
    for name, ddl in added:
        if name not in cols:
            conn.execute(text(
                "ALTER TABLE terminal_events ADD COLUMN %s %s" % (name, ddl)
            ))


@migration(68, "thread and reference source coordinates")
def _m068(conn, insp):
    """只补已有线程表来源列，不根据当前账号回填历史。"""
    from db.thread_scope import upgrade_thread_sources
    upgrade_thread_sources(conn)


@migration(69, "source-bound thread summaries")
def _m069(conn, insp):
    from db.thread_scope import upgrade_thread_summaries
    upgrade_thread_summaries(conn)
