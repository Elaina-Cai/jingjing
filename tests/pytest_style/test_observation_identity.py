"""P1：平台观察的事件 id 必须可重算，不能靠随机数。

验四件事：
1. 同一四元组在任何进程/任何空库上得到同一个 id；
2. 换平台/账号/会话/消息 id 必然不同；同一句话重发是两次发生，也不同；
3. 缺平台唯一键时返回空串（调用方自己决定兜底），不静默造一个随机 id；
4. 入站链路（Event.new / MessageEvent.new / 旧字典适配器）用的是同一份派生。
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from core.contracts.events import ChatType, MessageEvent, observation_event_id
from core.contracts.legacy import legacy_to_message_event
from core.terminal.event_model import Event, EventType
from core.terminal.event_store_db import TerminalEventStore
from db.meta_repo import MetaRepositoryManager
from db.migrations import run_migrations

W = "wechat"
BOT = "bot_a"
CHAT = "g1"
MSG = "m-1"


def _store(tmp_path, name: str):
    path = tmp_path / name
    meta = MetaRepositoryManager(str(path))
    run_migrations(str(path))
    return meta, TerminalEventStore(meta.engine)


def _inbound(external_id: str = MSG, *, content: str = "同一句话", chat: str = CHAT) -> Event:
    return Event.new(
        event_type=EventType.MESSAGE,
        platform=W,
        bot_account_id=BOT,
        chat_id=chat,
        chat_type="group",
        content=content,
        external_msg_id=external_id,
    )


def _rows(store: TerminalEventStore) -> int:
    with store.engine.connect() as conn:
        return int(conn.execute(text("SELECT COUNT(*) FROM terminal_events")).scalar() or 0)


def test_helper_is_deterministic_and_scope_sensitive():
    base = dict(platform=W, account_id=BOT, chat_id=CHAT, external_id=MSG)
    first = observation_event_id(**base)

    assert first == observation_event_id(**base)
    assert first.startswith("evt:")
    assert first != observation_event_id(**{**base, "platform": "qq"})
    assert first != observation_event_id(**{**base, "account_id": "bot_b"})
    assert first != observation_event_id(**{**base, "chat_id": "g2"})
    assert first != observation_event_id(**{**base, "external_id": "m-2"})


@pytest.mark.parametrize("missing", ["platform", "account_id", "chat_id", "external_id"])
def test_missing_platform_key_returns_empty(missing):
    base = dict(platform=W, account_id=BOT, chat_id=CHAT, external_id=MSG)
    base[missing] = ""
    assert observation_event_id(**base) == ""


def test_event_new_derives_from_external_id_and_keeps_ephemeral_events_random():
    a = _inbound()
    b = _inbound()
    assert a.event_id == b.event_id

    # 没有平台唯一键的事件（思考/动作等）仍是临时事件，不能共用同一个 id。
    x = Event.new(event_type=EventType.THOUGHT, platform=W, bot_account_id=BOT, chat_id=CHAT)
    y = Event.new(event_type=EventType.THOUGHT, platform=W, bot_account_id=BOT, chat_id=CHAT)
    assert x.event_id != y.event_id


def test_same_text_sent_twice_is_two_observations():
    first = _inbound("m-1", content="同一句话")
    second = _inbound("m-2", content="同一句话")
    assert first.event_id != second.event_id


def test_message_event_and_legacy_adapter_share_the_same_identity():
    canonical = MessageEvent.new(
        platform=W,
        account_id=BOT,
        chat_id=CHAT,
        chat_type=ChatType.GROUP,
        message_id=MSG,
    )
    legacy = legacy_to_message_event(
        {
            "source": W,
            "account_id": BOT,
            "chat_id": CHAT,
            "msg_id": MSG,
            "chat_type": "group",
            "msg_type_name": "text",
            "content": "同一句话",
        }
    )

    assert canonical.event_id == observation_event_id(
        platform=W, account_id=BOT, chat_id=CHAT, external_id=MSG
    )
    assert legacy.event_id == canonical.event_id


def test_rebuild_from_source_gets_the_same_id_and_replay_does_not_duplicate(tmp_path):
    meta_a, store_a = _store(tmp_path, "a.db")
    meta_b, store_b = _store(tmp_path, "b.db")
    try:
        first = store_a.append(_inbound())

        # 换一个空库重新采集同一条平台消息：id 必须相同（不是「新经历」）。
        rebuilt = store_b.append(_inbound())
        assert rebuilt.event_id == first.event_id

        # 同库重放：不插第二行，返回已存行与它真实的 sequence。
        replay = store_a.append(_inbound())
        assert replay.event_id == first.event_id
        assert replay.sequence == first.sequence
        assert _rows(store_a) == 1
    finally:
        meta_a.engine.dispose()
        meta_b.engine.dispose()
