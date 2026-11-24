# -*- coding: utf-8 -*-
"""A1-a / A1-c 落地回归：命令链接、reply 父链、turn_id 采集侧穿透、Migration 50。

对应 docs/任务清单.md 的 A1-a / A1-c 两行与 docs/任务模型.md §11.1/§11.2：

① 新命令请求 → 事件行 ``command_id`` 非空、``query(command_id=…)`` 命中、join > 0；
② ``record_authorization`` 回填 ``authorization_id``；
③ ``record_execution`` 去重追加 ``execution_ids`` 并回填执行事件行；
④ reply 的 ``parent_event_id`` == 入站事件 ``event_id``（且 != 平台 msg_id）、orphan=0；
⑤ message / thought / reply 同一个 ``turn_id``，SQL ``m⋈t⋈e`` 命中 1；
⑥ 多消息合并回合采用主消息的 turn_id，不丢链、不异常；
⑦ ``chat_window`` 对「事件 id」与「legacy msg_id」两种输入都解析出正确 parent_msg_id；
⑧ Migration 50 幂等 + 回填正确（含解不了的行保持原值、重复键不硬连）；
⑨ 哑 store（无 ``link_event``）不炸；
⑩ 静态守卫：事件行的 INSERT **全仓只许 owner**（``core/terminal/event_store_db.py``）
   写，同事务的台账路径改调 owner 的 ``append_in_session``；别处自建即红，旧的
   「配方别名」``event_row_values`` / ``next_sequence_expression`` 也不许再被外借。
⑪ 重放：同一 ``(platform, account_id, external_msg_id)`` 重复投递 → 只插一行、
   拿到同一个 sequence（``append`` 与 ``append_in_session`` 两条写入路径都测）。

Run directly:
    python tests/test_event_chain_links.py
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ROOT = Path(__file__).resolve().parent.parent
_TEST_TMP_ROOT = ROOT / "data" / "_event_chain_test"
# scheduler.main 导入时会调 init_logging，先隔离日志目录（同 test_scheduler_jobs）。
_TEST_LOG_DIR = str(Path("data") / "_test_logs_event_chain")
Path(_TEST_LOG_DIR).mkdir(parents=True, exist_ok=True)
os.environ["LOG_DIR"] = _TEST_LOG_DIR
# 更要紧的是**数据库**隔离：任何一处走 core.bootstrap.init_singletons()（缓存访问器
# 的兜底、ModuleNotFoundError 之外的运行期初始化等）都会在**真实** meta 库和配置里的
# account 库上跑 init_meta_db + run_migrations。2026-09-29 实测：一次测试运行就把
# Migration 50 落到了 data/yinyue_meta.db 与 4 个 account.db（applied_at 13:32:00Z）。
# 这里把三个路径指到测试目录，保证本文件永远不碰生产库。
_ISO_ROOT = (ROOT / "data" / "_event_chain_test" / "_iso").resolve()
_ISO_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["META_DB_PATH"] = str(_ISO_ROOT / "meta.db")
os.environ["PLATFORMS_DIR"] = str(_ISO_ROOT / "platforms")
os.environ["DATA_DIR"] = str(_ISO_ROOT / "data")

UTC = timezone.utc
W = "wechat"

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from core.context.answer_links import derive_answer_links  # noqa: E402
from core.context.chat_window import messages_from_rows  # noqa: E402
from core.terminal.command_ledger import CommandRecord, CommandStatus  # noqa: E402
from core.terminal.command_runtime import (  # noqa: E402
    get_terminal_command_recorder,
    install_terminal_command_service,
    reset_terminal_command_service,
)
from core.terminal.command_service import CommandService  # noqa: E402
from core.terminal.event_model import Event, EventType  # noqa: E402
from core.terminal.event_runtime import (  # noqa: E402
    install_terminal_event_store,
    reset_terminal_event_store,
)
from core.terminal.event_store_db import (  # noqa: E402
    TerminalEventStore,
    append_in_session,
)
from core.terminal.identity_service import IdentityService  # noqa: E402
from core.terminal.principal import RelationLevel, RelationshipState  # noqa: E402
from core.terminal.shadow import (  # noqa: E402
    record_action_event,
    record_message_events,
    record_reply_event,
    record_thought_event,
)
from db.meta_repo import MetaRepositoryManager  # noqa: E402
from db.migrations import run_migrations  # noqa: E402
from db.migrations import _m033, _m034, _m050  # noqa: E402
from db.repo_commands import CommandRepository  # noqa: E402


# --------------------------------------------------------------------- helpers
def _make_temp_dir() -> Path:
    _TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)
    path = _TEST_TMP_ROOT / f"case_{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=False)
    return path


def _dt(day: int = 1, hour: int = 10) -> datetime:
    return datetime(2026, 1, day, hour, 0, 0, tzinfo=UTC)


def _owner_identity() -> IdentityService:
    return IdentityService(
        relationships={
            (W, "bot_a", "p_owner"): RelationshipState(
                W, "bot_a", "p_owner", RelationLevel.OWNER, 1.0, "seed"
            ),
            (W, "bot_a", "p_friend"): RelationshipState(
                W, "bot_a", "p_friend", RelationLevel.ORDINARY, 0.4, "seed"
            ),
        },
        person_identities={
            (W, "owner_raw"): "p_owner",
            (W, "friend_raw"): "p_friend",
        },
    )


def _build(tmp: Path):
    db_path = tmp / "chain.db"
    meta = MetaRepositoryManager(str(db_path))
    run_migrations(str(db_path))
    return meta, db_path


def _cleanup(meta, db_path: Path) -> None:
    if meta is not None:
        try:
            meta.engine.dispose()
        except Exception:
            pass
    for suffix in ("", "-wal", "-shm"):
        try:
            db_path.with_name(db_path.name + suffix).unlink(missing_ok=True)
        except PermissionError:
            pass


def _raw(
    msg_id: str,
    *,
    chat_id: str = "friend_raw",
    chat_type: str = "private",
    sender_id: str = "friend_raw",
    sender_name: str = "朋友",
    content: str = "在吗",
    created_at: datetime | None = None,
) -> dict:
    return {
        "msg_id": msg_id,
        "chat_id": chat_id,
        "chat_type": chat_type,
        "sender_id": sender_id,
        "sender_name": sender_name,
        "content": content,
        "created_at": (created_at or _dt(1, 10)).isoformat(),
        "source": W,
    }


def _scalar(engine, sql: str, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar()


def _orphan_replies(engine) -> int:
    """parent 非空、但指向的 event_id 不在事件表里的 reply 行数。"""
    return int(
        _scalar(
            engine,
            "SELECT COUNT(*) FROM terminal_events r "
            " WHERE r.event_type = 'reply' "
            "   AND coalesce(r.parent_event_id, '') != '' "
            "   AND NOT EXISTS (SELECT 1 FROM terminal_events p "
            "                    WHERE p.event_id = r.parent_event_id)",
        )
        or 0
    )


def _with_command_runtime(meta, identity):
    """装好 CommandService + 事件流 store（用完由调用方 reset）。"""
    service = CommandService(identity, repository=CommandRepository(meta.engine))
    store = TerminalEventStore(meta.engine)
    install_terminal_command_service(service)
    install_terminal_event_store(store)
    return service, store


# ------------------------------------------------------------------ ① ② ③ A1-a
def test_command_request_links_event_row_to_command() -> None:
    """①新命令请求 → 事件行 command_id 非空、query 命中、join > 0。"""
    tmp = _make_temp_dir()
    meta = None
    db_path = tmp / "chain.db"
    try:
        meta, db_path = _build(tmp)
        identity = _owner_identity()
        service, store = _with_command_runtime(meta, identity)
        try:
            count = record_message_events(
                [_raw("m_cmd_1", chat_id="owner_raw", sender_id="owner_raw",
                      sender_name="老板", content="帮我查一下明天的日程")],
                platform=W,
                account_id="bot_a",
                identity_service=identity,
                command_recorder=get_terminal_command_recorder(identity),
            )
            assert count == 1
            commands = service.all_commands()
            assert len(commands) == 1, commands
            command_id = commands[0].command_id
            events = store.query(command_id=command_id)
            assert len(events) == 1, [e.event_id for e in events]
            assert events[0].external_msg_id == "m_cmd_1"
            assert events[0].command_id == command_id
            joined = _scalar(
                meta.engine,
                "SELECT COUNT(*) FROM terminal_events e "
                "  JOIN command_records c ON c.command_id = e.command_id",
            )
            assert int(joined or 0) > 0, joined
        finally:
            reset_terminal_command_service()
            reset_terminal_event_store()
    finally:
        _cleanup(meta, db_path)
        shutil.rmtree(tmp, ignore_errors=True)


def test_authorization_backfills_event_row() -> None:
    """②record_authorization → 该命令的事件行 authorization_id 非空。"""
    tmp = _make_temp_dir()
    meta = None
    db_path = tmp / "chain.db"
    try:
        meta, db_path = _build(tmp)
        identity = _owner_identity()
        service, store = _with_command_runtime(meta, identity)
        try:
            record_message_events(
                [_raw("m_auth_1", chat_id="owner_raw", sender_id="owner_raw",
                      sender_name="老板", content="帮我把明天的会改到下午")],
                platform=W,
                account_id="bot_a",
                identity_service=identity,
                command_recorder=get_terminal_command_recorder(identity),
            )
            command_id = service.all_commands()[0].command_id
            record = service.record_authorization(
                command_id,
                final_confirmation="确认",
                authorization_id="auth_1",
            )
            events = store.query(command_id=command_id)
            assert record.authorization_id == "auth_1"
            assert len(events) == 1 and events[0].authorization_id == "auth_1"
            assert store.get(events[0].event_id).authorization_id == "auth_1"
        finally:
            reset_terminal_command_service()
            reset_terminal_event_store()
    finally:
        _cleanup(meta, db_path)
        shutil.rmtree(tmp, ignore_errors=True)


def test_record_execution_dedupes_and_links_execution_events() -> None:
    """③record_execution 去重追加 execution_ids 并回填执行事件行。"""
    tmp = _make_temp_dir()
    meta = None
    db_path = tmp / "chain.db"
    try:
        meta, db_path = _build(tmp)
        identity = _owner_identity()
        service, store = _with_command_runtime(meta, identity)
        try:
            record_message_events(
                [_raw("m_exec_1", chat_id="owner_raw", sender_id="owner_raw",
                      sender_name="老板", content="帮我订明天上午的会议室")],
                platform=W,
                account_id="bot_a",
                identity_service=identity,
                command_recorder=get_terminal_command_recorder(identity),
            )
            command_id = service.all_commands()[0].command_id
            first = record_action_event(
                action="book_room", platform=W, bot_account_id="bot_a",
                chat_id="owner_raw", person_id="owner_raw", store=store,
            )
            second = record_action_event(
                action="notify_group", platform=W, bot_account_id="bot_a",
                chat_id="owner_raw", person_id="owner_raw", store=store,
            )
            updated = service.record_execution(
                command_id,
                execution_event_ids=[first.event_id, second.event_id],
                status=CommandStatus.EXECUTING,
            )
            assert updated.execution_ids == (first.event_id, second.event_id)
            assert updated.status == CommandStatus.EXECUTING
            # 重复登记去重追加（顺序保持，不重复写）
            again = service.record_execution(
                command_id, execution_event_ids=[second.event_id]
            )
            assert again.execution_ids == (first.event_id, second.event_id)
            # 落库可查（execution_ids_json 此前从未被赋值）
            reloaded = CommandService(identity, repository=CommandRepository(meta.engine))
            stored = reloaded.get(command_id)
            assert stored is not None
            assert stored.execution_ids == (first.event_id, second.event_id)
            assert stored.status == CommandStatus.EXECUTING
            # 执行事件行回填 command_id
            assert store.get(first.event_id).command_id == command_id
            assert store.get(second.event_id).command_id == command_id
        finally:
            reset_terminal_command_service()
            reset_terminal_event_store()
    finally:
        _cleanup(meta, db_path)
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------- ④ ⑤ ⑥ A1-c 链
def test_reply_parent_is_event_id_and_turn_chain_joins() -> None:
    """④⑤：父链写事件 id（orphan=0）+ 同回合 turn_id 串起 m⋈t⋈e。"""
    tmp = _make_temp_dir()
    meta = None
    db_path = tmp / "chain.db"
    try:
        meta, db_path = _build(tmp)
        store = TerminalEventStore(meta.engine)
        install_terminal_event_store(store)
        try:
            raw = _raw("m_turn_1", content="明天的天气怎么样")
            events = record_message_events(
                [raw], platform=W, account_id="bot_a", store=store,
                return_events=True,
            )
            inbound = events[0]
            # 采集侧穿透：事件行写入时就带 turn_id，回写到消息行
            assert inbound.turn_id, "消息事件行必须带 turn_id"
            assert raw["turn_id"] == inbound.turn_id
            assert raw["event_id"] == inbound.event_id

            thought = record_thought_event(
                content="先查天气",
                platform=W, bot_account_id="bot_a", chat_id="friend_raw",
                person_id="friend_raw", parent_event_id=inbound.event_id,
                turn_id=inbound.turn_id, store=store,
            )
            reply = record_reply_event(
                content="明天晴",
                platform=W, bot_account_id="bot_a", chat_id="friend_raw",
                person_id="friend_raw", parent_event_id=inbound.event_id,
                parent_msg_id="m_turn_1", turn_id=inbound.turn_id, store=store,
            )
            assert reply.parent_event_id == inbound.event_id
            assert reply.parent_event_id != "m_turn_1"
            assert reply.turn_id == inbound.turn_id == thought.turn_id
            assert store.get(reply.event_id).metadata.get("parent_msg_id") == "m_turn_1"
            assert _orphan_replies(meta.engine) == 0
            chain = _scalar(
                meta.engine,
                "SELECT COUNT(*) FROM terminal_events m "
                "  JOIN terminal_events t ON t.turn_id = m.turn_id AND t.event_type = 'thought' "
                "  JOIN terminal_events e ON e.turn_id = m.turn_id AND e.event_type = 'reply' "
                " WHERE m.event_type IN ('message','question','request','command')",
            )
            assert int(chain or 0) == 1, chain
        finally:
            reset_terminal_event_store()
    finally:
        _cleanup(meta, db_path)
        shutil.rmtree(tmp, ignore_errors=True)


def test_coalesced_turn_uses_main_message_turn_id() -> None:
    """⑥一个回合合并多条消息：thought/reply 用主消息的 turn_id，不丢链。"""
    tmp = _make_temp_dir()
    meta = None
    db_path = tmp / "chain.db"
    try:
        meta, db_path = _build(tmp)
        store = TerminalEventStore(meta.engine)
        install_terminal_event_store(store)
        try:
            older = _raw("m_batch_1", content="在吗")
            main = _raw("m_batch_2", content="帮我看看明天的天气", created_at=_dt(1, 11))
            events = record_message_events(
                [older, main], platform=W, account_id="bot_a", store=store,
                return_events=True,
            )
            # 一条入站消息一个 turn_id
            assert older["turn_id"] and main["turn_id"]
            assert older["turn_id"] != main["turn_id"]
            main_event = events[1]
            assert main_event.turn_id == main["turn_id"]

            # 主消息（本轮实际触发回复的那条）决定整回合的 turn_id
            thought = record_thought_event(
                content="查天气",
                platform=W, bot_account_id="bot_a", chat_id="friend_raw",
                person_id="friend_raw", parent_event_id=main_event.event_id,
                turn_id=main["turn_id"], store=store,
            )
            reply = record_reply_event(
                content="明天晴",
                platform=W, bot_account_id="bot_a", chat_id="friend_raw",
                person_id="friend_raw", parent_event_id=main_event.event_id,
                parent_msg_id="m_batch_2", turn_id=main["turn_id"], store=store,
            )
            assert thought.turn_id == reply.turn_id == main_event.turn_id
            same_turn = _scalar(
                meta.engine,
                "SELECT COUNT(*) FROM terminal_events WHERE turn_id = :turn",
                turn=main["turn_id"],
            )
            assert int(same_turn or 0) == 3, same_turn  # message + thought + reply
            # 同批的另一条消息保持自己的 turn_id，不混进本回合
            other = store.get(events[0].event_id)
            assert other.turn_id == older["turn_id"]
            # 合并回合不丢链：主消息 → 回复仍是事件 id
            assert reply.parent_event_id == main_event.event_id
            assert _orphan_replies(meta.engine) == 0
        finally:
            reset_terminal_event_store()
    finally:
        _cleanup(meta, db_path)
        shutil.rmtree(tmp, ignore_errors=True)


def test_reply_parent_resolver_prefers_event_id_then_store_lookup() -> None:
    """④生产写点：载荷带 event_id 直接用；旧载荷查库；解不了给空（绝不回退 msg_id）。"""
    import core.infra.logging as _logging

    _logging.init_logging = lambda *args, **kwargs: None
    import scheduler.main as sm

    tmp = _make_temp_dir()
    meta = None
    db_path = tmp / "chain.db"
    try:
        meta, db_path = _build(tmp)
        store = TerminalEventStore(meta.engine)
        install_terminal_event_store(store)
        try:
            raw = _raw("m_res_1")
            events = record_message_events(
                [raw], platform=W, account_id="bot_a", store=store,
                return_events=True,
            )
            assert raw["event_id"] == events[0].event_id
            ok1 = (
                sm._resolve_parent_event_id(
                    {"event_id": "evt_x"},
                    platform=W, bot_account_id="bot_a", chat_id="friend_raw",
                )
                == "evt_x"
            )
            ok2 = (
                sm._resolve_parent_event_id(
                    {"msg_id": "m_res_1"},
                    platform=W, bot_account_id="bot_a", chat_id="friend_raw",
                )
                == events[0].event_id
            )
            ok3 = (
                sm._resolve_parent_event_id(
                    {"msg_id": "m_unknown"},
                    platform=W, bot_account_id="bot_a", chat_id="friend_raw",
                )
                == ""
            )
            ok4 = (
                sm._resolve_parent_event_id(
                    {}, platform=W, bot_account_id="bot_a", chat_id="friend_raw"
                )
                == ""
            )
            assert ok1 and ok2 and ok3 and ok4, (ok1, ok2, ok3, ok4)
        finally:
            reset_terminal_event_store()
    finally:
        _cleanup(meta, db_path)
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------- ⑦ 读取侧兼容
def _window_row(
    *,
    row_id: int,
    event_id: str,
    event_type: str,
    actor: str,
    content: str,
    parent_event_id: str = "",
    msg_id: str = "",
    parent_msg_id: str = "",
    recipients: tuple[str, ...] = (),
) -> dict:
    metadata: dict = {"is_self": 1 if event_type == "reply" else 0}
    if msg_id:
        metadata["msg_id"] = msg_id
    if parent_msg_id:
        metadata["parent_msg_id"] = parent_msg_id
    return {
        "id": row_id,
        "event_id": event_id,
        "occurred_at": _dt(1, 10) ,
        "event_type": event_type,
        "actor_person_id": actor,
        "content": content,
        "parent_event_id": parent_event_id,
        "recipient_person_ids_json": json.dumps(list(recipients)),
        "metadata_json": json.dumps(metadata, ensure_ascii=False),
    }


def test_chat_window_resolves_event_id_and_legacy_parent() -> None:
    """⑦新写法（事件 id + metadata.parent_msg_id）与旧写法（平台 msg_id）都能解。"""
    request_id = "a" * 32
    request = _window_row(
        row_id=1, event_id=request_id, event_type="request", actor="friend_raw",
        content="帮我查天气", msg_id="m_req",
    )
    new_reply = _window_row(
        row_id=2, event_id="b" * 32, event_type="reply", actor="bot_a",
        content="@朋友 明天晴", parent_event_id=request_id, msg_id="m_rep_1",
        parent_msg_id="m_req", recipients=("friend_raw",),
    )
    # Migration 50 回填过的存量行：只有事件 id，没有 metadata.parent_msg_id
    migrated_reply = _window_row(
        row_id=3, event_id="c" * 32, event_type="reply", actor="bot_a",
        content="@朋友 明天晴", parent_event_id=request_id, msg_id="m_rep_2",
        recipients=("friend_raw",),
    )
    # 迁移之前的 legacy 行：parent_event_id 里就是平台 msg_id
    legacy_reply = _window_row(
        row_id=4, event_id="d" * 32, event_type="reply", actor="bot_a",
        content="@朋友 明天晴", parent_event_id="m_req", msg_id="m_rep_3",
        recipients=("friend_raw",),
    )
    msgs = messages_from_rows([request, new_reply, migrated_reply, legacy_reply])
    assert [m.parent_msg_id for m in msgs[1:]] == ["m_req", "m_req", "m_req"]
    links = derive_answer_links(msgs)
    assert {link.reply_event_id for link in links} == {"b" * 32, "c" * 32, "d" * 32}
    assert all(link.request_event_id == request_id for link in links)

    # 事件 id 但父事件不在窗口（类型/时间被过滤）→ 不硬连，也不当成 msg_id
    orphan = _window_row(
        row_id=5, event_id="e" * 32, event_type="reply", actor="bot_a",
        content="@朋友 在的", parent_event_id="f" * 32, msg_id="m_rep_4",
    )
    orphan_msgs = messages_from_rows([request, orphan])
    assert orphan_msgs[1].parent_msg_id == ""
    assert derive_answer_links(orphan_msgs) == []


# ------------------------------------------------------------- ⑧ Migration 50
def _legacy_db(tmp: Path):
    """老库（Migration 50 之前）：46 已加 external_msg_id，但唯一索引被降级过。

    手工建表而不是跑整条链，才能造出「同四元组两条同 msg_id」的历史脏数据
    （唯一索引没降级时插不进去），从而验证迁移对**有歧义**的行不动手。
    """
    db_path = tmp / "legacy.db"
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        _m033(conn, None)  # terminal_events（无 turn_id / external_msg_id）
        _m034(conn, None)  # command_records
        conn.execute(text(
            "ALTER TABLE terminal_events "
            "ADD COLUMN external_msg_id TEXT DEFAULT ''"  # _m046 的实列
        ))
    return engine, db_path


def _insert_event(conn, **values) -> None:
    cols = ", ".join(values)
    marks = ", ".join(f":{key}" for key in values)
    conn.execute(text(f"INSERT INTO terminal_events ({cols}) VALUES ({marks})"), values)


def _snapshot(conn) -> list[tuple]:
    return [
        tuple(str(value) for value in row)
        for row in conn.execute(
            text(
                "SELECT event_id, parent_event_id, command_id, turn_id "
                "  FROM terminal_events ORDER BY event_id"
            )
        ).fetchall()
    ]


def test_migration_50_backfills_and_is_idempotent() -> None:
    """⑧迁移回填 reply 父链 + 命令链接；解不了/有歧义的不动；可重跑。"""
    tmp = _make_temp_dir()
    engine = None
    db_path = tmp / "legacy.db"
    try:
        engine, db_path = _legacy_db(tmp)
        with engine.begin() as conn:
            # 入站消息（实列 external_msg_id 已在：_m046 之后的行）
            _insert_event(
                conn,
                event_id="evt_in1", occurred_at="2026-01-01 10:00:00", sequence=1,
                platform=W, bot_account_id="bot_a", chat_id="c1",
                event_type="message", content="帮我查天气",
                external_msg_id="m_in1", metadata_json=json.dumps({"msg_id": "m_in1"}),
            )
            # 入站消息（pre-46：external_msg_id 空，只能从 metadata 解）
            _insert_event(
                conn,
                event_id="evt_in2", occurred_at="2026-01-01 10:01:00", sequence=2,
                platform=W, bot_account_id="bot_a", chat_id="c1",
                event_type="message", content="明天呢",
                external_msg_id="", metadata_json=json.dumps({"msg_id": "m_in2"}),
            )
            # 两条 reply：parent 里写的是平台 msg_id（A1-c 之前的写法）
            for event_id, seq, parent in (
                ("evt_reply1", 3, "m_in1"),
                ("evt_reply2", 4, "m_in2"),
            ):
                _insert_event(
                    conn,
                    event_id=event_id, occurred_at=f"2026-01-01 10:0{seq}:00",
                    sequence=seq, platform=W, bot_account_id="bot_a", chat_id="c1",
                    event_type="reply", content="明天晴", parent_event_id=parent,
                    command_id="", metadata_json="{}",
                )
            # 命令投影：metadata.msg_id 与事件四元组对齐
            conn.execute(
                text(
                    "INSERT INTO command_records (command_id, created_at, "
                    "origin_platform, origin_bot_account_id, origin_chat_id, "
                    "metadata_json) VALUES ('cmd_1', '2026-01-01 10:00:00', "
                    ":p, 'bot_a', 'c1', :meta)"
                ),
                {"p": W, "meta": json.dumps({"msg_id": "m_in1"})},
            )

            _m050(conn, None)  # 跑迁移（老库：补列 + 建索引 + 回填）
            parents = dict(
                conn.execute(
                    text("SELECT event_id, parent_event_id FROM terminal_events")
                ).fetchall()
            )
            assert parents["evt_reply1"] == "evt_in1", parents
            assert parents["evt_reply2"] == "evt_in2", parents
            commands = dict(
                conn.execute(
                    text("SELECT event_id, command_id FROM terminal_events")
                ).fetchall()
            )
            assert commands["evt_in1"] == "cmd_1", commands
            assert commands["evt_reply1"] == "", commands
            orphan = conn.execute(
                text(
                    "SELECT COUNT(*) FROM terminal_events r "
                    " WHERE r.event_type = 'reply' "
                    "   AND coalesce(r.parent_event_id, '') != '' "
                    "   AND NOT EXISTS (SELECT 1 FROM terminal_events p "
                    "                    WHERE p.event_id = r.parent_event_id)"
                )
            ).scalar()
            assert int(orphan or 0) == 0, orphan
            cols = {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(terminal_events)"))
            }
            assert "turn_id" in cols, cols
            indexes = {
                row[1]
                for row in conn.execute(text("PRAGMA index_list(terminal_events)"))
            }
            assert "ix_terminal_events_turn" in indexes, indexes

            # 解不了的行 + 有歧义的行（同四元组两条同 msg_id）：保持原值
            _insert_event(
                conn,
                event_id="evt_reply_lost", occurred_at="2026-01-01 10:05:00",
                sequence=5, platform=W, bot_account_id="bot_a", chat_id="c1",
                event_type="reply", content="接不上", parent_event_id="m_missing",
                metadata_json="{}",
            )
            for seq in (6, 7):
                _insert_event(
                    conn,
                    event_id=f"evt_dup{seq}", occurred_at=f"2026-01-01 10:0{seq}:00",
                    sequence=seq, platform=W, bot_account_id="bot_a", chat_id="c1",
                    event_type="message", content="重放", external_msg_id="m_dup",
                    metadata_json=json.dumps({"msg_id": "m_dup"}),
                )
            _insert_event(
                conn,
                event_id="evt_reply_dup", occurred_at="2026-01-01 10:08:00",
                sequence=8, platform=W, bot_account_id="bot_a", chat_id="c1",
                event_type="reply", content="有歧义", parent_event_id="m_dup",
                metadata_json="{}",
            )
            _m050(conn, None)
            after_second = dict(
                conn.execute(
                    text("SELECT event_id, parent_event_id FROM terminal_events")
                ).fetchall()
            )
            # 已回填过的行再跑一次不许被改写（否则第二次会把父链清成 NULL）
            assert after_second["evt_reply1"] == "evt_in1", after_second
            assert after_second["evt_reply2"] == "evt_in2", after_second
            assert after_second["evt_reply_lost"] == "m_missing", after_second
            assert after_second["evt_reply_dup"] == "m_dup", after_second
            # 只剩故意解不了的 2 行是 orphan
            orphan_second = conn.execute(
                text(
                    "SELECT COUNT(*) FROM terminal_events r "
                    " WHERE r.event_type = 'reply' "
                    "   AND coalesce(r.parent_event_id, '') != '' "
                    "   AND NOT EXISTS (SELECT 1 FROM terminal_events p "
                    "                    WHERE p.event_id = r.parent_event_id)"
                )
            ).scalar()
            assert int(orphan_second or 0) == 2, orphan_second

            # 再跑一次：数值不变、无重复改写（total_changes 证明一行都没被碰）
            before = _snapshot(conn)
            rows_before = conn.execute(
                text("SELECT COUNT(*) FROM terminal_events")
            ).scalar()
            changes_before = conn.execute(text("SELECT total_changes()")).scalar()
            _m050(conn, None)
            changes_after = conn.execute(text("SELECT total_changes()")).scalar()
            assert changes_after == changes_before, (changes_before, changes_after)
            assert _snapshot(conn) == before
            assert (
                conn.execute(text("SELECT COUNT(*) FROM terminal_events")).scalar()
                == rows_before
            )
    finally:
        if engine is not None:
            engine.dispose()
        shutil.rmtree(tmp, ignore_errors=True)


def test_migration_50_registered_in_full_chain() -> None:
    """⑧迁移注册：整条链把 50 落到 49 之后的库上；meta-only 链不得越过 39。"""
    from db.migrations import _META_MIGRATION_VERSIONS

    # meta-only 链若先盖 50，随后的 run_migrations 会跳过 46-49（external_msg_id 等）。
    assert max(_META_MIGRATION_VERSIONS) < 40, _META_MIGRATION_VERSIONS

    tmp = _make_temp_dir()
    engine = None
    db_path = tmp / "fresh.db"
    try:
        run_migrations(str(db_path))
        engine = create_engine(f"sqlite:///{db_path}")
        with engine.connect() as conn:
            # 不锁死最新迁移号（与 tests/test_reminder_delivery.py 同一写法）：
            # 新增迁移只在「链把 50 落在 49 之后」这件事上加版本，不改这条不变式。
            assert conn.execute(
                text("SELECT MAX(version) FROM _schema_version")
            ).scalar() >= 51
            cols = {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(terminal_events)"))
            }
            assert "turn_id" in cols, cols
            indexes = {
                row[1]
                for row in conn.execute(text("PRAGMA index_list(terminal_events)"))
            }
            assert "ix_terminal_events_turn" in indexes, indexes
            # 46 的实列与去重索引在同一条链上先于 50 落地
            assert "external_msg_id" in cols, cols
            assert "ux_terminal_events_external_msg" in indexes, indexes
    finally:
        if engine is not None:
            engine.dispose()
        shutil.rmtree(tmp, ignore_errors=True)


def test_authorization_link_does_not_bootstrap() -> None:
    """A1-a 回归：链接回填只读命令投影的 meta engine，不得触发 init_singletons。

    2026-09-29 事故：``record_authorization`` 用 ``get_terminal_event_store(force=True)``
    找事件行，那个访问器缓存为空时会走 ``core.bootstrap.init_singletons()``——于是
    ``tests/test_command_service_db.py`` 里只调授权的 roundtrip 用例会把整个应用拉起来，
    并把 pending 迁移（Migration 50）落到**真实** meta/account 库上。
    """
    from unittest.mock import patch

    tmp = _make_temp_dir()
    meta = None
    db_path = tmp / "chain.db"
    try:
        meta, db_path = _build(tmp)
        service = CommandService(
            _owner_identity(), repository=CommandRepository(meta.engine)
        )
        command = service.record_command(
            issuer_person_id="p_owner",
            issuer_account_id="owner_raw",
            issuer_role="master",
            origin_platform=W,
            origin_bot_account_id="bot_a",
            origin_chat_id="owner_raw",
            origin_chat_type="private",
            raw_request="帮我查日程",
        )
        with patch("core.bootstrap.init_singletons") as init:
            init.return_value = {}
            record = service.record_authorization(
                command.command_id, final_confirmation="确认", authorization_id="auth_nb"
            )
        assert record.authorization_id == "auth_nb"
        assert init.call_count == 0, (
            "record_authorization 触发了 core.bootstrap.init_singletons："
            "真实 meta/account 库会被迁移（2026-09-29 事故）"
        )
    finally:
        _cleanup(meta, db_path)
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------- ⑨ 哑 store 守卫
class _DumbStore:
    """无 ``link_event`` / ``query`` 的旧替身（tests/test_terminal_external_msg_id.py 风格）。"""

    def __init__(self) -> None:
        self.events: list = []

    def append(self, event):
        self.events.append(event)
        return event

    def all(self):
        return tuple(self.events)

    def count(self) -> int:
        return len(self.events)


class _Recorder:
    def __init__(self, command) -> None:
        self.command = command
        self.calls = 0

    def record_from_message(self, raw, **kwargs):
        self.calls += 1
        return self.command


def test_dumb_store_without_link_event_does_not_raise() -> None:
    """⑨哑 store（没有 link_event）照常入库、不炸，turn_id 仍写在事件上。"""
    command = CommandRecord(
        command_id="cmd_dumb",
        created_at=_dt(),
        issuer_person_id="p_owner",
        issuer_account_id="owner_raw",
        issuer_role="master",
        origin_platform=W,
        origin_bot_account_id="bot_a",
        origin_chat_id="owner_raw",
        origin_chat_type="private",
        raw_request="帮我查日程",
    )
    store = _DumbStore()
    recorder = _Recorder(command)
    count = record_message_events(
        [_raw("m_dumb_1", chat_id="owner_raw", sender_id="owner_raw",
              content="帮我查日程")],
        platform=W,
        account_id="bot_a",
        store=store,
        command_recorder=recorder,
    )
    assert count == 1
    assert recorder.calls == 1
    event = store.all()[0]
    assert event.event_id and event.turn_id
    assert event.command_id == ""  # 哑 store 不支持窄更新 → 保持 append 时的值


# --------------------------------------------------------------------- ⑪ 重放
def _message_event(external_msg_id: str, *, event_id: str = "") -> Event:
    return Event(
        event_id=event_id,
        occurred_at=_dt(1, 10),
        platform=W,
        bot_account_id="bot_a",
        chat_id="friend_raw",
        chat_type="private",
        actor_person_id="p_friend",
        event_type=EventType.MESSAGE,
        content="在吗",
        external_msg_id=external_msg_id,
    )


def test_replayed_external_message_inserts_one_row_and_keeps_sequence() -> None:
    """⑪同一四元组重放 → 只插一行、拿到同一个 sequence（两条写入路径都幂等）。

    G2-1 之后事件行有两个写入口：owner 的 ``append``（自开 session + commit）与
    ``append_in_session``（调用方 session，台账路径用）。**幂等必须两条都成立**：
    只在一条上成立 = 另一条把同一条外部消息写第二遍（重放 / 重试 / 双进程都能触发）。
    """
    tmp = _make_temp_dir()
    meta = None
    db_path = tmp / "chain.db"
    try:
        meta, db_path = _build(tmp)
        store = TerminalEventStore(meta.engine)
        total_sql = "SELECT COUNT(*) FROM terminal_events"

        first = _message_event("m_replay_1")
        store.append(first)
        assert first.sequence == 1, first.sequence

        # ① append 重放：换一个 event_id、同一个四元组 → 返回**已存行**（同 sequence）。
        stored = store.append(_message_event("m_replay_1"))
        assert stored.event_id == first.event_id, (stored.event_id, first.event_id)
        assert stored.sequence == first.sequence == 1, stored.sequence
        assert _scalar(meta.engine, total_sql) == 1

        # ② append_in_session 重放（库里已有行）：同样命中已存行，不插第二行。
        with Session(meta.engine) as session:
            stored2 = append_in_session(session, _message_event("m_replay_1"))
            session.commit()
        assert stored2.event_id == first.event_id, stored2.event_id
        assert stored2.sequence == 1, stored2.sequence
        assert _scalar(meta.engine, total_sql) == 1

        # ③ 同一 session 内连投两次：第一行还没 commit，第二次也要在**同事务里**回查到。
        with Session(meta.engine) as session:
            mine = _message_event("m_replay_2")
            append_in_session(session, mine)
            again = append_in_session(session, _message_event("m_replay_2"))
            session.commit()
        assert mine.sequence == 2 and again.sequence == 2, (mine.sequence, again.sequence)
        assert again.event_id == mine.event_id, (again.event_id, mine.event_id)
        assert _scalar(meta.engine, total_sql) == 2

        # ④ 反向对照：不同 external_msg_id 必须真的插新行、拿新号
        #    （否则本用例会在"什么都没插"时也绿 = 空断言）。
        fresh = _message_event("m_replay_3")
        store.append(fresh)
        assert fresh.sequence == 3, fresh.sequence
        assert _scalar(meta.engine, total_sql) == 3
        assert _scalar(
            meta.engine,
            "SELECT COUNT(*) FROM terminal_events "
            " WHERE external_msg_id = 'm_replay_1'",
        ) == 1
    finally:
        _cleanup(meta, db_path)
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------- ⑩ 事件行 INSERT 唯一性守卫
#: 事件流的写入 owner：``TerminalEventStore``——``append``（自开 session）与它公开的
#: 同事务入口 ``append_in_session(session, event)``，两者共用同一份 INSERT + 库内原子
#: 取号 + 三道保护。**全仓唯一**：同事务的台账路径（``core/tasks/store._insert_event``）
#: 改调 ``append_in_session`` 之后，白名单收缩到这一个文件（G2-1）。
_EVENT_STORE_OWNER = "core/terminal/event_store_db.py"
#: 允许出现 `insert(TerminalEvent)` 的文件（其余一律红）。**只有一个**。
_EVENT_INSERT_ALLOWED = frozenset({_EVENT_STORE_OWNER})
#: 旧「配方别名」：``event_row_values`` / ``next_sequence_expression`` 曾把字段映射与
#: 取号表达式外借给第二个写方（抄一份 = 等着漂移）；G2-1 后已删——外部一律走
#: ``append_in_session``，所以这两个名字**不许再出现在 owner 之外**。
_EVENT_INSERT_RECIPE_NAMES = frozenset({
    "event_row_values",
    "next_sequence_expression",
})
#: 扫描根：产品代码。tests/ 不在内——夹具要造「存量畸形行」（重号/空号/原始 SQL）
#: 才能测取号与迁移，那正是测试存在的理由。
_EVENT_INSERT_ROOTS = ("core", "db", "agents", "scripts")
_EVENT_INSERT_SKIP_DIRS = frozenset({"__pycache__", "build", "site-packages", "_scratch"})


def _event_insert_sites() -> list[tuple[str, int]]:
    """产品代码里直接 INSERT ``terminal_events`` 的位置（AST + 原始 SQL 两种写法）。"""
    found: list[tuple[str, int]] = []
    for root in _EVENT_INSERT_ROOTS:
        for path in sorted((ROOT / root).rglob("*.py")):
            if _EVENT_INSERT_SKIP_DIRS & set(path.parts):
                continue
            rel = path.relative_to(ROOT).as_posix()
            source = path.read_text(encoding="utf-8-sig")
            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                # ① ORM：insert(TerminalEvent)
                if isinstance(node, ast.Call):
                    func = node.func
                    name = (
                        func.id if isinstance(func, ast.Name)
                        else func.attr if isinstance(func, ast.Attribute)
                        else ""
                    )
                    if name == "insert" and node.args:
                        target = node.args[0]
                        if isinstance(target, ast.Name) and target.id == "TerminalEvent":
                            found.append((rel, node.lineno))
                # ② 原始 SQL：exec_driver_sql / text("INSERT INTO terminal_events …")
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if "INSERT INTO terminal_events" in node.value.upper():
                        found.append((rel, node.lineno))
    return found


def _event_insert_recipe_users() -> list[tuple[str, int, str]]:
    """产品代码里还在引用旧「配方别名」的位置（文件、行号、名字）。"""
    found: list[tuple[str, int, str]] = []
    for root in _EVENT_INSERT_ROOTS:
        for path in sorted((ROOT / root).rglob("*.py")):
            if _EVENT_INSERT_SKIP_DIRS & set(path.parts):
                continue
            rel = path.relative_to(ROOT).as_posix()
            source = path.read_text(encoding="utf-8-sig")
            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                name = (
                    node.id if isinstance(node, ast.Name)
                    else node.attr if isinstance(node, ast.Attribute)
                    else ""
                )
                if name in _EVENT_INSERT_RECIPE_NAMES:
                    found.append((rel, node.lineno, name))
    return found


def test_event_rows_are_inserted_only_by_the_store_owner() -> None:
    """⑩事件行 INSERT **全仓只许 owner** 写；别处自建即红（白名单已收缩到 1 个文件）。"""
    sites = _event_insert_sites()
    found_files = {rel for rel, _ in sites}
    assert found_files == _EVENT_INSERT_ALLOWED, (
        "事件行 INSERT 只能出现在 owner "
        f"{sorted(_EVENT_INSERT_ALLOWED)}；别处自建会绕过取号/幂等/查重，"
        f"实测：{sorted(sites)}"
    )
    recipe_users = [
        item for item in _event_insert_recipe_users()
        if item[0] != _EVENT_STORE_OWNER
    ]
    assert not recipe_users, (
        "旧「配方别名」已随 G2-1 删除，外部一律走 owner 的 append_in_session："
        f"{recipe_users}"
    )


# --------------------------------------------------------------------- runner
def main() -> int:
    tests = [
        test_command_request_links_event_row_to_command,
        test_authorization_backfills_event_row,
        test_record_execution_dedupes_and_links_execution_events,
        test_reply_parent_is_event_id_and_turn_chain_joins,
        test_coalesced_turn_uses_main_message_turn_id,
        test_reply_parent_resolver_prefers_event_id_then_store_lookup,
        test_chat_window_resolves_event_id_and_legacy_parent,
        test_migration_50_backfills_and_is_idempotent,
        test_migration_50_registered_in_full_chain,
        test_authorization_link_does_not_bootstrap,
        test_dumb_store_without_link_event_does_not_raise,
        test_replayed_external_message_inserts_one_row_and_keeps_sequence,
        test_event_rows_are_inserted_only_by_the_store_owner,
    ]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback

            print(f"FAIL {test.__name__}: {exc!r}")
            traceback.print_exc()
    print(f"{len(tests) - failed}/{len(tests)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
