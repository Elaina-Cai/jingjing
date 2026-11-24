"""Who is allowed to carry affect at all.

Scope is a *policy* decision, kept here as a pure function so it can be
reasoned about and tested without a database or a model:

* the owner always counts;
* in a **group**, everyone counts (operator decision, 2026-09-20);
* in a **private** chat, a person counts only when she has a real relationship
  with them (亲密 / 熟悉); strangers and anyone with no relationship row still
  get no affect block.

What this does and does not change
----------------------------------
Opening the group case removes the "flat register toward strangers" rule there:
her tone now moves with her mood for every group member she answers.  It does
*not* touch the privacy property, which comes from a different place -- a block
is only ever rendered for the person being addressed (see
``AffectManager.build`` and ``_affect_target``), so one person's state still
cannot appear in a reply to somebody else.

A person with no ``relationships`` row at all still gets nothing: affect is
stored on that row and the writer never inserts one.  Group speakers get their
own row the first time they speak, so this shows up as a one-turn delay for
someone brand new, not as a permanent exclusion.
"""

from __future__ import annotations

# Tiers that may carry affect in a private chat.  The values come from
# ``core.speech.turn.relationship_facts`` / the ``relationships`` table.
TRACKED_TIERS = frozenset({"亲密", "熟悉"})


def in_scope(*, is_owner: bool, relation_tier: str = "",
             in_group: bool = False) -> bool:
    """Whether this turn's addressee may receive an affect block."""
    if is_owner:
        return True
    if in_group:
        return True
    return str(relation_tier or "").strip() in TRACKED_TIERS
