"""Pure canary policy helpers (P3).

The active gate can run in two modes:

``strict`` (default)
    Only the three signals operators asked for: an explicit @-mention, a
    quote-reply to a known bot message, or the same person continuing an open
    window without a third party interrupting.

``smart``
    Trust the full resolver reply set (name calls, deictic follow-ups and
    high-confidence continuations included).  This is the second phase after
    the strict canary has stayed clean.

Keeping the predicate here -- instead of inside the environment-driven gate --
lets the shadow report measure what strict mode *would* have done before the
gate is switched on.
"""

from __future__ import annotations

import os
from typing import Any, Mapping
from safelog import warn

ACTIVE_MODE_STRICT = "strict"
ACTIVE_MODE_SMART = "smart"
ACTIVE_MODES = frozenset({ACTIVE_MODE_STRICT, ACTIVE_MODE_SMART})
STRICT_MIN_SCORE_ENV = "YINYUE_CONVERSATION_STRICT_MIN_SCORE"


def normalise_mode(raw: Any) -> str:
    value = str(raw or "").strip().lower()
    return value if value in ACTIVE_MODES else ACTIVE_MODE_STRICT


def strict_min_score() -> float:
    """Optional safety floor for strict-mode same-person continuations.

    Real traces showed soft continuations scoring 0.55-0.65 that were clearly
    side-chat, while answers to the bot's question can also score around
    0.65.  Operators can raise this without changing the resolver itself.
    """
    raw = (os.environ.get(STRICT_MIN_SCORE_ENV) or "").strip()
    if not raw:
        return 0.0
    try:
        value = float(raw)
    except ValueError:
        warn("core/conversation/policy.py:48 ValueError", err=repr(__import__("sys").exc_info()[1]))
        return 0.0
    if value != value:
        return 0.0
    return max(0.0, min(1.0, value))


def _truthy(signals: Mapping[str, Any], key: str) -> bool:
    return bool(signals.get(key))


def strict_reply_allowed(
    signals: Mapping[str, Any] | None,
    *,
    score: float | None = None,
) -> bool:
    if not signals:
        return False
    # ``reply_link_to_bot`` (a real reply pointer at one of her messages), not
    # ``reply_to_bot``: the latter is also true for merely containing a
    # ``[引用 鲸鲸: ...]`` header, and quoting her is not the same as talking to
    # her -- the same confusion the resolver's hard-positive block was fixed
    # for.  Leaving the broad signal here would have re-opened that hole on the
    # strict path while the resolver stayed correct.
    if _truthy(signals, "explicit_mention") or _truthy(signals, "reply_link_to_bot"):
        return True
    # A quote *plus* a question is a genuine question put to her, and the
    # resolver replies to it too -- keep the two gates consistent.
    if _truthy(signals, "quote_to_bot") and _truthy(signals, "expects_reply"):
        return True
    same_person_continuation = (
        _truthy(signals, "window_open")
        and _truthy(signals, "same_sender")
        and not _truthy(signals, "third_party")
    )
    if not same_person_continuation:
        return False
    if score is not None:
        try:
            if float(score) + 1e-9 < strict_min_score():
                return False
        except (TypeError, ValueError):
            warn("core/conversation/policy.py:89 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
    return True


def strict_signal_name(
    signals: Mapping[str, Any] | None,
    *,
    score: float | None = None,
) -> str:
    if not signals:
        return "none"
    if _truthy(signals, "explicit_mention"):
        return "mention"
    if _truthy(signals, "reply_link_to_bot"):
        return "reply"
    if _truthy(signals, "quote_to_bot"):
        return "quote"
    if strict_reply_allowed(signals, score=score):
        return "same_person"
    return "none"


def mode_allows(
    mode: str,
    signals: Mapping[str, Any] | None,
    *,
    score: float | None = None,
) -> bool:
    return (
        normalise_mode(mode) == ACTIVE_MODE_SMART
        or strict_reply_allowed(signals, score=score)
    )
