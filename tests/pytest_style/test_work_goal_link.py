"""P3：执行实例（jobs/work_items）必须能指回目标真源。

仓库规则是「新字段先落事件 payload，稳定后再提列」，所以这里先钉住：
`work_item_created.goal_id` 只在意图功能开启时给值（flag off 时不会开目标，
挂悬空引用比空串更坏）。
"""
from datetime import datetime

from core.tasks.store import _created_event, _goal_id_for_reminder

DUE = datetime(2026, 11, 7, 10, 0)


def _fields():
    return {
        "work_item_id": "w-1",
        "kind": "notify",
        "trigger": "at",
        "status": "open",
        "due_at": DUE,
        "priority": 0,
        "target_platform": "wechat",
        "target_bot_account_id": "bot",
        "target_chat_id": "c1",
        "target_chat_type": "private",
        "requester_person_id": "p-owner",
        "requester_role": "owner",
        "requester_is_owner": True,
        "requester_account_id": "wxid_owner",
        "spec_json": "{}",
        "idempotency_key": "w-1",
        "origin_platform": "wechat",
        "origin_bot_account_id": "bot",
        "origin_chat_id": "c1",
        "origin_chat_type": "private",
        "command_id": "",
        "authorization_id": "",
        "session_id": "",
        "thread_id": "",
        "repeat_spec_json": None,
        "max_attempts": 3,
        "fallback_text": "",
        "acceptance_criteria": "send_log.ok",
        "visibility_scope": "private",
        "external_msg_id": "",
        "legacy_reminder_id": 7,
    }


def test_goal_id_is_empty_when_the_goal_layer_is_off(monkeypatch):
    monkeypatch.setenv("YINYUE_FEATURE_INTENTION_TICK", "off")
    row = type("Row", (), {"id": 7, "remind_at": DUE})()

    assert _goal_id_for_reminder(row) == ""


def test_goal_id_matches_the_reminder_goal_key(monkeypatch):
    monkeypatch.setenv("YINYUE_FEATURE_INTENTION_TICK", "on")
    row = type("Row", (), {"id": 7, "remind_at": DUE})()

    assert _goal_id_for_reminder(row) == "reminder:7@2026-11-07 10:00"


def test_missing_reminder_id_has_no_goal():
    row = type("Row", (), {"remind_at": DUE})()

    assert _goal_id_for_reminder(row) == ""


def test_created_event_carries_the_goal_id():
    event = _created_event(
        _fields(),
        anchor={"goal_id": "reminder:7@2026-11-07 10:00"},
        goal="喝水",
    )

    assert event.action_payload["goal_id"] == "reminder:7@2026-11-07 10:00"


def test_created_event_without_goal_keeps_an_explicit_empty_string():
    event = _created_event(_fields(), anchor={}, goal="喝水")

    assert event.action_payload["goal_id"] == ""
