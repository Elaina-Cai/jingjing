"""Plain-stdlib checks for the canonical contracts and refactor foundations.

Run directly (``python tests/test_contracts.py``) or via the repository's
existing test-script convention.  No pytest dependency is required.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import features
from core.contracts import (
    Addressee,
    ChatType,
    DecisionAction,
    DecisionRecord,
    DecisionStage,
    DialogueAct,
    MessageKind,
    legacy_to_message_event,
)
from core.observability import decision_trace


def test_feature_flags() -> bool:
    print("\n[feature flags]")
    key = features.env_key("integration_test_flag")
    old = os.environ.pop(key, None)
    try:
        ok = not features.enabled("integration_test_flag")
        os.environ[key] = "on"
        ok &= features.enabled("integration_test_flag")
        os.environ[key] = "0"
        ok &= not features.enabled("integration_test_flag", default=True)
        print("  ok" if ok else "  FAIL")
        return ok
    finally:
        if old is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = old


def test_legacy_adapter() -> bool:
    print("\n[legacy adapter]")
    msg = {
        "msg_id": "1001",
        "sender_id": "wxid_a",
        "sender_name": "张三",
        "chat_id": "group@chatroom",
        "chat_type": "group",
        "content": "大家早上好",
        "msg_type": 1,
        "msg_type_name": "text",
        "mentions": json.dumps([{"user_id": "wxid_a", "user_name": "张三"}]),
        "media_path": "",
        "created_at": "2026-09-20 09:30:00",
        "source": "wechat",
        "account_id": "wxid_bot",
    }
    event = legacy_to_message_event(msg, event_id="evt-1")
    ok = event.event_id == "evt-1"
    ok &= event.platform == "wechat"
    ok &= event.account_id == "wxid_bot"
    ok &= event.chat.chat_type == ChatType.GROUP
    ok &= event.chat.is_group
    ok &= event.kind == MessageKind.TEXT
    ok &= event.sender_id == "wxid_a"
    ok &= event.sender_name == "张三"
    ok &= len(event.mentions) == 1 and event.mentions[0].user_id == "wxid_a"
    ok &= event.evidence_ref().message_id == "1001"
    ok &= event.conversation_key == ("wechat", "wxid_bot", "group@chatroom")
    ok &= event.to_dict()["kind"] == "text"

    numeric = dict(msg)
    numeric.pop("msg_type_name")
    numeric["msg_type"] = 47
    numeric["content"] = "[表情包]"
    event2 = legacy_to_message_event(numeric)
    ok &= event2.kind == MessageKind.STICKER
    print("  ok" if ok else "  FAIL")
    return bool(ok)


def test_decision_contracts() -> bool:
    print("\n[decision contracts]")
    record = DecisionRecord(
        stage=DecisionStage.ENGAGEMENT,
        action=DecisionAction.OBSERVE,
        reason="medium confidence continuation",
        confidence=0.61,
        dialogue_act=DialogueAct.ANSWER_TO_BOT,
        addressee=Addressee.BOT,
        rule_ids=("engagement.continuation",),
        signals={"same_sender": 0.3, "time_decay": 0.2},
    )
    payload = record.to_dict()
    ok = payload["stage"] == "engagement"
    ok &= payload["action"] == "observe"
    ok &= payload["addressee"] == "bot"
    ok &= payload["rule_ids"] == ["engagement.continuation"]
    ok &= payload["signals"]["same_sender"] == 0.3
    print("  ok" if ok else "  FAIL")
    return bool(ok)


def test_decision_trace() -> bool:
    print("\n[decision trace]")
    repo_root = Path(__file__).resolve().parent.parent
    trace_dir = repo_root / "logs"
    trace_dir.mkdir(parents=True, exist_ok=True)
    path = trace_dir / "_test_contracts_decisions.jsonl"
    path.unlink(missing_ok=True)
    try:
        ok = decision_trace.record_decision(
            {"kind": "unit", "answer": 42},
            force=True,
            path=path,
        )
        ok &= path.exists()
        rows = decision_trace.read_decisions(limit=10, path=path)
        ok &= len(rows) == 1 and rows[0]["answer"] == 42
        ok &= "trace_id" in rows[0] and "ts" in rows[0]

        message = {
            "msg_id": "2001",
            "sender_id": "wxid_a",
            "sender_name": "张三",
            "chat_id": "group@chatroom",
            "chat_type": "group",
            "content": "那你觉得呢",
            "msg_type_name": "text",
            "source": "wechat",
            "account_id": "wxid_bot",
        }
        decision = SimpleNamespace(
            should_reply=True,
            reason="group_deferred_to_state",
            reply_platform="wechat",
            reply_account="self",
            reply_account_id="wxid_bot",
            reply_chat_id="group@chatroom",
            is_group=True,
            needs_judge=False,
        )
        count = decision_trace.trace_route_decisions(
            [message], [decision], context=None, force=True, path=path,
        )
        rows = decision_trace.read_decisions(limit=10, path=path)
        ok &= count == 1 and len(rows) == 2
        last = rows[-1]
        ok &= last["kind"] == "route_decision"
        ok &= last["decision"]["should_reply"] is True
        ok &= last["event"]["conversation"]["chat_type"] == "group"
        ok &= last["event"]["content"] == "那你觉得呢"
        print("  ok" if ok else "  FAIL")
        return bool(ok)
    finally:
        path.unlink(missing_ok=True)


def main() -> bool:
    print("=" * 50)
    print("contracts / refactor foundation checks")
    print("=" * 50)
    ok = test_feature_flags()
    ok &= test_legacy_adapter()
    ok &= test_decision_contracts()
    ok &= test_decision_trace()
    print("\n" + ("all passed" if ok else "FAILED"))
    return bool(ok)


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
