"""Canonical decision contracts for the conversation pipeline.

The legacy router returns a routing decision.  The target pipeline returns a
staged decision record: whether to speak (engagement), who is addressed, what
kind of utterance this is, and which policy rules participated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping


class DecisionStage(StrEnum):
    SAFETY = "safety"
    PRE_GATE = "pre_gate"
    ENGAGEMENT = "engagement"
    CONDUCT = "conduct"
    TOOL = "tool"
    DELIVERY = "delivery"


class DecisionAction(StrEnum):
    ALLOW = "allow"
    REPLY = "reply"
    OBSERVE = "observe"
    DROP = "drop"
    DEFER = "defer"
    DENY = "deny"
    REWRITE = "rewrite"
    REQUIRE = "require"
    CLOSE = "close"
    LOG_ONLY = "log_only"


class DialogueAct(StrEnum):
    GREETING = "greeting"
    QUESTION_TO_BOT = "question_to_bot"
    QUESTION_TO_GROUP = "question_to_group"
    ANSWER_TO_BOT = "answer_to_bot"
    ACK = "ack"
    CLOSING = "closing"
    COMMAND = "command"
    BANTER = "banter"
    BROADCAST = "broadcast"
    OTHER_ADDRESSED = "other_addressed"
    MEDIA = "media"
    UNKNOWN = "unknown"


class TurnIntent(StrEnum):
    """What the current turn is doing, independent of who it addresses.

    ``dialogue_act`` answers "who/which act"; ``turn_intent`` answers
    "answer vs question vs ack vs command ...".  The loop guard uses
    ``ANSWER + unknown_answer`` to stop the bot asking another question after
    the user already said they do not know.
    """

    ANSWER = "answer"
    ACK = "ack"
    QUESTION = "question"
    BANTER = "banter"
    COMMAND = "command"
    STATEMENT = "statement"
    UNKNOWN = "unknown"


class Addressee(StrEnum):
    BOT = "bot"
    PERSON = "person"
    GROUP = "group"
    MULTIPLE = "multiple"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class DecisionRecord:
    """One staged policy decision, suitable for tracing and replay."""

    stage: DecisionStage
    action: DecisionAction
    reason: str = ""
    confidence: float = 0.0
    dialogue_act: DialogueAct = DialogueAct.UNKNOWN
    addressee: Addressee = Addressee.UNKNOWN
    rule_ids: tuple[str, ...] = ()
    signals: Mapping[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage.value,
            "action": self.action.value,
            "reason": self.reason,
            "confidence": self.confidence,
            "dialogue_act": self.dialogue_act.value,
            "addressee": self.addressee.value,
            "rule_ids": list(self.rule_ids),
            "signals": dict(self.signals),
        }
