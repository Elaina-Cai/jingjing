"""terminal_events.external_msg_id：实列 + 回填 + 幂等写路径（任务模型 A3 ②）。

对应 docs/任务模型.md §7.1 跨库写一致性第一步：
- Migration 46：external_msg_id 实列 + 非空偏唯一索引 + 历史 JSON 回填；
- 写路径：record_message_events / TerminalEventStore.append 按
  (platform, bot_account_id, chat_id, external_msg_id) 幂等；
- 失败可见：单条失败不吞整批，也不阻塞正常消息。

Run directly:
    python tests/test_terminal_external_msg_id.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ROOT = Path(__file__).resolve().parent.parent
_TMP_ROOT = ROOT / "data" / "_terminal_external_msg_id_test"
W = "wechat"


def _make_temp_dir() -> Path:
    _TMP_ROOT.mkdir(parents=True, exist_ok=True)
    path = _TMP_ROOT / f"case_{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=False)
    return path


def _dt(hour: int = 12) -> datetime:
    return datetime(2026, 9, 28, hour, 0, 0)


def _raw_message(msg_id: str, chat_id: str = "friend_raw") -> dict:
    return {
        "msg_id": msg_id,
        "chat_id": chat_id,
        "chat_type": "private",
        "sender_id": chat_id,
        "sender_name": "朋友",
        "content": "在吗",
        "created_at": _dt().isoformat(),
        "source": W,
    }


def _build(tmp: Path):
    from db.meta_repo import MetaRepositoryManager
    from db.migrations import run_migrations

    db_path = tmp / "external_msg_id.db"
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


class _CountingRecorder:
    def __init__(self) -> None:
        self.calls = 0

    def record_from_message(self, raw, **kwargs) -> None:
        self.calls += 1


class _FlakyStore:
    """append 对指定 external_msg_id 抛错，其余照记（测单条失败不吞批）。"""

    def __init__(self, fail_key: str) -> None:
        self.fail_key = fail_key
        self.events = []

    def append(self, event):
        if event.external_msg_id == self.fail_key:
            raise RuntimeError("simulated append failure")
        self.events.append(event)
        return event

    def all(self):
        return tuple(self.events)

    def count(self) -> int:
        return len(self.events)


def test_migration_46_adds_column_backfills_and_indexes() -> None:
    """老表跑迁移：补实列、回填 JSON、建唯一偏索引、可重跑、重复键被拒。"""
    import sqlite3

    from sqlalchemy import create_engine, text

    from db.migrations import _m046, run_migrations

    tmp = _make_temp_dir()
    db_path = tmp / "m46.db"
    engine = None
    con = None
    try:
        # 裸库全量迁移：_m033 先建老 schema 表，_m046 再升级（真实老库路径）
        run_migrations(str(db_path))
        engine = create_engine(f"sqlite:///{db_path}")
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO terminal_events (
                        event_id, occurred_at, sequence, platform, bot_account_id,
                        chat_id, chat_type, actor_person_id, actor_account_id,
                        event_type, content, metadata_json
                    ) VALUES (
                        'legacy_evt', :ts, 1, 'wechat', 'bot_a',
                        'friend_raw', 'private', 'friend_raw', 'friend_raw',
                        'message', '在吗', :meta
                    )
                    """
                ),
                {
                    "ts": _dt(),
                    "meta": json.dumps({"msg_id": "legacy_m1"}, ensure_ascii=False),
                },
            )
            # 等价于"46 之前落的历史行"：实列为空、JSON 里有 msg_id
            _m046(conn, None)
            row = conn.execute(
                text(
                    "SELECT external_msg_id FROM terminal_events "
                    "WHERE event_id='legacy_evt'"
                )
            ).fetchone()
            assert row is not None and row[0] == "legacy_m1", row
            _m046(conn, None)  # 幂等重跑
            count = conn.execute(
                text("SELECT COUNT(*) FROM terminal_events WHERE event_id='legacy_evt'")
            ).scalar()
            assert count == 1
        engine.dispose()
        engine = None

        con = sqlite3.connect(str(db_path))
        names = {r[1] for r in con.execute("PRAGMA index_list(terminal_events)")}
        assert "ux_terminal_events_external_msg" in names, names
        try:
            con.execute(
                "INSERT INTO terminal_events (event_id, occurred_at, sequence, "
                "platform, bot_account_id, chat_id, event_type, content, "
                "external_msg_id, metadata_json) VALUES "
                "('dup_evt', '2026-09-28 12:00:00', 2, 'wechat', 'bot_a', "
                "'friend_raw', 'message', '在吗', 'legacy_m1', '{}')"
            )
            raise AssertionError("重复 external_msg_id 应该被唯一索引拒绝")
        except sqlite3.IntegrityError:
            con.rollback()
    finally:
        if con is not None:
            con.close()
        if engine is not None:
            engine.dispose()
        shutil.rmtree(tmp, ignore_errors=True)


def test_record_message_events_is_idempotent() -> None:
    """同一条消息重放：事件不重复、命令不重记、返回同一个 event_id。"""
    from core.terminal.event_store_db import TerminalEventStore
    from core.terminal.shadow import record_message_events

    tmp = _make_temp_dir()
    meta = None
    db_path = None
    try:
        meta, db_path = _build(tmp)
        store = TerminalEventStore(meta.engine)
        recorder = _CountingRecorder()

        first = record_message_events(
            [_raw_message("m_idem")],
            platform=W,
            account_id="bot_a",
            store=store,
            command_recorder=recorder,
            return_events=True,
        )
        assert len(first) == 1 and store.count() == 1
        assert first[0].external_msg_id == "m_idem"
        assert recorder.calls == 1

        second = record_message_events(
            [_raw_message("m_idem")],
            platform=W,
            account_id="bot_a",
            store=store,
            command_recorder=recorder,
            return_events=True,
        )
        assert len(second) == 1 and store.count() == 1
        assert second[0].event_id == first[0].event_id
        assert recorder.calls == 1

        looked_up = store.get_by_external_msg_id(
            platform=W,
            bot_account_id="bot_a",
            chat_id="friend_raw",
            external_msg_id="m_idem",
        )
        assert looked_up is not None and looked_up.event_id == first[0].event_id

        # 没带 msg_id 的消息不参与幂等（保持旧行为：照常追加）
        count = record_message_events(
            [_raw_message("")], platform=W, account_id="bot_a", store=store
        )
        assert count == 1 and store.count() == 2
    finally:
        if meta is not None and db_path is not None:
            _cleanup(meta, db_path)
        shutil.rmtree(tmp, ignore_errors=True)


def test_store_append_returns_stored_winner_for_same_external_key() -> None:
    """store.append 直接撞 external_msg_id：返回库里那行，不新增。"""
    from core.terminal.event_model import Event, EventType
    from core.terminal.event_store_db import TerminalEventStore

    tmp = _make_temp_dir()
    meta = None
    db_path = None
    try:
        meta, db_path = _build(tmp)
        store = TerminalEventStore(meta.engine)
        first = store.append(Event.new(
            event_type=EventType.MESSAGE,
            occurred_at=_dt(),
            platform=W,
            bot_account_id="bot_a",
            chat_id="friend_raw",
            chat_type="private",
            content="在吗",
            external_msg_id="m_dup",
        ))
        second = store.append(Event.new(
            event_type=EventType.MESSAGE,
            occurred_at=_dt(13),
            platform=W,
            bot_account_id="bot_a",
            chat_id="friend_raw",
            chat_type="private",
            content="在吗（重放）",
            external_msg_id="m_dup",
        ))
        assert store.count() == 1
        assert second.event_id == first.event_id
        assert second.content == "在吗"
    finally:
        if meta is not None and db_path is not None:
            _cleanup(meta, db_path)
        shutil.rmtree(tmp, ignore_errors=True)


def test_bad_message_does_not_kill_the_batch() -> None:
    """单条写失败只丢那一条（现在会 log），后面的消息照记。"""
    from core.terminal.shadow import record_message_events

    store = _FlakyStore("bad_1")
    count = record_message_events(
        [_raw_message("bad_1"), _raw_message("good_1")],
        platform=W,
        account_id="bot_a",
        store=store,
    )
    assert count == 1, count
    assert [e.external_msg_id for e in store.all()] == ["good_1"]


def test_event_roundtrip_keeps_external_msg_id() -> None:
    from core.terminal.event_model import Event, EventType

    event = Event.new(
        event_type=EventType.MESSAGE,
        occurred_at=_dt(),
        platform=W,
        bot_account_id="bot_a",
        chat_id="friend_raw",
        content="在吗",
        external_msg_id="m_round",
        turn_id="t_round",
    )
    data = event.as_dict()
    assert data["external_msg_id"] == "m_round"
    assert data["turn_id"] == "t_round"
    again = Event.from_dict(data)
    assert again.external_msg_id == "m_round"
    assert again.turn_id == "t_round"


def main() -> int:
    tests = [
        test_migration_46_adds_column_backfills_and_indexes,
        test_record_message_events_is_idempotent,
        test_store_append_returns_stored_winner_for_same_external_key,
        test_bad_message_does_not_kill_the_batch,
        test_event_roundtrip_keeps_external_msg_id,
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
    raise SystemExit(main())
