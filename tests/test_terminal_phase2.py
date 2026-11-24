"""Phase 2 event-stream persistence tests.

Run directly:
    python tests/test_terminal_phase2.py
"""

from __future__ import annotations

import os
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ROOT = Path(__file__).resolve().parent.parent
_TEST_TMP_ROOT = ROOT / "data" / "_terminal_phase2_test"
UTC = timezone.utc
W = "wechat"


def _make_temp_dir() -> Path:
    _TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)
    path = _TEST_TMP_ROOT / f"case_{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=False)
    return path


from core.terminal.event_model import Event, EventType, Sensitivity  # noqa: E402
from core.terminal.event_runtime import (  # noqa: E402
    get_terminal_event_store,
    install_terminal_event_store,
    reset_terminal_event_store,
)
from core.terminal.event_store_db import TerminalEventStore  # noqa: E402
from core.terminal.principal import (  # noqa: E402
    Capability,
    ChatType,
    PrincipalContext,
    RelationLevel,
    VisibilityScope,
)
from core.terminal.shadow import (  # noqa: E402
    record_action_event,
    record_message_events,
    record_reply_event,
    record_thought_event,
)


def _dt(day: int = 1, hour: int = 12) -> datetime:
    return datetime(2026, 1, day, hour, 0, 0, tzinfo=UTC)


def _build_store(tmp: Path):
    from db.meta_repo import MetaRepositoryManager
    from db.migrations import run_migrations

    db_path = tmp / "phase2_meta.db"
    meta = MetaRepositoryManager(str(db_path))
    run_migrations(str(db_path))
    store = TerminalEventStore(meta.engine)
    return meta, store, db_path


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


def test_migration_33_creates_terminal_events() -> None:
    import sqlite3

    tmp = _make_temp_dir()
    meta = None
    db_path = tmp / "phase2_meta.db"
    try:
        meta, store, db_path = _build_store(tmp)
        conn = sqlite3.connect(str(db_path))
        try:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            cols = {row[1] for row in conn.execute("PRAGMA table_info(terminal_events)")}
            version = conn.execute("SELECT MAX(version) FROM _schema_version").fetchone()[0]
        finally:
            conn.close()
        assert "terminal_events" in tables
        assert version >= 33
        for col in (
            "event_id", "occurred_at", "sequence", "platform", "bot_account_id",
            "chat_id", "actor_person_id", "event_type", "visibility_scope_json",
            "sensitivity", "metadata_json",
        ):
            assert col in cols, col
    finally:
        _cleanup(meta, db_path)
        shutil.rmtree(tmp, ignore_errors=True)


def test_append_sequence_and_roundtrip() -> None:
    tmp = _make_temp_dir()
    meta = None
    db_path = tmp / "phase2_meta.db"
    try:
        meta, store, db_path = _build_store(tmp)
        first = Event.new(
            event_type=EventType.MESSAGE,
            occurred_at=_dt(1),
            platform=W,
            bot_account_id="bot_a",
            chat_id="friend_raw",
            chat_type="private",
            actor_person_id="p_friend",
            content="在吗",
            visibility_scope=frozenset({VisibilityScope.ISSUER_ONLY}),
            metadata={"msg_id": "m1"},
        )
        second = Event.new(
            event_type=EventType.REPLY,
            occurred_at=_dt(1, 12),
            platform=W,
            bot_account_id="bot_a",
            chat_id="friend_raw",
            chat_type="private",
            actor_person_id="p_bot",
            content="在的",
            visibility_scope=frozenset({VisibilityScope.ISSUER_ONLY}),
        )
        store.append(first)
        store.append(second)
        assert first.sequence == 1 and second.sequence == 2
        assert store.count() == 2

        loaded = store.get(first.event_id)
        assert loaded is not None
        assert loaded.content == "在吗"
        assert loaded.event_type == EventType.MESSAGE
        assert loaded.metadata["msg_id"] == "m1"
        assert loaded.visibility_scope == frozenset({VisibilityScope.ISSUER_ONLY})

        # Reopening the store resumes from the DB sequence.
        reopened = TerminalEventStore(meta.engine)
        third = reopened.append(Event.new(
            event_type=EventType.ACTION,
            occurred_at=_dt(2),
            platform=W,
            bot_account_id="bot_a",
            content="search_weather",
        ))
        assert third.sequence == 3
    finally:
        _cleanup(meta, db_path)
        shutil.rmtree(tmp, ignore_errors=True)


def test_visibility_filters_use_principal() -> None:
    tmp = _make_temp_dir()
    meta = None
    db_path = tmp / "phase2_meta.db"
    try:
        meta, store, db_path = _build_store(tmp)
        store.append(Event.new(
            event_type=EventType.THOUGHT,
            occurred_at=_dt(1),
            platform=W,
            bot_account_id="bot_a",
            chat_id="friend_raw",
            chat_type="private",
            actor_person_id="p_bot",
            content="内部推理",
            sensitivity=Sensitivity.OWNER_CHAIN_OF_THOUGHT,
            visibility_scope=frozenset({VisibilityScope.OWNER_ONLY}),
        ))
        store.append(Event.new(
            event_type=EventType.MESSAGE,
            occurred_at=_dt(1, 13),
            platform=W,
            bot_account_id="bot_a",
            chat_id="friend_raw",
            chat_type="private",
            actor_person_id="p_friend",
            content="普通消息",
            visibility_scope=frozenset({VisibilityScope.ISSUER_ONLY}),
        ))
        owner = PrincipalContext(
            platform=W,
            bot_account_id="bot_a",
            chat_id="owner_raw",
            chat_type=ChatType.PRIVATE,
            person_id="p_owner",
            display_name="老板",
            relation_to_bot=RelationLevel.OWNER,
            capabilities=frozenset({Capability.VIEW_RAW_THOUGHT}),
        )
        friend = PrincipalContext(
            platform=W,
            bot_account_id="bot_a",
            chat_id="friend_raw",
            chat_type=ChatType.PRIVATE,
            person_id="p_friend",
            display_name="朋友",
            relation_to_bot=RelationLevel.ORDINARY,
        )
        owner_events = store.query_visible(owner)
        friend_events = store.query_visible(friend)
        assert len(owner_events) == 2
        assert [e.event_type for e in friend_events] == [EventType.MESSAGE]
    finally:
        _cleanup(meta, db_path)
        shutil.rmtree(tmp, ignore_errors=True)


def test_default_store_switches_to_sqlite_when_enabled() -> None:
    tmp = _make_temp_dir()
    meta = None
    db_path = tmp / "phase2_meta.db"
    try:
        meta, store, db_path = _build_store(tmp)
        install_terminal_event_store(store)
        try:
            assert get_terminal_event_store() is store

            # Shadow helpers called without an explicit store must now persist
            # to sqlite instead of the Phase 0 JSONL file.
            event = record_reply_event(
                content="从正式事件流回复",
                platform=W,
                bot_account_id="bot_a",
                chat_id="friend_raw",
                chat_type="private",
                person_id="p_bot",
            )
            assert event.event_id
            assert store.count() == 1
            assert store.recent(1)[0].content == "从正式事件流回复"

            count = record_message_events(
                [{
                    "msg_id": "m2",
                    "chat_id": "friend_raw",
                    "chat_type": "private",
                    "sender_id": "friend_raw",
                    "sender_name": "朋友",
                    "content": "你好",
                    "created_at": _dt(2).isoformat(),
                    "source": W,
                }],
                platform=W,
                account_id="bot_a",
                identity_service=None,
            )
            assert count == 1
            assert store.count() == 2
        finally:
            reset_terminal_event_store()
    finally:
        _cleanup(meta, db_path)
        shutil.rmtree(tmp, ignore_errors=True)


def test_bot_sent_event_actor_is_the_bot() -> None:
    """Bot-emitted events carry the bot as actor and the counterpart as recipient.

    Regression guard: ``record_reply_event`` / ``record_action_event`` /
    ``record_thought_event`` used to copy the *counterpart* into
    ``actor_person_id`` (mislabelling the speaker as the person being answered —
    the "张冠李戴" bug tracked by
    ``gate_hard_rules.check_actor_semantics``).  The counterpart belongs in
    ``recipient_person_ids``, and the private-chat ACL grants the recipient
    visibility through that list.
    """
    tmp = _make_temp_dir()
    meta = None
    db_path = tmp / "phase2_meta.db"
    try:
        meta, store, db_path = _build_store(tmp)
        reply = record_reply_event(
            content="在的",
            platform=W,
            bot_account_id="bot_a",
            chat_id="friend_raw",
            chat_type="private",
            person_id="p_friend",
            store=store,
        )
        assert reply.actor_person_id == "bot_a", reply.actor_person_id
        assert reply.actor_account_id == "bot_a"
        assert reply.recipient_person_ids == ("p_friend",)

        action = record_action_event(
            action="search_weather",
            platform=W,
            bot_account_id="bot_a",
            chat_id="friend_raw",
            chat_type="private",
            person_id="p_friend",
            store=store,
        )
        assert action.actor_person_id == "bot_a"
        assert action.recipient_person_ids == ("p_friend",)

        thought = record_thought_event(
            content="先查天气",
            platform=W,
            bot_account_id="bot_a",
            chat_id="friend_raw",
            chat_type="private",
            person_id="p_friend",
            store=store,
        )
        assert thought.actor_person_id == "bot_a"
        assert thought.recipient_person_ids == ("p_friend",)

        # An event with no counterpart still names the bot as actor, no recipient.
        orphan = record_reply_event(
            content="广播",
            platform=W,
            bot_account_id="bot_a",
            chat_id="friend_raw",
            chat_type="private",
            store=store,
        )
        assert orphan.actor_person_id == "bot_a"
        assert orphan.recipient_person_ids == ()

        friend = PrincipalContext(
            platform=W,
            bot_account_id="bot_a",
            chat_id="friend_raw",
            chat_type=ChatType.PRIVATE,
            person_id="p_friend",
            display_name="朋友",
            relation_to_bot=RelationLevel.ORDINARY,
        )
        friend_events = store.query_visible(friend)
        # Visible because p_friend is in recipient_person_ids, not the actor slot.
        assert reply in friend_events
        # Raw action/thought detail stays owner-only.
        assert action not in friend_events and thought not in friend_events
    finally:
        _cleanup(meta, db_path)
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    tests = [
        test_migration_33_creates_terminal_events,
        test_append_sequence_and_roundtrip,
        test_visibility_filters_use_principal,
        test_default_store_switches_to_sqlite_when_enabled,
        test_bot_sent_event_actor_is_the_bot,
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
