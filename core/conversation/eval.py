"""Golden-set evaluation for the addressee resolver (P3).

This is a harness, not production logic.  It maps each golden case into a
synthetic ``MessageEvent`` + ``ConversationWindow`` and compares the resolver
decision with the expected one.  It intentionally skips private/owner policy
cases: that policy is a separate concern which the resolver does not own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from core.contracts import (
    ChatType,
    ConversationRef,
    DecisionAction,
    MentionRef,
    MessageEvent,
    MessageKind,
)
from core.conversation.domain import ConversationWindow
from core.conversation.resolver import AddresseeResolver

_ACTION_MAP = {
    "reply": DecisionAction.REPLY,
    "observe": DecisionAction.OBSERVE,
    "drop": DecisionAction.DROP,
    "close": DecisionAction.CLOSE,
    "defer": DecisionAction.OBSERVE,
}
_KNOWN_BOT_NAMES = frozenset({"鲸鲸"})


@dataclass
class CaseOutcome:
    case_id: str
    scenario: str
    expected_action: str
    predicted_action: str
    matched: bool
    addressee_match: bool
    dialogue_act_match: bool
    reason: str = ""


@dataclass
class EvalReport:
    total: int = 0
    evaluated: int = 0
    skipped: int = 0
    exact_accuracy: float = 0.0
    reply_precision: float = 0.0
    reply_recall: float = 0.0
    false_interjection_rate: float = 0.0
    outcomes: list[CaseOutcome] = field(default_factory=list)
    skipped_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "evaluated": self.evaluated,
            "skipped": self.skipped,
            "exact_accuracy": round(self.exact_accuracy, 4),
            "reply_precision": round(self.reply_precision, 4),
            "reply_recall": round(self.reply_recall, 4),
            "false_interjection_rate": round(self.false_interjection_rate, 4),
            "skipped_ids": list(self.skipped_ids),
            "outcomes": [
                {
                    "case_id": o.case_id,
                    "scenario": o.scenario,
                    "expected": o.expected_action,
                    "predicted": o.predicted_action,
                    "matched": o.matched,
                    "addressee_match": o.addressee_match,
                    "dialogue_act_match": o.dialogue_act_match,
                    "reason": o.reason,
                }
                for o in self.outcomes
            ],
        }


def _as_message(case: Mapping[str, Any]) -> Mapping[str, Any]:
    message = case.get("message")
    if isinstance(message, Mapping):
        return message
    return {"content": str(message or ""), "sender": "用户"}


def _kind(content: str) -> MessageKind:
    if "[表情包" in content or "[sticker" in content:
        return MessageKind.STICKER
    if "[合并转发]" in content:
        return MessageKind.FORWARD
    if content.startswith("http://") or content.startswith("https://"):
        return MessageKind.LINK
    return MessageKind.TEXT


def _mentions(raw: Any) -> tuple[MentionRef, ...]:
    if not raw:
        return ()
    if not isinstance(raw, (list, tuple)):
        raw = [raw]
    out = []
    for item in raw:
        value = str(item).strip()
        if not value or value in _KNOWN_BOT_NAMES or value.lower() == "bot":
            out.append(MentionRef(user_id="bot"))
        else:
            out.append(MentionRef(user_id=value, display_name=value))
    return tuple(out)


def _case_event(case: Mapping[str, Any], now: datetime) -> MessageEvent:
    message = _as_message(case)
    content = str(message.get("content") or "")
    sender = str(message.get("sender") or "用户")
    return MessageEvent(
        event_id=f"eval:{case.get('id')}",
        platform="wechat",
        account_id="eval_bot",
        chat=ConversationRef(
            platform="wechat",
            account_id="eval_bot",
            chat_id="eval_group@chatroom",
            chat_type=ChatType(str(case.get("chat_type") or "group")),
        ),
        message_id=f"eval-{case.get('id')}",
        sender_id=str(message.get("sender_id") or sender),
        sender_name=sender,
        kind=_kind(content),
        content=content,
        reply_to_id=str(message.get("reply_to") or ""),
        mentions=_mentions(message.get("mentions")),
        created_at=now,
    )


def _window_from_history(case: Mapping[str, Any], now: datetime) -> ConversationWindow | None:
    history = case.get("history") or []
    if not isinstance(history, list):
        return None
    assistants = [h for h in history if isinstance(h, Mapping) and h.get("role") == "assistant"]
    if not assistants:
        return None
    users = [h for h in history if isinstance(h, Mapping) and h.get("role") == "user"]
    person = str(users[-1].get("sender") if users else "用户")
    last_bot = assistants[-1]
    bot_text = str(last_bot.get("content") or "")
    window = ConversationWindow(
        platform="wechat",
        account_id="eval_bot",
        chat_id="eval_group@chatroom",
    )
    window.opens_with(person, now=now)
    window.last_bot_message_id = "eval-bot-last"
    window.last_bot_asked_question = ("?" in bot_text) or ("？" in bot_text)
    window.last_activity_at = now
    return window


def _other_names(case: Mapping[str, Any], current_sender: str) -> list[str]:
    names: set[str] = set(str(n) for n in (case.get("other_names") or []))
    for item in case.get("history") or []:
        if not isinstance(item, Mapping):
            continue
        sender = str(item.get("sender") or "")
        if sender and sender != current_sender and sender not in _KNOWN_BOT_NAMES:
            names.add(sender)
    message = _as_message(case)
    for mention in message.get("mentions") or []:
        value = str(mention)
        if value and value.lower() != "bot" and value not in _KNOWN_BOT_NAMES:
            names.add(value)
    return sorted(names)


def evaluate_cases(
    cases: Iterable[Mapping[str, Any]],
    *,
    resolver: AddresseeResolver | None = None,
) -> EvalReport:
    resolver = resolver or AddresseeResolver()
    cases = [case for case in cases if isinstance(case, Mapping)]
    report = EvalReport(total=len(cases))
    now = datetime.now()
    tp = fp = fn = tn = 0

    for case in cases:
        case_id = str(case.get("id") or "")
        chat_type = str(case.get("chat_type") or "group")
        if chat_type != "group":
            report.skipped += 1
            report.skipped_ids.append(case_id)
            continue

        expected_raw = str((case.get("expected") or {}).get("action") or "")
        expected = _ACTION_MAP.get(expected_raw)
        if expected is None:
            report.skipped += 1
            report.skipped_ids.append(case_id)
            continue

        event = _case_event(case, now)
        window = _window_from_history(case, now)
        decision = resolver.evaluate(
            event,
            window=window,
            bot_ids={"bot"},
            aliases=["鲸鲸"],
            other_names=_other_names(case, event.sender_name),
            now=now,
        )
        matched = decision.action == expected
        expected_addressee = str((case.get("expected") or {}).get("addressee") or "")
        expected_act = str((case.get("expected") or {}).get("dialogue_act") or "")
        addressee_match = (
            not expected_addressee or decision.addressee.value == expected_addressee
        )
        act_match = (
            not expected_act or decision.dialogue_act.value == expected_act
        )
        report.evaluated += 1
        if matched:
            report.outcomes.append(CaseOutcome(
                case_id=case_id, scenario=str(case.get("scenario") or ""),
                expected_action=expected_raw, predicted_action=decision.action.value,
                matched=True, addressee_match=addressee_match,
                dialogue_act_match=act_match, reason=decision.reason,
            ))
        else:
            report.outcomes.append(CaseOutcome(
                case_id=case_id, scenario=str(case.get("scenario") or ""),
                expected_action=expected_raw, predicted_action=decision.action.value,
                matched=False, addressee_match=addressee_match,
                dialogue_act_match=act_match,
                reason=f"{decision.reason}; score={decision.score:.2f}",
            ))

        is_reply = decision.action == DecisionAction.REPLY
        expects_reply = expected == DecisionAction.REPLY
        if is_reply and expects_reply:
            tp += 1
        elif is_reply and not expects_reply:
            fp += 1
        elif not is_reply and expects_reply:
            fn += 1
        else:
            tn += 1

    report.exact_accuracy = (
        sum(1 for o in report.outcomes if o.matched) / report.evaluated
        if report.evaluated else 0.0
    )
    report.reply_precision = tp / (tp + fp) if (tp + fp) else 0.0
    report.reply_recall = tp / (tp + fn) if (tp + fn) else 0.0
    report.false_interjection_rate = fp / (fp + tn) if (fp + tn) else 0.0
    return report


def load_and_evaluate(path: str | Path,
                      resolver: AddresseeResolver | None = None) -> EvalReport:
    import json
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    cases = data.get("cases", []) if isinstance(data, Mapping) else []
    return evaluate_cases(cases, resolver=resolver)
