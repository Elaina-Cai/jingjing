from datetime import datetime

from core.terminal.event_model import Event, EventType
from core.terminal.event_store_db import TerminalEventStore
from core.terminal.principal import (
    ChatType,
    PrincipalContext,
    RelationLevel,
    VisibilityScope,
)
from db.meta_repo import MetaRepositoryManager
from db.migrations import run_migrations


def _store(tmp_path):
    path = tmp_path / "events.db"
    meta = MetaRepositoryManager(str(path))
    run_migrations(str(path))
    return meta, TerminalEventStore(meta.engine)


def _event(platform, account, msg_id, content, *, occurred_at=None, scope=None):
    return Event.new(
        event_type=EventType.MESSAGE,
        platform=platform,
        bot_account_id=account,
        chat_id="shared-room",
        chat_type="group",
        content=content,
        external_msg_id=msg_id,
        occurred_at=occurred_at,
        visibility_scope=scope
        or frozenset({VisibilityScope.GROUP_MEMBERS}),
        metadata={"group_id": "shared-room"},
    )


def _principal(platform="wechat", account="bot_a"):
    return PrincipalContext(
        platform,
        account,
        "shared-room",
        ChatType.GROUP,
        "person-a",
        "A",
        RelationLevel.ORDINARY,
        group_id="shared-room",
    )


def test_same_external_id_on_two_platforms_is_two_events_and_replay_is_stable(tmp_path):
    meta, store = _store(tmp_path)
    try:
        first = store.append(_event("wechat", "bot_a", "m-1", "same text"))
        second = store.append(_event("qq", "bot_b", "m-1", "same text"))

        assert first.event_id != second.event_id
        assert store.count() == 2

        replay = store.append(_event("wechat", "bot_a", "m-1", "same text"))
        assert replay.event_id == first.event_id
        assert replay.sequence == first.sequence
        assert store.count() == 2
        assert len(store.query(platform="wechat")) == 1
        assert len(store.query(platform="qq")) == 1
    finally:
        meta.engine.dispose()


def test_late_event_keeps_occurred_at_and_gets_a_new_sequence(tmp_path):
    meta, store = _store(tmp_path)
    try:
        first = store.append(
            _event(
                "wechat",
                "bot_a",
                "m-first",
                "first",
                occurred_at=datetime(2026, 11, 8, 12, 0),
            )
        )
        late = store.append(
            _event(
                "wechat",
                "bot_a",
                "m-late",
                "late",
                occurred_at=datetime(2026, 11, 8, 9, 0),
            )
        )

        assert late.sequence > first.sequence
        assert late.occurred_at == datetime(2026, 11, 8, 9, 0)
        stored = store.get_by_external_msg_id(
            platform="wechat",
            bot_account_id="bot_a",
            chat_id="shared-room",
            external_msg_id="m-late",
        )
        assert stored is not None and stored.event_id == late.event_id
        assert [event.external_msg_id for event in store.recent(2)] == [
            "m-first",
            "m-late",
        ]
    finally:
        meta.engine.dispose()


def test_query_visible_does_not_mix_group_events_across_platforms(tmp_path):
    meta, store = _store(tmp_path)
    try:
        store.append(_event("wechat", "bot_a", "m-wx", "wechat group"))
        store.append(_event("qq", "bot_b", "m-qq", "qq group"))
        store.append(
            _event(
                "qq",
                "bot_b",
                "m-public",
                "public",
                scope=frozenset({VisibilityScope.PUBLIC}),
            )
        )

        visible = store.query_visible(_principal())
        assert [event.external_msg_id for event in visible] == [
            "m-wx",
            "m-public",
        ]
    finally:
        meta.engine.dispose()
