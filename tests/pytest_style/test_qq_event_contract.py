"""QQ OneBot 事件到统一 Message 的契约（不依赖真实 NapCat）。"""
from __future__ import annotations

from core.execution.qq_provider import inbound_from_event
from core.ingestion.message_bridge import inbound_to_message


GROUP_AT = {
    "post_type": "message",
    "message_type": "group",
    "self_id": 10001,
    "user_id": 20003,
    "group_id": 333333,
    "message_id": 90002,
    "time": 1758780010,
    "raw_message": "[CQ:at,qq=10001] 在吗",
    "sender": {"nickname": "小红", "card": "红红"},
    "message": [
        {"type": "at", "data": {"qq": "10001", "name": "鲸鲸"}},
        {"type": "text", "data": {"text": " 在吗"}},
    ],
}


def test_qq_group_event_keeps_scope_content_and_stable_message_id():
    first = inbound_from_event(dict(GROUP_AT))
    second = inbound_from_event(dict(GROUP_AT))
    assert first is not None and second is not None
    a = inbound_to_message(first)
    b = inbound_to_message(second)
    assert a.id == b.id
    assert a.chat_id == "group:333333"
    assert a.chat_type == "group"
    assert a.sender_id == "20003"
    assert a.sender_name == "红红"
    assert a.content == "@鲸鲸 在吗"


def test_qq_group_and_private_with_same_number_do_not_collide():
    group = inbound_to_message(inbound_from_event(dict(GROUP_AT)))
    private_event = dict(GROUP_AT)
    private_event.update(
        {
            "message_type": "private",
            "user_id": 333333,
            "message_id": 90003,
            "sender": {"nickname": "小刚", "card": ""},
            "message": [{"type": "text", "data": {"text": "私聊"}}],
        }
    )
    private_event.pop("group_id", None)
    private = inbound_to_message(inbound_from_event(private_event))
    assert private.chat_id == "user:333333"
    assert private.chat_type == "private"
    assert group.id != private.id
