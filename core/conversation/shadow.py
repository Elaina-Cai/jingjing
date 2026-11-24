"""Shadow conversation observer.

Runs the P3 addressee resolver on real messages without changing routing or
sending.  It keeps an in-memory conversation window so follow-up decisions can
be compared against the legacy router, and writes one trace row per decision.

The observer is a bridge, not the final conversation engine:

* it is disabled unless ``YINYUE_FEATURE_CONVERSATION_SHADOW=1``;
* windows use a ``WindowStore`` (in-memory in tests, meta-backed in runtime);
* bot message ids are fed back by ``mark_bot_reply`` once the outbox/evidence
  loop reports them, so ``reply_to_bot`` can become precise.
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from typing import Any, Iterable, Mapping, Sequence

from core import features
from core.contracts import DecisionAction, MessageEvent
from core.contracts.legacy import legacy_to_message_event
from core.conversation.domain import AddresseeDecision, ConversationWindow, WindowState
from core.conversation.resolver import AddresseeResolver
from core.conversation.signals import text_looks_like_question
from core.conversation.store import (
    InMemoryWindowStore,
    WindowKey,
    WindowStore,
    make_key,
)
from core.observability.decision_trace import record_decision
from safelog import warn

log = logging.getLogger(__name__)

_OBSERVER: "ConversationShadowObserver | None" = None


class ConversationShadowObserver:
    def __init__(
        self,
        resolver: AddresseeResolver | None = None,
        *,
        store: WindowStore | None = None,
        recent_per_chat: int = 30,
        ttl_seconds: float = 180.0,
    ):
        self._resolver = resolver or AddresseeResolver()
        self._ttl_seconds = ttl_seconds
        self._store = store or InMemoryWindowStore()
        self._recent: dict[tuple[str, str, str], deque[MessageEvent]] = defaultdict(
            lambda: deque(maxlen=max(1, recent_per_chat))
        )

    def reset(self) -> None:
        self._store.clear()
        self._recent.clear()

    # ------------------------------------------------------------------ public
    def observe_batch(
        self,
        messages: Sequence[Mapping[str, Any]] | Iterable[Mapping[str, Any]],
        decisions: Sequence[Any] | Iterable[Any],
        *,
        bot_ids: Iterable[str] = (),
        aliases: Iterable[str] = (),
        other_names: Iterable[str] = (),
        platform: str = "",
        account_id: str = "",
        force: bool = False,
        trace_path=None,
    ) -> list[AddresseeDecision]:
        if not force and not features.enabled(features.CONVERSATION_SHADOW):
            return []

        messages = list(messages)
        decisions = list(decisions)
        results: list[AddresseeDecision] = []

        for index, message in enumerate(messages):
            if not isinstance(message, Mapping):
                continue
            legacy = decisions[index] if index < len(decisions) else None
            event = legacy_to_message_event(
                message,
                platform=platform or str(message.get("source") or ""),
                account_id=account_id or str(message.get("account_id") or ""),
            )
            decision = self._observe_one(
                event,
                legacy,
                bot_ids=bot_ids,
                aliases=aliases,
                other_names=other_names,
                force=force,
                trace_path=trace_path,
            )
            results.append(decision)
        return results

    # ----------------------------------------------------------------- private
    def _observe_one(
        self,
        event: MessageEvent,
        legacy: Any,
        *,
        bot_ids: Iterable[str],
        aliases: Iterable[str],
        other_names: Iterable[str],
        force: bool,
        trace_path,
    ) -> AddresseeDecision:
        chat_key = (event.platform, event.account_id, event.chat.chat_id)
        person_key = event.sender_id or event.sender_name or event.message_id
        window_key = make_key(*chat_key, person_key)
        window = self._store.get(window_key)
        if window is not None and not window.is_open(event.created_at, self._ttl_seconds):
            self._store.delete(window_key)
            window = None

        decision = self._resolver.evaluate(
            event,
            window=window,
            bot_ids=bot_ids,
            aliases=aliases,
            other_names=other_names,
            recent=list(self._recent[chat_key]),
            now=event.created_at,
            ttl_seconds=self._ttl_seconds,
        )

        self._record(event, legacy, decision, window, force=force, trace_path=trace_path)
        self._update_window(window_key, window, event, legacy, decision)
        self._recent[chat_key].append(event)
        return decision

    def _record(
        self,
        event: MessageEvent,
        legacy: Any,
        decision: AddresseeDecision,
        window: ConversationWindow | None,
        *,
        force: bool,
        trace_path,
    ) -> None:
        row = {
            "kind": "addressee_shadow",
            "event": event.to_dict(),
            "legacy": {
                "should_reply": bool(getattr(legacy, "should_reply", False)),
                "reason": str(getattr(legacy, "reason", "")),
            },
            "shadow": decision.to_dict(),
            "window": {
                "person_id": window.person_id if window else "",
                "state": window.state.value if window else WindowState.IDLE.value,
                "last_bot_message_id": window.last_bot_message_id if window else "",
                "last_bot_asked_question": window.last_bot_asked_question if window else False,
                "turn_count": window.turn_count if window else 0,
            },
        }
        # Reaching this method means the shadow feature flag (or an explicit
        # test force) already authorised observation, so the row must be
        # written even when the general decision-trace flag is off.
        record_decision(row, force=True, path=trace_path)

    def _update_window(
        self,
        window_key: WindowKey,
        window: ConversationWindow | None,
        event: MessageEvent,
        legacy: Any,
        decision: AddresseeDecision,
    ) -> None:
        person_id = window_key[-1]
        if bool(getattr(legacy, "should_reply", False)):
            if window is None:
                window = ConversationWindow(
                    platform=event.platform,
                    account_id=event.account_id,
                    chat_id=event.chat.chat_id,
                )
            window.opens_with(person_id, now=event.created_at)
            # The legacy router does not expose the bot's reply id or whether
            # it asked a question; ``mark_bot_reply`` fills those in after the
            # outbound send succeeds.
            window.last_bot_message_id = ""
            window.last_bot_asked_question = False
            self._store.save(window_key, window)
            return

        if window is None:
            return
        if decision.action == DecisionAction.CLOSE:
            window.state = WindowState.CLOSED
            self._store.delete(window_key)
        elif decision.action in (DecisionAction.REPLY, DecisionAction.OBSERVE):
            window.touch_person(message_id=event.message_id, now=event.created_at)
            self._store.save(window_key, window)
        elif decision.signals.get("other_addressed") or decision.signals.get("broadcast"):
            window.state = WindowState.COOLDOWN
            self._store.save(window_key, window)

    def mark_bot_reply(
        self,
        *,
        platform: str,
        account_id: str,
        chat_id: str,
        person_id: str,
        reply: str = "",
        message_id: str = "",
        send_log_id: str = "",
        now=None,
    ) -> ConversationWindow:
        """Feed one successful outbound reply back into the shadow window."""
        window_key = make_key(platform, account_id, chat_id, person_id)
        window = self._store.get(window_key)
        if window is None:
            window = ConversationWindow(
                platform=platform,
                account_id=account_id,
                chat_id=chat_id,
            )
            window.opens_with(person_id, now=now)
        asked_question = text_looks_like_question(reply)
        if send_log_id:
            window.last_send_log_id = str(send_log_id)
        window.touch_bot(
            message_id=message_id or window.last_bot_message_id,
            asked_question=asked_question,
            now=now,
        )
        self._store.save(window_key, window)
        return window

    def recent_events(
        self, platform: str, account_id: str, chat_id: str,
    ) -> list[MessageEvent]:
        """Recent events for one chat, used by the active gate's interruption
        check.  Returns a copy so callers cannot mutate observer state."""
        return list(self._recent[(platform, account_id, chat_id)])

    @property
    def store(self) -> WindowStore:
        return self._store


def get_shadow_observer() -> ConversationShadowObserver:
    global _OBSERVER
    if _OBSERVER is None:
        store = None
        try:
            from core.terminal.store_port import get_meta_repo
            from core.conversation.store import MetaWindowStore
            repo = getattr(get_meta_repo(), "conversation_windows", None)
            if repo is not None:
                store = MetaWindowStore(repo)
        except Exception:
            warn("core/conversation/shadow.py:261 Exception", err=repr(__import__("sys").exc_info()[1]))
            store = None
        _OBSERVER = ConversationShadowObserver(store=store)
    return _OBSERVER


def reset_shadow_observer() -> None:
    global _OBSERVER
    _OBSERVER = None
