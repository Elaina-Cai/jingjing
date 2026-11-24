"""Quoting her is not the same as talking to her.

Why this file exists
--------------------
In the group she answered *"怎么一看到你就觉得难崩"* as though it were a
question put to her.  It was not: the room was ribbing P5, and the line
happened to quote one of 鲸鲸's own stickers back at her.

The old policy made quoting her a **hard positive**:

    addressed_to_bot = (explicit_mention or reply_to_bot or pat_on_bot)
    if addressed_to_bot: return REPLY, score=1.0

``reply_to_bot`` was true for a ``[引用 鲸鲸: ...]`` header alone, so that line
got score 1.0 and an unconditional reply.  ``banter`` was recorded as 1.0 and
then never used to damp anything.

The fix separates two different things:

* **a reply pointer** at one of her messages -- the sender picked that message
  to answer.  Still a hard positive.
* **a quote header** -- weak evidence.  It feeds the soft score, where banter
  and third_party can outweigh it.

Two legitimate behaviours had to survive, and they are pinned below: quoting
her *to ask her something* still replies, and a real reply-link still replies.

Run directly: python tests/test_quote_is_not_address.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _resolver():
    from core.conversation.resolver import AddresseeResolver

    return AddresseeResolver(apply_overrides=False)


def _decide(**signals):
    from core.conversation.signals import AddresseeSignals

    return _resolver().decide(AddresseeSignals(**signals))


# --------------------------------------------------------- the incident itself

def test_quoted_jab_is_not_answered() -> None:
    """The exact 难崩 case: quotes her, is banter, was aimed at P5."""
    d = _decide(
        quote_to_bot=True, reply_to_bot=True,
        banter=True, deictic_reference=True,
    )
    assert d.action.value != "reply", f"still answering a jab: {d.action.value}"
    assert d.score < 0.75, d.score


def test_banter_now_actually_damps() -> None:
    """``banter`` used to be recorded and ignored.  It has to move the score."""
    with_banter = _decide(quote_to_bot=True, reply_to_bot=True, banter=True)
    without = _decide(quote_to_bot=True, reply_to_bot=True)
    assert with_banter.score < without.score, (
        f"banter no longer damps: {with_banter.score} vs {without.score}"
    )


def test_quote_alone_is_not_a_hard_positive() -> None:
    """A bare quote must not be labelled as addressed to her."""
    d = _decide(quote_to_bot=True, reply_to_bot=True)
    assert d.score < 1.0, "a bare quote still scores a perfect 1.0"


# --------------------------------------------------------- what must still work

def test_quoted_question_to_her_is_answered() -> None:
    """`[引用 鲸鲸: 在呢] 今天天气如何` is a real question put to her."""
    d = _decide(quote_to_bot=True, reply_to_bot=True, expects_reply=True)
    assert d.action.value == "reply", f"{d.action.value} score={d.score}"
    assert d.addressee.value == "bot", d.addressee.value


def test_reply_link_to_her_is_still_a_hard_positive() -> None:
    """A real reply-pointer is addressing, quote-header or not."""
    d = _decide(reply_link_to_bot=True)
    assert d.action.value == "reply", d.action.value
    assert d.score == 1.0, d.score


def test_explicit_mention_unchanged() -> None:
    d = _decide(explicit_mention=True)
    assert d.action.value == "reply" and d.score == 1.0


def test_pat_on_her_unchanged() -> None:
    d = _decide(pat_on_bot=True, name_call=True)
    assert d.action.value == "reply" and d.score == 1.0


def test_quote_of_someone_else_still_dropped() -> None:
    d = _decide(quote_to_other=True, other_addressed=True)
    assert d.action.value == "drop", d.action.value


# --------------------------------------------------------- signal-level wiring

def test_reply_link_is_distinct_from_quote() -> None:
    """The two signals must be separable, or the policy cannot tell them apart."""
    from core.conversation.signals import AddresseeSignals

    a = AddresseeSignals(quote_to_bot=True, reply_to_bot=True)
    assert a.reply_link_to_bot is False, (
        "a quote header must not set reply_link_to_bot"
    )
    b = AddresseeSignals(reply_link_to_bot=True, reply_to_bot=True)
    assert b.quote_to_bot is False


def test_signals_are_exported_in_the_dict() -> None:
    """Traces/replay read ``as_dict()``; the new signal must appear there."""
    d = _decide(reply_link_to_bot=True)
    assert "reply_link_to_bot" in d.signals, sorted(d.signals)
    assert d.signals["reply_link_to_bot"] == 1.0


# ------------------------------------------- the *other* gate (strict mode)

def test_strict_gate_uses_the_same_distinction() -> None:
    """``policy.strict_reply_allowed`` must not re-open the quote hole.

    There are two independent gates on this decision.  Fixing only the
    resolver would have left the strict path still admitting a bare quote.
    """
    from core.conversation.policy import strict_reply_allowed, strict_signal_name

    bare_quote = {
        "quote_to_bot": 1.0, "reply_to_bot": 1.0,
        "banter": 1.0, "expects_reply": 0.0,
    }
    assert not strict_reply_allowed(bare_quote), (
        "strict mode still admits a bare quote of her"
    )
    assert strict_signal_name(bare_quote) == "quote", strict_signal_name(bare_quote)

    # A real reply pointer is still admitted.
    assert strict_reply_allowed({"reply_link_to_bot": 1.0, "reply_to_bot": 1.0})
    # A quoted question is still admitted, matching the resolver.
    assert strict_reply_allowed({"quote_to_bot": 1.0, "expects_reply": 1.0})
    # An explicit @ is still admitted.
    assert strict_reply_allowed({"explicit_mention": 1.0})


def test_strict_gate_rejects_a_quoted_jab() -> None:
    """The 难崩 message must not slip through the strict gate either."""
    from core.conversation.policy import strict_reply_allowed

    incident = {
        "quote_to_bot": 1.0, "reply_to_bot": 1.0,
        "banter": 1.0, "deictic_reference": 1.0,
        "expects_reply": 0.0,
    }
    assert not strict_reply_allowed(incident), (
        "the quoted jab still passes the strict gate"
    )


def main() -> bool:
    tests = [
        test_quoted_jab_is_not_answered,
        test_banter_now_actually_damps,
        test_quote_alone_is_not_a_hard_positive,
        test_quoted_question_to_her_is_answered,
        test_reply_link_to_her_is_still_a_hard_positive,
        test_explicit_mention_unchanged,
        test_pat_on_her_unchanged,
        test_quote_of_someone_else_still_dropped,
        test_reply_link_is_distinct_from_quote,
        test_signals_are_exported_in_the_dict,
        test_strict_gate_uses_the_same_distinction,
        test_strict_gate_rejects_a_quoted_jab,
    ]
    failed = []
    for test in tests:
        try:
            test()
            print(f"  ok   {test.__name__}")
        except Exception as exc:
            failed.append(test.__name__)
            print(f"  FAIL {test.__name__}: {type(exc).__name__}: {exc}")
    print("\nall passed" if not failed else f"\nfailed: {', '.join(failed)}")
    return not failed


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
