"""WeChat revokes become linked facts without guessing an ambiguous target."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from core.context import cognitive_commit as cc
from core.memory import belief_commit
from core.terminal.event_model import Event, EventType
from core.terminal.event_store_db import TerminalEventStore
from core.terminal.shadow import record_message_events
from db.meta_repo import MetaRepositoryManager
from db.migrations import run_migrations


SCOPE = cc.Scope("wechat", "bot-a", "friend-a")


@pytest.fixture()
def store(tmp_path):
    path = tmp_path / "events.db"
    meta = MetaRepositoryManager(str(path))
    run_migrations(str(path))
    try:
        yield TerminalEventStore(meta.engine)
    finally:
        meta.engine.dispose()


def _message(
    msg_id: str,
    content: str,
    at: datetime,
    *,
    sender_name: str = "朋友",
    is_self: bool = False,
) -> dict:
    return {
        "msg_id": msg_id,
        "chat_id": SCOPE.chat_id,
        "chat_type": "private",
        "sender_id": SCOPE.account_id if is_self else "friend-wxid",
        "sender_name": sender_name,
        "content": content,
        "created_at": at.isoformat(),
        "is_self": int(is_self),
        "source": SCOPE.platform,
    }


def _revoke(
    msg_id: str,
    notice: str,
    at: datetime,
    *,
    newmsgid: str = "",
) -> dict:
    reference = f"<newmsgid>{newmsgid}</newmsgid>" if newmsgid else ""
    return _message(
        msg_id,
        "<?xml version=\"1.0\"?><sysmsg type=\"revokemsg\"><revokemsg>"
        f"{reference}<content>{notice}</content><revoketime>0</revoketime>"
        "</revokemsg></sysmsg>",
        at,
        sender_name="系统",
    )


def _record(store: TerminalEventStore, rows: list[dict]) -> None:
    assert record_message_events(
        rows,
        platform=SCOPE.platform,
        account_id=SCOPE.account_id,
        store=store,
    ) == len(rows)


def _revoke_event(store: TerminalEventStore):
    rows = store.query(event_types=(EventType.REVOKE,))
    assert len(rows) == 1
    return rows[0]


def test_explicit_newmsgid_links_the_revoke_to_its_source(store):
    at = datetime(2026, 11, 8, 12, 0, 0)
    _record(store, [
        _message("source-1", "我喜欢咖啡", at),
        _revoke("revoke-1", '"朋友" 撤回了一条消息', at + timedelta(seconds=5),
                newmsgid="source-1"),
    ])

    source = store.get_by_external_msg_id(
        platform=SCOPE.platform,
        bot_account_id=SCOPE.account_id,
        chat_id=SCOPE.chat_id,
        external_msg_id="source-1",
    )
    revoke = _revoke_event(store)
    assert source is not None
    assert revoke.parent_event_id == source.event_id
    assert revoke.metadata["revoke"]["resolution"] == "exact"
    assert revoke.metadata["revoke"]["ref_external_msg_id"] == "source-1"
    assert belief_commit.evidence_state(source.event_id, SCOPE, store=store) == "dead"


def test_missing_newmsgid_uses_unique_same_sender_hint(store):
    at = datetime(2026, 11, 8, 12, 0, 0)
    _record(store, [
        _message("source-2", "我还是喜欢原来的方案", at),
        _revoke("revoke-2", '"朋友" 撤回了一条消息', at + timedelta(seconds=12)),
    ])

    source = store.get_by_external_msg_id(
        platform=SCOPE.platform,
        bot_account_id=SCOPE.account_id,
        chat_id=SCOPE.chat_id,
        external_msg_id="source-2",
    )
    revoke = _revoke_event(store)
    assert source is not None
    assert revoke.parent_event_id == source.event_id
    assert revoke.metadata["revoke"]["resolution"] == "inferred"
    assert belief_commit.evidence_state(source.event_id, SCOPE, store=store) == "dead"


def test_explicit_unknown_id_does_not_fall_back_to_a_plausible_neighbor(store):
    at = datetime(2026, 11, 8, 12, 0, 0)
    _record(store, [
        _message("source-2b", "这句话看起来像要撤回的那句", at),
        _revoke("revoke-2b", '"朋友" 撤回了一条消息', at + timedelta(seconds=5),
                newmsgid="missing-source"),
    ])

    revoke = _revoke_event(store)
    assert revoke.parent_event_id == ""
    assert revoke.metadata["revoke"]["resolution"] == "unknown"
    assert revoke.metadata["revoke"]["ref_external_msg_id"] == "missing-source"


def test_ambiguous_hint_stays_unknown_instead_of_revoking_the_wrong_message(store):
    at = datetime(2026, 11, 8, 12, 0, 0)
    _record(store, [
        _message("source-3a", "下午可以", at),
        _message("source-3b", "晚点也行", at + timedelta(seconds=30)),
        _revoke("revoke-3", '"朋友" 撤回了一条消息', at + timedelta(seconds=45)),
    ])

    revoke = _revoke_event(store)
    assert revoke.parent_event_id == ""
    assert revoke.metadata["revoke"]["resolution"] == "unknown"
    for external_id in ("source-3a", "source-3b"):
        source = store.get_by_external_msg_id(
            platform=SCOPE.platform,
            bot_account_id=SCOPE.account_id,
            chat_id=SCOPE.chat_id,
            external_msg_id=external_id,
        )
        assert source is not None
        assert belief_commit.evidence_state(source.event_id, SCOPE, store=store) == "unknown"


def test_self_revoke_can_link_an_echoed_own_message(store):
    at = datetime(2026, 11, 8, 12, 0, 0)
    _record(store, [
        _message("source-4", "我自己发错了", at, sender_name="鲸鲸", is_self=True),
        _revoke("revoke-4", "你撤回了一条消息", at + timedelta(seconds=6)),
    ])

    source = store.get_by_external_msg_id(
        platform=SCOPE.platform,
        bot_account_id=SCOPE.account_id,
        chat_id=SCOPE.chat_id,
        external_msg_id="source-4",
    )
    revoke = _revoke_event(store)
    assert source is not None
    assert revoke.parent_event_id == source.event_id
    assert revoke.metadata["revoke"]["is_self"] is True


def test_legacy_untyped_revoke_is_recognized_without_backfilling_rows(store):
    at = datetime(2026, 11, 8, 12, 0, 0)
    _record(store, [_message("source-5", "旧记录里的这句话后来被撤回", at)])
    legacy = store.append(Event.new(
        event_type=EventType.MESSAGE,
        occurred_at=at + timedelta(seconds=8),
        platform=SCOPE.platform,
        bot_account_id=SCOPE.account_id,
        chat_id=SCOPE.chat_id,
        chat_type="private",
        content=(
            "<?xml version=\"1.0\"?><sysmsg type=\"revokemsg\"><revokemsg>"
            "<content>\"朋友\" 撤回了一条消息</content>"
            "<revoketime>0</revoketime></revokemsg></sysmsg>"
        ),
        external_msg_id="legacy-revoke-5",
        metadata={"msg_id": "legacy-revoke-5", "sender_name": "系统", "is_self": 0},
    ))

    source = store.get_by_external_msg_id(
        platform=SCOPE.platform,
        bot_account_id=SCOPE.account_id,
        chat_id=SCOPE.chat_id,
        external_msg_id="source-5",
    )
    assert legacy.event_type == EventType.MESSAGE
    assert source is not None
    assert belief_commit.evidence_state(source.event_id, SCOPE, store=store) == "dead"
    assert len(store.query(event_types=(EventType.REVOKE,))) == 0
