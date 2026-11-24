"""TurnIntent regression checks (P1 loop-guard prerequisite).

Run directly: python tests/test_turn_intent.py

Covers:
* utterance classification (answer / ack / question / command / banter / statement);
* the ``unknown_answer`` signal for "我也不知道" only in answer context;
* active-gate propagation via ``TurnDecision.active_intent`` (duck + frozen).
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from types import SimpleNamespace
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.contracts import (
    Addressee,
    ChatType,
    DecisionAction,
    DialogueAct,
    MessageEvent,
    MessageKind,
    TurnIntent,
)
from core.conversation import AddresseeResolver, ConversationWindow
from core.conversation.active import apply_active_decisions
from core.conversation.store import InMemoryWindowStore, make_key


def _event(
    content: str,
    *,
    sender: str = "wxid_zhang",
    message_id: str = "m1",
    created_at: datetime | None = None,
) -> MessageEvent:
    return MessageEvent.new(
        platform="wechat",
        account_id="wxid_bot",
        chat_id="group@chatroom",
        chat_type=ChatType.GROUP,
        message_id=message_id,
        sender_id=sender,
        sender_name="张三",
        content=content,
        kind=MessageKind.TEXT,
        created_at=created_at or datetime.now(),
    )


def _window(*, asked: bool = True, now: datetime | None = None) -> ConversationWindow:
    now = now or datetime.now()
    window = ConversationWindow(
        platform="wechat", account_id="wxid_bot",
        chat_id="group@chatroom", person_id="wxid_zhang",
    )
    window.opens_with("wxid_zhang", now=now)
    window.last_bot_asked_question = asked
    window.last_activity_at = now
    return window


def main() -> bool:
    resolver = AddresseeResolver()
    now = datetime.now()
    bot_ids = {"wxid_bot"}
    aliases = ["鲸鲸"]

    def evaluate(content: str, *, asked: bool = True):
        return resolver.evaluate(
            _event(content, created_at=now),
            window=_window(asked=asked, now=now),
            bot_ids=bot_ids, aliases=aliases, now=now,
        )

    ok = True

    d = evaluate("我也不知道")
    ok &= d.turn_intent == TurnIntent.ANSWER
    ok &= d.signals.get("unknown_answer") == 1.0
    ok &= d.dialogue_act == DialogueAct.ANSWER_TO_BOT
    ok &= d.action == DecisionAction.REPLY
    print(f"  {'ok' if ok else 'FAIL'} unknown_answer: {d.turn_intent.value}/"
          f"{d.dialogue_act.value}/{d.action.value}")

    d = evaluate("不知道。")
    ok &= d.signals.get("unknown_answer") == 1.0
    ok &= d.turn_intent == TurnIntent.ANSWER
    print(f"  {'ok' if ok else 'FAIL'} unknown_answer_with_punctuation: "
          f"unknown={d.signals.get('unknown_answer')}")

    d = evaluate("1分钟")
    ok &= d.turn_intent == TurnIntent.ANSWER
    ok &= d.signals.get("unknown_answer") == 0.0
    print(f"  {'ok' if ok else 'FAIL'} plain_answer: {d.turn_intent.value} "
          f"unknown={d.signals.get('unknown_answer')}")

    d = evaluate("噢噢")
    ok &= d.turn_intent == TurnIntent.ACK
    ok &= d.dialogue_act == DialogueAct.ACK
    print(f"  {'ok' if ok else 'FAIL'} ack: {d.turn_intent.value}/{d.dialogue_act.value}")

    d = evaluate("你在玩什么")
    ok &= d.turn_intent == TurnIntent.QUESTION
    print(f"  {'ok' if ok else 'FAIL'} question: {d.turn_intent.value}")

    d = evaluate("帮我查一下天气")
    ok &= d.turn_intent == TurnIntent.COMMAND
    print(f"  {'ok' if ok else 'FAIL'} command: {d.turn_intent.value}")

    d = evaluate("哈哈")
    ok &= d.turn_intent == TurnIntent.BANTER
    print(f"  {'ok' if ok else 'FAIL'} banter: {d.turn_intent.value}")

    d = evaluate("今天天气不错", asked=False)
    ok &= d.turn_intent == TurnIntent.STATEMENT
    print(f"  {'ok' if ok else 'FAIL'} statement: {d.turn_intent.value}")

    # "我也不知道" without a pending bot question is not an answer.
    d = evaluate("我也不知道", asked=False)
    ok &= d.signals.get("unknown_answer") == 0.0
    print(f"  {'ok' if ok else 'FAIL'} unknown_requires_question: "
          f"unknown={d.signals.get('unknown_answer')}")

    d = evaluate("我也不知道")
    ok &= d.to_dict().get("turn_intent") == "answer"
    print(f"  {'ok' if ok else 'FAIL'} decision_to_dict: {d.to_dict().get('turn_intent')}")

    # Active gate must forward the intent/unknown flag on the route decision.
    store = InMemoryWindowStore()
    key = make_key("wechat", "wxid_bot", "group@chatroom", "wxid_zhang")
    store.save(key, _window(asked=True, now=now))
    route_decision = SimpleNamespace(
        should_reply=False, is_group=True, reason="", reply_chat_id="",
    )
    msg = {
        "source": "wechat",
        "chat_id": "group@chatroom",
        "chat_type": "group",
        "msg_id": "m-active",
        "msg_type": 1,
        "sender_id": "wxid_zhang",
        "sender_name": "张三",
        "content": "我也不知道",
    }
    applied = apply_active_decisions(
        [msg], [route_decision],
        store=store,
        bot_ids=bot_ids, aliases=aliases,
        groups={"group@chatroom"}, mode="smart", force=True,
        platform="wechat", account_id="wxid_bot", reply_account_id="wxid_bot",
        # A non-empty recent view is required for the no_intervening bonus;
        # only same-sender history keeps it eligible.
        recent_factory=lambda *_: [_event("上一轮消息", message_id="m-prev")],
    )
    ok &= applied == 1
    ok &= bool(route_decision.should_reply)
    ok &= getattr(route_decision, "active_intent", "") == "answer"
    ok &= bool(getattr(route_decision, "active_unknown_answer", False))
    print(f"  {'ok' if ok else 'FAIL'} active_gate_forward: applied={applied} "
          f"intent={getattr(route_decision, 'active_intent', '')} "
          f"unknown={getattr(route_decision, 'active_unknown_answer', False)}")

    # WP-06 stage B：frozen TurnDecision 同样要能过 gate（replace 写回列表）。
    from core.routing.turn_decision import TurnDecision

    frozen = [TurnDecision(action="silent", is_group=True, reason="")]
    applied_frozen = apply_active_decisions(
        [msg], frozen,
        store=store,
        bot_ids=bot_ids, aliases=aliases,
        groups={"group@chatroom"}, mode="smart", force=True,
        platform="wechat", account_id="wxid_bot", reply_account_id="wxid_bot",
        recent_factory=lambda *_: [_event("上一轮消息", message_id="m-prev")],
    )
    ok &= applied_frozen == 1
    ok &= frozen[0].should_reply is True
    ok &= frozen[0].reason == "conversation_active"
    ok &= frozen[0].active_intent == "answer"
    ok &= frozen[0].active_unknown_answer is True
    print(f"  {'ok' if ok else 'FAIL'} active_gate_frozen_turn_decision: "
          f"applied={applied_frozen} action={frozen[0].action} "
          f"intent={frozen[0].active_intent}")

    print("\nall passed" if ok else "\nsome checks failed")
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
