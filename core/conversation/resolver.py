"""Shadow addressee resolver (P3).

Pure decision logic: given a ``MessageEvent``, an optional conversation window
and recent context, produce a ``REPLY / OBSERVE / DROP / CLOSE`` decision.  It
has no production side effects and is wired into the router only after its
precision has been validated in shadow mode.
"""

from __future__ import annotations

import logging
from typing import Iterable, Sequence

from core.contracts import (
    Addressee,
    DecisionAction,
    DialogueAct,
    MessageEvent,
    TurnIntent,
)
from core.conversation.config import load_overrides
from core.conversation.domain import AddresseeDecision, ConversationWindow
from core.conversation.signals import AddresseeSignals, extract_signals
from safelog import warn

log = logging.getLogger(__name__)

_DEFAULT_REPLY_THRESHOLD = 0.75
_DEFAULT_OPEN_REPLY_THRESHOLD = 0.55
_DEFAULT_OBSERVE_THRESHOLD = 0.35

# Deterministic starting weights.  They are intentionally simple: the first
# goal is a replayable baseline, not a tuned model.  Calibration can change
# these without touching the decision flow.
_WEIGHTS: dict[str, float] = {
    "window_open": 0.05,
    "same_sender": 0.20,
    "no_intervening": 0.10,
    "bot_asked_question": 0.20,
    "deictic_reference": 0.10,
    "expects_reply": 0.10,
    "time_decay": 0.10,
    # 曾有一个恒 0 的话题相似度权重，已退役（owner：N3 焦点状态；静态守卫见
    # ``tests/test_signal_weights.py``）。要重新引入必须先有真实生产者，
    # 不许再放一个恒 0 的权重。
    # Quoting the bot is evidence, not proof: it adds to the soft score so a
    # quoted question still gets answered, but it can be outweighed by banter
    # or by the message addressing somebody else.  It used to be a hard REPLY
    # at 1.0 and that is what made her answer lines aimed at other people.
    "reply_to_bot": 0.30,
    "third_party": -0.30,
    "topic_shift": -0.30,
    # Ribbing / joking aloud.  Weak on its own (a friendly joke to her is still
    # a message to her) but enough together with ``reply_to_bot`` to keep a
    # quoted jab from clearing the reply threshold.
    "banter": -0.25,
    "media": -0.15,
}


class AddresseeResolver:
    def __init__(
        self,
        *,
        weights: dict[str, float] | None = None,
        reply_threshold: float | None = None,
        open_reply_threshold: float | None = None,
        observe_threshold: float | None = None,
        apply_overrides: bool = True,
    ):
        overrides = load_overrides() if apply_overrides else None
        self.weights = dict(_WEIGHTS)
        if overrides:
            for key, value in overrides.weights.items():
                if key in self.weights:
                    self.weights[key] = value
                else:
                    log.warning("conversation_weight_unknown weight=%s", key)
        # Programmatic weights win over environment/file tuning.  Tuning itself
        # passes its candidate weights here with ``apply_overrides=False``.
        if weights:
            for key, value in weights.items():
                self.weights[key] = float(value)

        self.reply_threshold = self._threshold(
            reply_threshold,
            overrides.reply_threshold if overrides else None,
            _DEFAULT_REPLY_THRESHOLD,
        )
        self.open_reply_threshold = self._threshold(
            open_reply_threshold,
            overrides.open_reply_threshold if overrides else None,
            _DEFAULT_OPEN_REPLY_THRESHOLD,
        )
        self.observe_threshold = self._threshold(
            observe_threshold,
            overrides.observe_threshold if overrides else None,
            _DEFAULT_OBSERVE_THRESHOLD,
        )

    @staticmethod
    def _threshold(explicit: float | None, override: float | None,
                   default: float) -> float:
        value = explicit if explicit is not None else override
        if value is None:
            value = default
        try:
            number = float(value)
        except (TypeError, ValueError):
            warn("core/conversation/resolver.py:108 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
            return default
        if number < 0.0 or number > 1.0:
            return default
        return number

    def config_snapshot(self) -> dict:
        """Effective weights/thresholds, useful for diagnostics/traces."""
        return {
            "weights": dict(sorted(self.weights.items())),
            "reply_threshold": self.reply_threshold,
            "open_reply_threshold": self.open_reply_threshold,
            "observe_threshold": self.observe_threshold,
        }

    # ------------------------------------------------------------------ public
    def evaluate(
        self,
        event: MessageEvent,
        *,
        window: ConversationWindow | None = None,
        bot_ids: Iterable[str] = (),
        aliases: Iterable[str] = (),
        other_names: Iterable[str] = (),
        bot_message_ids: Iterable[str] = (),
        recent: Sequence[MessageEvent] = (),
        now=None,
        ttl_seconds: float = 180.0,
    ) -> AddresseeDecision:
        signals = extract_signals(
            event,
            window=window,
            bot_ids=bot_ids,
            aliases=aliases,
            other_names=other_names,
            bot_message_ids=bot_message_ids,
            recent=recent,
            now=now,
            ttl_seconds=ttl_seconds,
        )
        return self.decide(signals)

    # ------------------------------------------------------------------ public
    def decide(self, signals: AddresseeSignals) -> AddresseeDecision:
        """Apply the deterministic policy to already-extracted signals.

        Keeping this separate from ``evaluate`` lets offline replay/tuning
        re-run the exact production decision flow over stored shadow signals.
        """
        # 1. Hard negatives.  Explicit address to the bot always wins over the
        #    "someone else was named" heuristic.
        #
        #    A quote header is deliberately NOT a hard positive on its own.
        #    Quoting somebody is not the same as talking to them: "怎么一看到你
        #    就觉得难崩" quoted 鲸鲸's own sticker back at her while the room was
        #    ribbing P5, and because a quote granted a hard REPLY at score 1.0
        #    she answered a line that was not for her.  ``reply_link_to_bot``
        #    (a real reply-pointer at one of her messages) still is one, because
        #    that genuinely is a reply to her.
        #
        #    Everything else remains reachable: an @, a name call, a pat, or a
        #    quoted question are all handled below, and a bare quote now feeds
        #    the soft score where banter and third_party can damp it.
        addressed_to_bot = (
            signals.explicit_mention
            or signals.pat_on_bot
            or signals.reply_link_to_bot
        )
        if signals.other_addressed and not addressed_to_bot:
            return self._decision(
                DecisionAction.DROP, signals,
                addressee=Addressee.PERSON,
                dialogue_act=DialogueAct.OTHER_ADDRESSED,
                reason="other_addressed",
            )
        if signals.broadcast:
            return self._decision(
                DecisionAction.DROP, signals,
                addressee=Addressee.GROUP,
                dialogue_act=DialogueAct.BROADCAST,
                reason="broadcast",
            )

        # 2. Hard positives.
        if addressed_to_bot:
            return self._decision(
                DecisionAction.REPLY, signals, score=1.0,
                addressee=Addressee.BOT,
                dialogue_act=self._dialogue_act(signals),
                reason="explicitly_addressed",
            )
        if signals.name_call and (signals.expects_reply or signals.window_open):
            score = max(0.8, self._score(signals) + 0.35)
            return self._decision(
                DecisionAction.REPLY, signals, score=score,
                addressee=Addressee.BOT,
                dialogue_act=self._dialogue_act(signals),
                reason="name_call",
            )
        # Quoting her *and* asking something is a real question put to her:
        # "[引用 鲸鲸: 在呢] 今天天气如何".  The quote alone is weak (see the
        # hard-positive block), but the two together are unambiguous.  Banter
        # is excluded -- "怎么一看到你就觉得难崩" quotes her and is a rhetorical
        # jab at a third party, not a question to her.
        if (
            signals.quote_to_bot
            and signals.expects_reply
            and not signals.banter
            and not signals.other_addressed
        ):
            return self._decision(
                DecisionAction.REPLY, signals,
                score=max(0.8, self._score(signals)),
                addressee=Addressee.BOT,
                dialogue_act=self._dialogue_act(signals),
                reason="quoted_question_to_bot",
            )

        # 3. Closing / acknowledgement handling.
        if signals.closing:
            return self._decision(
                DecisionAction.CLOSE, signals,
                addressee=Addressee.BOT,
                dialogue_act=DialogueAct.CLOSING,
                reason="closing",
            )
        if signals.ack and not signals.expects_reply:
            action = DecisionAction.CLOSE if signals.window_open else DecisionAction.DROP
            return self._decision(
                action, signals,
                addressee=Addressee.BOT,
                dialogue_act=DialogueAct.ACK,
                reason="acknowledgement",
            )

        # 4. Soft score for the remaining ambiguous messages.
        score = round(max(0.0, min(1.0, self._score(signals))), 6)
        threshold = (
            self.open_reply_threshold if signals.window_open
            else self.reply_threshold
        )
        if score >= threshold - 1e-9:
            action = DecisionAction.REPLY
            reason = "continuation" if signals.window_open else "high_confidence"
        elif score >= self.observe_threshold:
            action = DecisionAction.OBSERVE
            reason = "ambiguous_observe"
        else:
            action = DecisionAction.DROP
            reason = "low_confidence"
        return self._decision(
            action, signals, score=score,
            addressee=self._addressee(signals, action),
            dialogue_act=self._dialogue_act(signals),
            reason=reason,
        )

    # ------------------------------------------------------------------ helpers
    def _score(self, s: AddresseeSignals) -> float:
        total = 0.0
        for name, weight in self.weights.items():
            value = getattr(s, name, 0.0)
            if isinstance(value, bool):
                value = 1.0 if value else 0.0
            total += weight * float(value)
        return total

    @staticmethod
    def _addressee(signals: AddresseeSignals,
                   action: DecisionAction) -> Addressee:
        if signals.explicit_mention or signals.reply_link_to_bot or signals.name_call:
            return Addressee.BOT
        if signals.other_addressed:
            return Addressee.PERSON
        # A plain quote header no longer forces ``BOT`` -- it is weak evidence
        # and must not relabel somebody else's joke as a message to her.  The
        # addressee is only inferred from a quote when the room context agrees
        # (an open window with the same person).
        if (signals.quote_to_bot or signals.reply_to_bot) and signals.window_open:
            return Addressee.BOT
        if signals.broadcast:
            return Addressee.GROUP
        if signals.window_open and signals.same_sender:
            return Addressee.BOT
        return Addressee.UNKNOWN

    @staticmethod
    def _dialogue_act(signals: AddresseeSignals) -> DialogueAct:
        if signals.broadcast:
            return DialogueAct.BROADCAST
        if signals.other_addressed:
            return DialogueAct.OTHER_ADDRESSED
        if signals.closing:
            return DialogueAct.CLOSING
        if signals.ack:
            return DialogueAct.ACK
        if signals.unknown_answer:
            return DialogueAct.ANSWER_TO_BOT
        if signals.reply_to_bot and not signals.expects_reply:
            return DialogueAct.ANSWER_TO_BOT
        if signals.expects_reply:
            if signals.topic_shift:
                return DialogueAct.QUESTION_TO_GROUP
            if signals.explicit_mention or signals.reply_to_bot or signals.window_open:
                return DialogueAct.QUESTION_TO_BOT
            return DialogueAct.QUESTION_TO_GROUP
        if signals.media:
            return DialogueAct.MEDIA
        if signals.window_open and signals.same_sender:
            return DialogueAct.ANSWER_TO_BOT
        return DialogueAct.UNKNOWN

    @staticmethod
    def _turn_intent(signals: AddresseeSignals) -> TurnIntent:
        """Classify the utterance itself (answer / question / ack / ...).

        This is deliberately independent from ``addressee``: a question asked
        to the group is still ``QUESTION``.  The loop guard cares about
        ``ANSWER`` plus ``unknown_answer``.
        """
        if signals.ack:
            return TurnIntent.ACK
        if signals.closing:
            return TurnIntent.STATEMENT
        if signals.command:
            return TurnIntent.COMMAND
        if signals.other_addressed or signals.broadcast or signals.media:
            return TurnIntent.STATEMENT
        if signals.expects_reply:
            return TurnIntent.QUESTION
        if signals.banter:
            return TurnIntent.BANTER
        if (
            signals.reply_to_bot
            or signals.quote_to_bot
            or (
                signals.window_open
                and signals.same_sender
                and signals.bot_asked_question
            )
        ):
            return TurnIntent.ANSWER
        return TurnIntent.STATEMENT

    @staticmethod
    def _decision(
        action: DecisionAction,
        signals: AddresseeSignals,
        *,
        score: float = 0.0,
        addressee: Addressee = Addressee.UNKNOWN,
        dialogue_act: DialogueAct = DialogueAct.UNKNOWN,
        turn_intent: TurnIntent | None = None,
        reason: str = "",
    ) -> AddresseeDecision:
        return AddresseeDecision(
            action=action,
            score=score,
            addressee=addressee,
            dialogue_act=dialogue_act,
            turn_intent=(
                turn_intent
                if turn_intent is not None
                else AddresseeResolver._turn_intent(signals)
            ),
            reason=reason,
            signals=signals.as_dict(),
        )
