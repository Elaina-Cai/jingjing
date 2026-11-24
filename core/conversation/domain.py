"""Pure domain objects for the conversation/addressee layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from core.contracts import (
    Addressee,
    DecisionAction,
    DecisionRecord,
    DecisionStage,
    DialogueAct,
    TurnIntent,
)


class WindowState(StrEnum):
    IDLE = "idle"
    OPEN = "open"
    WAITING = "waiting"
    COOLDOWN = "cooldown"
    CLOSED = "closed"


@dataclass
class ConversationWindow:
    """Short-lived multi-turn window between the bot and one person."""

    platform: str
    account_id: str
    chat_id: str
    person_id: str = ""
    state: WindowState = WindowState.IDLE
    topic: str = ""
    last_bot_message_id: str = ""
    last_send_log_id: str = ""
    last_bot_asked_question: bool = False
    last_person_message_id: str = ""
    opened_at: datetime | None = None
    last_activity_at: datetime | None = None
    turn_count: int = 0
    bot_turn_count: int = 0
    confidence: float = 0.0

    def is_open(self, now: datetime | None = None,
                ttl_seconds: float = 180.0) -> bool:
        if self.state in (WindowState.IDLE, WindowState.CLOSED):
            return False
        now = now or datetime.now()
        last = self.last_activity_at or self.opened_at
        if last is None:
            return False
        return now - last <= timedelta(seconds=ttl_seconds)

    def opens_with(self, person_id: str, *, topic: str = "",
                   now: datetime | None = None) -> "ConversationWindow":
        now = now or datetime.now()
        self.person_id = person_id
        self.state = WindowState.OPEN
        self.topic = topic or self.topic
        self.opened_at = now
        self.last_activity_at = now
        self.turn_count = 0
        self.bot_turn_count = 0
        self.confidence = 0.5
        return self

    def touch_person(self, *, message_id: str = "", now: datetime | None = None
                     ) -> "ConversationWindow":
        self.state = WindowState.OPEN
        self.last_person_message_id = message_id or self.last_person_message_id
        self.last_activity_at = now or datetime.now()
        self.turn_count += 1
        return self

    def touch_bot(self, *, message_id: str = "", asked_question: bool = False,
                  now: datetime | None = None) -> "ConversationWindow":
        self.state = WindowState.WAITING if asked_question else WindowState.OPEN
        self.last_bot_message_id = message_id or self.last_bot_message_id
        self.last_bot_asked_question = bool(asked_question)
        self.last_activity_at = now or datetime.now()
        self.bot_turn_count += 1
        return self


@dataclass(frozen=True)
class AddresseeDecision:
    action: DecisionAction
    score: float = 0.0
    addressee: Addressee = Addressee.UNKNOWN
    dialogue_act: DialogueAct = DialogueAct.UNKNOWN
    turn_intent: TurnIntent = TurnIntent.UNKNOWN
    reason: str = ""
    signals: dict[str, float] = field(default_factory=dict)

    @property
    def should_reply(self) -> bool:
        return self.action == DecisionAction.REPLY

    def to_record(self) -> DecisionRecord:
        return DecisionRecord(
            stage=DecisionStage.ENGAGEMENT,
            action=self.action,
            reason=self.reason,
            confidence=max(0.0, min(1.0, self.score)),
            dialogue_act=self.dialogue_act,
            addressee=self.addressee,
            signals=self.signals,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "score": self.score,
            "addressee": self.addressee.value,
            "dialogue_act": self.dialogue_act.value,
            "turn_intent": self.turn_intent.value,
            "reason": self.reason,
            "signals": dict(self.signals),
        }
