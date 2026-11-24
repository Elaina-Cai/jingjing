"""Golden-set resolver evaluation + precise reply-to-bot regression (P3).

Run directly: python tests/test_golden_resolver_eval.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.contracts import (
    Addressee,
    ChatType,
    DecisionAction,
    MessageEvent,
    MessageKind,
)
from core.conversation import AddresseeResolver, ConversationWindow
from core.conversation.eval import load_and_evaluate

_ROOT = Path(__file__).resolve().parent.parent
_CASES = _ROOT / "tests" / "golden" / "group_conversation_cases.json"


def test_golden() -> bool:
    print("\n[golden resolver eval]")
    report = load_and_evaluate(_CASES)
    data = report.to_dict()
    ok = report.evaluated >= 10
    ok &= report.skipped == 2
    ok &= set(report.skipped_ids) == {"private.owner.question", "private.owner.closing"}
    ok &= report.exact_accuracy >= 0.9
    ok &= report.reply_precision >= 0.9
    ok &= report.reply_recall >= 0.9
    ok &= report.false_interjection_rate <= 0.05
    print(
        f"  evaluated={report.evaluated} skipped={report.skipped} "
        f"exact={data['exact_accuracy']} precision={data['reply_precision']} "
        f"recall={data['reply_recall']} interjection={data['false_interjection_rate']}"
    )
    for outcome in report.outcomes:
        if not outcome.matched:
            print(f"  FAIL {outcome.case_id}: expected={outcome.expected_action} "
                  f"predicted={outcome.predicted_action} reason={outcome.reason}")
    print("  ok" if ok else "  FAIL")
    return bool(ok)


def test_reply_to_known_bot_message() -> bool:
    print("\n[reply-to-bot precise signal]")
    resolver = AddresseeResolver()
    window = ConversationWindow(
        platform="wechat", account_id="bot", chat_id="group@chatroom",
        person_id="wxid_a",
    )
    window.opens_with("wxid_a")
    window.last_bot_message_id = "bot-777"
    event = MessageEvent.new(
        platform="wechat", account_id="bot",
        chat_id="group@chatroom", chat_type=ChatType.GROUP,
        message_id="m-1", sender_id="wxid_a", sender_name="张三",
        content="那我周五再来找你", kind=MessageKind.TEXT,
        reply_to_id="bot-777",
    )
    decision = resolver.evaluate(event, window=window, bot_ids={"bot"},
                                 aliases=["鲸鲸"])
    ok = decision.action == DecisionAction.REPLY
    ok &= decision.addressee == Addressee.BOT
    print(f"  action={decision.action.value} reason={decision.reason}")
    print("  ok" if ok else "  FAIL")
    return bool(ok)


def main() -> bool:
    print("=" * 50)
    print("golden resolver eval checks")
    print("=" * 50)
    ok = test_golden()
    ok &= test_reply_to_known_bot_message()
    print("\n" + ("all passed" if ok else "FAILED"))
    return bool(ok)


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
