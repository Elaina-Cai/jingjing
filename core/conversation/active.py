"""Active conversation gate (P3 canary).

This is the first step where the resolver can influence real routing.  It is
deliberately opt-in twice:

* ``YINYUE_FEATURE_CONVERSATION_ACTIVE=1``
* ``YINYUE_CONVERSATION_ACTIVE_GROUPS`` contains the chat id (or
  ``platform:chat_id``)

Only messages the legacy router already decided *not* to answer are candidates,
so explicit mentions, owner messages and all existing hard rules keep their
current behavior.  The gate only turns a silent decision into a reply; it never
cancels a legacy reply.

The gate has two rollout modes (``YINYUE_CONVERSATION_ACTIVE_MODE``):

* ``strict`` (default) -- @-mention, quote-reply to the bot, or the same
  person continuing without a third party;
* ``smart`` -- the resolver's full reply set, including name calls and
  high-confidence soft continuations.

Both modes still require the chat to be explicitly allowlisted.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Callable, Iterable, Mapping, Sequence

from core import features
from core.contracts import DecisionAction, MessageEvent
from core.contracts.legacy import legacy_to_message_event
from core.conversation.policy import (
    ACTIVE_MODE_STRICT,
    mode_allows,
    normalise_mode,
    strict_signal_name,
)
from core.conversation.resolver import AddresseeResolver
from core.conversation.store import WindowStore, make_key
from safelog import warn

log = logging.getLogger(__name__)

_ACTIVE_GROUP_ENV = "YINYUE_CONVERSATION_ACTIVE_GROUPS"
_ACTIVE_MODE_ENV = "YINYUE_CONVERSATION_ACTIVE_MODE"


def active_enabled() -> bool:
    return features.enabled(features.CONVERSATION_ACTIVE)


def active_groups() -> set[str]:
    raw = (os.environ.get(_ACTIVE_GROUP_ENV) or "").strip()
    if not raw:
        return set()
    if raw.startswith("["):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            warn("core/conversation/active.py:61 json.JSONDecodeError", err=repr(__import__("sys").exc_info()[1]))
            return set()
        if not isinstance(data, list):
            return set()
        return {str(item).strip() for item in data if str(item).strip()}
    return {
        part.strip()
        for part in raw.replace(";", ",").split(",")
        if part.strip()
    }


def group_is_active(platform: str, chat_id: str,
                    groups: set[str] | None = None) -> bool:
    groups = active_groups() if groups is None else groups
    return chat_id in groups or f"{platform}:{chat_id}" in groups


def active_mode() -> str:
    """Return the configured canary mode, failing safe to ``strict``."""
    return normalise_mode(os.environ.get(_ACTIVE_MODE_ENV))


def apply_active_decisions(
    messages: Sequence[Mapping[str, Any]] | Iterable[Mapping[str, Any]],
    decisions: Sequence[Any] | Iterable[Any],
    *,
    resolver: AddresseeResolver | None = None,
    store: WindowStore | None = None,
    bot_ids: Iterable[str] = (),
    aliases: Iterable[str] = (),
    other_names: Iterable[str] = (),
    groups: set[str] | None = None,
    platform: str = "",
    account_id: str = "",
    reply_account_id: str = "",
    recent_factory: Callable[[str, str, str], Sequence[MessageEvent]] | None = None,
    mode: str = "",
    force: bool = False,
) -> int:
    """Turn resolver-approved follow-ups into replies for allowlisted groups.

    ``mode`` defaults to the environment value; tests and the shadow report can
    override it to compare strict and smart behavior on identical inputs.
    ``recent_factory`` supplies the recent-message view so the resolver can see
    a third-party interruption; without it the strict continuation predicate
    stays conservative but cannot apply the ``no_intervening`` bonus.

    Returns the number of decisions changed from silent to reply.
    """
    if not force and not active_enabled():
        return 0
    if store is None:
        return 0

    resolver = resolver or AddresseeResolver()
    active_mode_value = normalise_mode(mode) if mode else active_mode()
    messages = list(messages)
    applied = 0

    for index, message in enumerate(messages):
        if index >= len(decisions) or not isinstance(message, Mapping):
            continue
        decision = decisions[index]
        if bool(getattr(decision, "should_reply", False)):
            continue
        if not bool(getattr(decision, "is_group", False)):
            continue
        if str(getattr(decision, "reason", "")) == "ignored_group":
            continue

        event_platform = platform or str(message.get("source") or "")
        chat_id = str(message.get("chat_id") or getattr(decision, "reply_chat_id", ""))
        if not group_is_active(event_platform, chat_id, groups):
            continue

        event = legacy_to_message_event(
            message,
            platform=event_platform,
            account_id=account_id or str(message.get("account_id") or ""),
        )
        recent: list[MessageEvent] = []
        if recent_factory is not None:
            try:
                recent = list(recent_factory(
                    event.platform, event.account_id, event.chat.chat_id,
                ))
            except Exception:
                warn("core/conversation/active.py:148 Exception", err=repr(__import__("sys").exc_info()[1]))
                recent = []
        person_key = event.sender_id or event.sender_name or event.message_id
        window = store.get(make_key(
            event.platform, event.account_id, event.chat.chat_id, person_key,
        ))
        outcome = resolver.evaluate(
            event,
            window=window,
            bot_ids=bot_ids,
            aliases=aliases,
            other_names=other_names,
            recent=recent,
        )
        if outcome.action != DecisionAction.REPLY:
            continue
        if not mode_allows(
            active_mode_value, outcome.signals, score=outcome.score,
        ):
            continue

        from core.routing.turn_decision import TurnDecision, with_active_reply

        if isinstance(decision, TurnDecision):
            # WP-06 stage B：TurnDecision 是 frozen；构建新对象写回**调用方的
            # 列表**（外部按原位置读取，行为与就地赋值逐字段一致）。
            try:
                turn_intent_value = outcome.turn_intent.value
                unknown_answer = bool(outcome.signals.get("unknown_answer"))
            except Exception:
                warn("core/conversation/active.py:177 Exception", err=repr(__import__("sys").exc_info()[1]))
                turn_intent_value, unknown_answer = "", False
            decisions[index] = with_active_reply(
                decision,
                reply_platform=event.platform,
                reply_account_id=reply_account_id,
                reply_chat_id=event.chat.chat_id,
                active_intent=str(turn_intent_value or ""),
                active_unknown_answer=unknown_answer,
            )
        else:
            # 轻量 duck 类型（历史调用面/测试）保持原地可写语义。
            decision.should_reply = True
            decision.reason = "conversation_active"
            decision.reply_platform = event.platform
            decision.reply_account = "self"
            decision.reply_account_id = reply_account_id or getattr(
                decision, "reply_account_id", "",
            )
            decision.reply_chat_id = event.chat.chat_id
            decision.is_group = True
            # Attributes are observability-only; only duck types can carry them.
            try:
                decision.active_mode = active_mode_value
                decision.active_signal = strict_signal_name(
                    outcome.signals, score=outcome.score,
                )
                decision.active_score = outcome.score
                decision.active_intent = outcome.turn_intent.value
                decision.active_unknown_answer = bool(
                    outcome.signals.get("unknown_answer")
                )
            except Exception:
                warn("core/conversation/active.py:209 Exception", err=repr(__import__("sys").exc_info()[1]))
        applied += 1

    if applied:
        log.info(
            "conversation_active_applied count=%d mode=%s platform=%s",
            applied, active_mode_value, platform,
        )
    return applied
