"""Conversation state and addressee-resolution primitives.

This package is the P3 shadow core: it decides *whether a message is addressed
to the bot* and *what kind of utterance it is*.  It is deliberately pure
(contracts + stdlib only) so it can be tested and compared against the legacy
router before it has any production side effects.
"""

from __future__ import annotations

from core.conversation.domain import AddresseeDecision, ConversationWindow, WindowState
from core.conversation.active import (
    active_enabled,
    active_groups,
    active_mode,
    apply_active_decisions,
    group_is_active,
)
from core.conversation.eval import EvalReport, evaluate_cases, load_and_evaluate
from core.conversation.resolver import AddresseeResolver
from core.conversation.reconcile import reconcile_bot_message_ids
from core.conversation.shadow import (
    ConversationShadowObserver,
    get_shadow_observer,
    reset_shadow_observer,
)

# ``report`` and ``tune`` are intentionally not imported here: they are CLI
# entry points (``python -m core.conversation.report`` / ``.tune``), and
# importing them from the package would trigger runpy's "already imported"
# warning during those commands.

__all__ = [
    "AddresseeDecision",
    "AddresseeResolver",
    "ConversationShadowObserver",
    "ConversationWindow",
    "EvalReport",
    "WindowState",
    "active_enabled",
    "active_groups",
    "active_mode",
    "apply_active_decisions",
    "evaluate_cases",
    "get_shadow_observer",
    "group_is_active",
    "load_and_evaluate",
    "reconcile_bot_message_ids",
    "reset_shadow_observer",
]
